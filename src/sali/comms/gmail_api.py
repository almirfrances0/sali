"""Send Gmail over the HTTPS API (§44) — the way that works when the network blocks SMTP.

Many networks block outbound SMTP (465/587/25) entirely, so smtplib can never connect. The Gmail API
runs over HTTPS (443), which is open, and sends AS the user's own address. Auth is OAuth2: a one-time
consent (via `sali email-auth`) yields a refresh token stored in the SecretStore; each send swaps it
for a short-lived access token. Raw httpx — no google client library.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.mime.text import MIMEText
from urllib.parse import urlencode

import httpx

from sali.comms.mail import MailUnavailable, _deterministic_id

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - a public URL, not a secret
_SEND_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_TIMEOUT = 20.0


@dataclass(slots=True)
class GmailOAuth:
    client_id: str
    client_secret: str
    refresh_token: str


def authorization_url(client_id: str, redirect_uri: str) -> str:
    """The URL Almir opens to grant send access (offline → we get a refresh token)."""
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": _SCOPE, "access_type": "offline", "prompt": "consent",
    }
    return f"{_AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    """Trade the one-time consent code for a durable refresh token."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_TOKEN_ENDPOINT, data={
            "client_id": client_id, "client_secret": client_secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri,
        })
    resp.raise_for_status()
    token = resp.json().get("refresh_token")
    if not token:
        raise MailUnavailable("Google did not return a refresh token — re-run consent with prompt=consent")
    return str(token)


class GmailApiSender:
    def __init__(self, address: str, oauth: GmailOAuth) -> None:
        self._address = address
        self._oauth = oauth

    async def _access_token(self) -> str:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_TOKEN_ENDPOINT, data={
                "client_id": self._oauth.client_id, "client_secret": self._oauth.client_secret,
                "refresh_token": self._oauth.refresh_token, "grant_type": "refresh_token",
            })
        if resp.status_code != 200:
            raise MailUnavailable(
                "Gmail API auth failed — the refresh token may be revoked; re-run `sali email-auth`")
        return str(resp.json()["access_token"])

    async def send(self, to: str, subject: str, body: str) -> str:
        message_id = _deterministic_id(self._address, to, subject, body)
        mime = MIMEText(body, _charset="utf-8")
        mime["From"] = self._address
        mime["To"] = to
        mime["Subject"] = subject
        mime["Message-ID"] = message_id
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        token = await self._access_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _SEND_ENDPOINT, headers={"Authorization": f"Bearer {token}"}, json={"raw": raw})
        if resp.status_code >= 400:
            raise MailUnavailable(f"Gmail API rejected the send ({resp.status_code}): {resp.text[:200]}")
        return message_id
