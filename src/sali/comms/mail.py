"""Email over stdlib IMAP/SMTP (§44). No async dependency — imaplib/smtplib are blocking, so every
call runs in asyncio.to_thread with an explicit socket timeout so the event loop never stalls.

Auth is an app-password resolved from the SecretStore at connect time (never stored in the DB, never
logged). Sending mints a DETERMINISTIC Message-ID (hash of account+to+subject+body) so re-sending the
same mail is dedup-able rather than a fresh duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.utils
import hashlib
import imaplib
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText
from typing import Any, Protocol

from sali.comms.models import EmailMessage

_TIMEOUT = 20.0  # socket timeout for every IMAP/SMTP op — fail fast, never hang the loop


@dataclass(slots=True)
class MailAccount:
    address: str
    imap_host: str
    smtp_host: str
    password: str  # resolved from SecretStore just before use; never persisted
    imap_port: int = 993
    smtp_port: int = 465


class MailClient(Protocol):
    async def search(self, query: str = "", *, limit: int = 20) -> list[EmailMessage]: ...
    async def read(self, uid: str) -> EmailMessage | None: ...
    async def send(self, to: str, subject: str, body: str) -> str: ...


class ImapSmtpMail:
    def __init__(self, account: MailAccount) -> None:
        self._acct = account

    async def search(self, query: str = "", *, limit: int = 20) -> list[EmailMessage]:
        return await asyncio.to_thread(self._search_blocking, query, limit)

    async def read(self, uid: str) -> EmailMessage | None:
        return await asyncio.to_thread(self._read_blocking, uid)

    async def send(self, to: str, subject: str, body: str) -> str:
        return await asyncio.to_thread(self._send_blocking, to, subject, body)

    # ---- blocking bodies (run in a thread) ----
    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self._acct.imap_host, self._acct.imap_port, timeout=_TIMEOUT)
        conn.login(self._acct.address, self._acct.password)
        return conn

    def _search_blocking(self, query: str, limit: int) -> list[EmailMessage]:
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            criteria = ("TEXT", f'"{query}"') if query.strip() else ("ALL",)
            _typ, data = conn.search(None, *criteria)
            uids = (data[0].split() if data and data[0] else [])[-limit:]
            out = []
            for raw_uid in reversed(uids):  # newest first
                uid = raw_uid.decode()
                _t, hdr = conn.fetch(uid, "(BODY.PEEK[HEADER])")
                out.append(_headers_to_message(uid, hdr))
            return out
        finally:
            _safe_logout(conn)

    def _read_blocking(self, uid: str) -> EmailMessage | None:
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            _typ, data = conn.fetch(uid, "(RFC822)")
            if not data or not isinstance(data[0], tuple):
                return None
            msg = email.message_from_bytes(data[0][1])
            return EmailMessage(
                uid=uid, sender=str(msg.get("From", "")), subject=str(msg.get("Subject", "")),
                date=str(msg.get("Date", "")), body=_extract_body(msg))
        finally:
            _safe_logout(conn)

    def _send_blocking(self, to: str, subject: str, body: str) -> str:
        message_id = _deterministic_id(self._acct.address, to, subject, body)
        mime = MIMEText(body, _charset="utf-8")
        mime["From"] = self._acct.address
        mime["To"] = to
        mime["Subject"] = subject
        mime["Message-ID"] = message_id
        mime["Date"] = email.utils.formatdate()
        with smtplib.SMTP_SSL(self._acct.smtp_host, self._acct.smtp_port, timeout=_TIMEOUT) as smtp:
            smtp.login(self._acct.address, self._acct.password)
            smtp.send_message(mime)
        return message_id


class FakeMailClient:
    """In-memory mailbox for CI — no socket. Seed inbox items; sends are recorded."""

    def __init__(self, inbox: list[EmailMessage] | None = None) -> None:
        self.inbox = list(inbox or [])
        self.sent: list[tuple[str, str, str]] = []

    async def search(self, query: str = "", *, limit: int = 20) -> list[EmailMessage]:
        hits = [m for m in self.inbox
                if not query or query.lower() in (m.subject + m.sender + m.body).lower()]
        return hits[:limit]

    async def read(self, uid: str) -> EmailMessage | None:
        return next((m for m in self.inbox if m.uid == uid), None)

    async def send(self, to: str, subject: str, body: str) -> str:
        self.sent.append((to, subject, body))
        return _deterministic_id("fake", to, subject, body)


def _deterministic_id(account: str, to: str, subject: str, body: str) -> str:
    digest = hashlib.sha256(f"{account}\x00{to}\x00{subject}\x00{body}".encode()).hexdigest()[:32]
    domain = account.split("@")[-1] if "@" in account else "sali.local"
    return f"<{digest}@{domain}>"


def _headers_to_message(uid: str, hdr: object) -> EmailMessage:
    raw = hdr[0][1] if isinstance(hdr, list) and hdr and isinstance(hdr[0], tuple) else b""
    msg = email.message_from_bytes(raw)
    return EmailMessage(uid=uid, sender=str(msg.get("From", "")),
                        subject=str(msg.get("Subject", "")), date=str(msg.get("Date", "")))


def _extract_body(msg: Any) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                    part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", "replace")
    return str(msg.get_payload())


def _safe_logout(conn: imaplib.IMAP4_SSL) -> None:
    # Closing a mail connection must never raise into the caller.
    with contextlib.suppress(Exception):
        conn.logout()
