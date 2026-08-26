"""Email + calendar (§44): the fake clients, the service, not-configured handling, and the tools."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sali.comms.calendar import FakeCalClient
from sali.comms.mail import FakeMailClient
from sali.comms.models import CalendarEvent, EmailMessage
from sali.comms.service import CommsNotConfigured, CommsService
from sali.config.secrets import FakeSecretStore
from sali.config.settings import CommsSettings, Settings
from sali.core.clock import SystemClock
from sali.core.enums import RiskLevel
from sali.tools.builtins.comms_tool import (
    CalendarAdd,
    CalendarList,
    EmailRead,
    EmailSearch,
    EmailSend,
)
from sali.tools.context import ToolContext


def _inbox() -> list[EmailMessage]:
    return [
        EmailMessage("1", "boss@work.com", "Q3 numbers", "Mon", body="the figures are attached"),
        EmailMessage("2", "friend@x.com", "lunch?", "Tue", body="wanna grab lunch"),
    ]


def _svc(**kw: object) -> CommsService:
    return CommsService(CommsSettings(), FakeSecretStore(), **kw)  # type: ignore[arg-type]


# ---- fake mail client --------------------------------------------------------------------------
async def test_fake_mail_search_read_send() -> None:
    client = FakeMailClient(_inbox())
    assert len(await client.search()) == 2
    assert [m.uid for m in await client.search("lunch")] == ["2"]  # matches subject/body
    read = await client.read("1")
    assert read is not None and read.sender == "boss@work.com"
    mid = await client.send("a@b.com", "hi", "there")
    assert client.sent == [("a@b.com", "hi", "there")] and mid.startswith("<")


async def test_send_message_id_is_deterministic() -> None:
    a = await FakeMailClient().send("a@b.com", "s", "body")
    b = await FakeMailClient().send("a@b.com", "s", "body")
    assert a == b  # same content → same Message-ID (idempotency)


# ---- service routing + not-configured ----------------------------------------------------------
async def test_service_routes_to_injected_fakes() -> None:
    svc = _svc(mail=FakeMailClient(_inbox()), cal=FakeCalClient())
    assert len(await svc.email_search()) == 2
    uid = await svc.calendar_add("standup", datetime(2026, 8, 26, 9, tzinfo=UTC),
                                 datetime(2026, 8, 26, 9, 15, tzinfo=UTC))
    assert uid.startswith("fake-")


async def test_service_raises_when_not_configured() -> None:
    with pytest.raises(CommsNotConfigured):
        await _svc().email_search()  # no account, no fake → clearly not set up
    with pytest.raises(CommsNotConfigured):
        await _svc().calendar_list(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))


# ---- tools -------------------------------------------------------------------------------------
def _ctx(svc: CommsService) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), comms=svc)


async def test_email_tools() -> None:
    ctx = _ctx(_svc(mail=FakeMailClient(_inbox())))
    listed = await EmailSearch().run({"query": "Q3"}, ctx)
    assert listed.ok and any("Q3 numbers" in s for s in listed.output["messages"])
    read = await EmailRead().run({"uid": "1"}, ctx)
    assert read.ok and read.output["sender"] == "boss@work.com"
    sent = await EmailSend().run({"to": "x@y.com", "subject": "hey", "body": "yo"}, ctx)
    assert sent.ok and sent.output["to"] == "x@y.com"


async def test_calendar_tools() -> None:
    events = [CalendarEvent("e1", "meeting", datetime(2026, 8, 26, 10, tzinfo=UTC))]
    ctx = _ctx(_svc(cal=FakeCalClient(events)))
    listed = await CalendarList().run({"start": "2026-08-26", "end": "2026-08-27"}, ctx)
    assert listed.ok and any("meeting" in e for e in listed.output["events"])
    added = await CalendarAdd().run(
        {"summary": "dentist", "start": "2026-09-01T14:00", "end": "2026-09-01T15:00"}, ctx)
    assert added.ok and added.output["summary"] == "dentist"


async def test_email_tool_reports_not_configured_clearly() -> None:
    res = await EmailSearch().run({}, _ctx(_svc()))  # no account
    assert not res.ok and "set up" in (res.error or "")


def test_gmail_authorization_url() -> None:
    from sali.comms.gmail_api import authorization_url

    url = authorization_url("cid123", "http://localhost:8080")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid123" in url  # offline + consent → we get a durable refresh token
    assert "access_type=offline" in url and "prompt=consent" in url and "gmail.send" in url


def _gmail_settings() -> CommsSettings:
    from sali.config.settings import MailAccountConfig

    return CommsSettings(mail=MailAccountConfig(
        address="salieno.co@gmail.com", imap_host="imap.gmail.com", smtp_host="smtp.gmail.com",
        send_backend="gmail_api"))


def test_send_routes_to_gmail_api_over_https_when_configured() -> None:
    from sali.comms.gmail_api import GmailApiSender

    secrets = FakeSecretStore({"mail.password": "pw", "gmail.client_id": "cid",
                               "gmail.client_secret": "sec", "gmail.refresh_token": "rt"})
    sender = CommsService(_gmail_settings(), secrets)._sender()
    assert isinstance(sender, GmailApiSender)  # send goes over the API, not blocked SMTP


def test_send_raises_when_gmail_creds_missing() -> None:
    from sali.config.secrets import SecretNotFound

    secrets = FakeSecretStore({"mail.password": "pw"})  # no OAuth creds yet
    with pytest.raises(SecretNotFound):
        CommsService(_gmail_settings(), secrets)._sender()  # → prompts to run `sali email-auth`


def test_outbound_risk_levels() -> None:
    # Almir's call: email sends immediately, no confirm (do-not-restrict). Creating a calendar event
    # on a shared calendar still confirms. Reading is always free.
    assert EmailSend.risk_level is RiskLevel.R2  # sends freely, no confirm
    assert CalendarAdd.risk_level is RiskLevel.R4  # still confirms
    assert EmailSearch.risk_level is RiskLevel.R1
