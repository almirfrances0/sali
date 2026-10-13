"""CommsService — the email/calendar surface Sali's tools use (§44).

Resolves the app-password from the SecretStore at connect time (never persisted), and picks the live
IMAP/SMTP/CalDAV clients from CommsSettings — or accepts injected fakes for CI. If an account isn't
configured, the relevant methods raise CommsNotConfigured, which the tool turns into a clear "set up
email first" message rather than a crash.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sali.comms.calendar import CalAccount, CalClient, CalDavCalendar
from sali.comms.mail import ImapSmtpMail, MailAccount, MailClient
from sali.comms.models import CalendarEvent, EmailMessage
from sali.config.settings import CommsSettings


class CommsNotConfigured(RuntimeError):
    """A comms account (email / calendar) hasn't been set up yet."""


class CommsService:
    def __init__(self, settings: CommsSettings, secrets: Any, *,
                 mail: MailClient | None = None, cal: CalClient | None = None) -> None:
        self._settings = settings
        self._secrets = secrets
        self._mail = mail
        self._cal = cal

    # ---- email ----
    def _mail_client(self) -> MailClient:
        if self._mail is not None:
            return self._mail
        acct = self._settings.mail
        if acct is None:
            raise CommsNotConfigured("email isn't set up — add an account + `sali secrets set` its "
                                     "app-password")
        self._mail = ImapSmtpMail(MailAccount(
            address=acct.address, imap_host=acct.imap_host, smtp_host=acct.smtp_host,
            password=self._secrets.require(acct.secret_ref),
            imap_port=acct.imap_port, smtp_port=acct.smtp_port))
        return self._mail

    async def email_search(self, query: str = "", *, limit: int = 20) -> list[EmailMessage]:
        return await self._mail_client().search(query, limit=limit)

    async def email_read(self, uid: str) -> EmailMessage | None:
        return await self._mail_client().read(uid)

    async def email_send(self, to: str, subject: str, body: str) -> str:
        return str(await self._sender().send(to, subject, body))

    def _sender(self) -> Any:
        """The SEND path: the injected fake, or the Gmail API over HTTPS (for SMTP-blocked networks),
        else SMTP. Reading always stays IMAP; only sending moves to the API."""
        if self._mail is not None:
            return self._mail  # the test fake reads and sends
        acct = self._settings.mail
        if acct is None:
            raise CommsNotConfigured("email isn't set up — add an account + `sali secrets set` its "
                                     "app-password")
        if acct.send_backend == "gmail_api":
            from sali.comms.gmail_api import GmailApiSender, GmailOAuth
            return GmailApiSender(acct.address, GmailOAuth(
                client_id=self._secrets.require(acct.gmail_client_id_ref),
                client_secret=self._secrets.require(acct.gmail_client_secret_ref),
                refresh_token=self._secrets.require(acct.gmail_refresh_token_ref)))
        return self._mail_client()  # SMTP (fails clearly if the network blocks it)

    # ---- calendar ----
    def _cal_client(self) -> CalClient:
        if self._cal is not None:
            return self._cal
        acct = self._settings.calendar
        if acct is None:
            raise CommsNotConfigured("calendar isn't set up — add a CalDAV url + `sali secrets set` "
                                     "its password")
        self._cal = CalDavCalendar(CalAccount(
            url=acct.url, username=acct.username, password=self._secrets.require(acct.secret_ref)))
        return self._cal

    async def calendar_list(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return await self._cal_client().list_events(start, end)

    async def calendar_add(self, summary: str, start: datetime, end: datetime,
                          *, location: str | None = None) -> str:
        return await self._cal_client().add_event(summary, start, end, location=location)
