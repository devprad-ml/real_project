"""The IMAP channel. A fake mailbox stands in for the server; MIME parsing, tenant
routing, the dedupe guards and the commit-then-flag ordering all run for real."""

from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DocumentStatus
from app.ingestion import email_poller
from app.ingestion.email_poller import MAX_EML_DEPTH, _attachments, poll_once
from app.models import Document, Src

from conftest import intake_address, make_message


def _docs(tenant):
    with SessionLocal() as s:
        return s.scalars(select(Document).where(Document.tenant_id == tenant)).all()


def _sources(tenant):
    with SessionLocal() as s:
        return s.scalars(select(Src).where(Src.tenant_id == tenant)).all()


def test_one_attachment_becomes_one_document(tenant, blob, mailbox):
    mailbox.messages = [
        make_message(1, to=intake_address(tenant), attachments=[("a.pdf", b"AAA")])
    ]

    assert poll_once(SessionLocal) == 1

    [doc] = _docs(tenant)
    assert doc.status == DocumentStatus.RECEIVED
    assert doc.source_filename == "a.pdf"
    [src] = _sources(tenant)
    assert doc.source_id == src.id
    assert src.channel == "email"
    assert src.raw_meta["from"] == "billing@hospital.example"
    assert mailbox.seen == {"1"}


def test_three_attachments_are_three_documents_and_one_source(tenant, blob, mailbox):
    mailbox.messages = [
        make_message(
            1,
            to=intake_address(tenant),
            attachments=[("a.pdf", b"AAA"), ("b.pdf", b"BBB"), ("c.pdf", b"CCC")],
        )
    ]

    assert poll_once(SessionLocal) == 3

    assert len(_docs(tenant)) == 3
    [src] = _sources(tenant)
    assert {d.source_id for d in _docs(tenant)} == {src.id}


def test_a_repolled_message_is_skipped(tenant, blob, mailbox):
    """The flag did not land last time; the message comes back. sources.ref is what
    makes that harmless."""
    mailbox.messages = [
        make_message(1, to=intake_address(tenant), attachments=[("a.pdf", b"AAA")])
    ]
    poll_once(SessionLocal)
    mailbox.seen.clear()

    assert poll_once(SessionLocal) == 0

    assert len(_docs(tenant)) == 1
    assert len(_sources(tenant)) == 1
    assert mailbox.seen == {"1"}


def test_the_same_file_in_two_emails_is_one_document(tenant, blob, mailbox):
    """ref dedupe cannot catch this -- different messages. sha256 does."""
    to = intake_address(tenant)
    mailbox.messages = [
        make_message(1, to=to, attachments=[("a.pdf", b"AAA")]),
        make_message(2, to=to, attachments=[("fwd.pdf", b"AAA")]),
    ]

    assert poll_once(SessionLocal) == 1

    assert len(_docs(tenant)) == 1
    assert len(_sources(tenant)) == 2
    assert mailbox.seen == {"1", "2"}


def test_an_unknown_recipient_is_dropped_and_flagged(tenant, blob, mailbox):
    mailbox.messages = [
        make_message(1, to="intake+nobody@example.com", attachments=[("a.pdf", b"AAA")])
    ]

    assert poll_once(SessionLocal) == 0

    assert _docs(tenant) == [] and _sources(tenant) == []
    assert mailbox.seen == {"1"}  # flagged, or it would be re-fetched forever


def test_delivered_to_beats_a_non_matching_to(tenant, blob, mailbox):
    """A forwarded or list-delivered message: To is the original recipient, the
    plus-address only survives in Delivered-To."""
    mailbox.messages = [
        make_message(
            1,
            to="someone-else@hospital.example",
            attachments=[("a.pdf", b"AAA")],
            extra_headers={"Delivered-To": intake_address(tenant)},
        )
    ]

    assert poll_once(SessionLocal) == 1


def test_an_oversized_attachment_is_skipped_and_recorded(
    tenant, blob, mailbox, monkeypatch
):
    small = get_settings().model_copy(update={"max_attachment_bytes": 10})
    monkeypatch.setattr(email_poller, "get_settings", lambda: small)
    mailbox.messages = [
        make_message(
            1,
            to=intake_address(tenant),
            attachments=[("big.pdf", b"X" * 100), ("ok.pdf", b"fine")],
        )
    ]

    assert poll_once(SessionLocal) == 1

    [doc] = _docs(tenant)
    assert doc.source_filename == "ok.pdf"
    [src] = _sources(tenant)
    assert src.raw_meta["skipped"] == [{"filename": "big.pdf", "reason": "too_large"}]
    assert not any("big.pdf" in k for k in blob.data)  # never written


def test_a_failure_before_the_commit_loses_nothing(
    tenant, blob, mailbox, monkeypatch
):
    """The message must NOT be flagged. If it were, the document would be gone with no
    trace anywhere -- the whole reason SEEN is set after the commit."""
    mailbox.messages = [
        make_message(1, to=intake_address(tenant), attachments=[("a.pdf", b"AAA")])
    ]
    real = email_poller.intake_bytes

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(email_poller, "intake_bytes", boom)
    assert poll_once(SessionLocal) == 0
    assert mailbox.seen == set()
    assert _docs(tenant) == [] and _sources(tenant) == []  # rolled back with it

    monkeypatch.setattr(email_poller, "intake_bytes", real)
    assert poll_once(SessionLocal) == 1
    assert len(_docs(tenant)) == 1


def test_a_flag_that_fails_after_the_commit_does_not_duplicate(tenant, blob, mailbox):
    mailbox.messages = [
        make_message(1, to=intake_address(tenant), attachments=[("a.pdf", b"AAA")])
    ]
    mailbox.flag_fails = True
    with pytest.raises(ConnectionError):
        poll_once(SessionLocal)
    assert len(_docs(tenant)) == 1  # the commit already happened

    mailbox.flag_fails = False
    assert poll_once(SessionLocal) == 0  # the re-poll is a no-op, not a duplicate

    assert len(_docs(tenant)) == 1
    assert mailbox.seen == {"1"}


# --- _attachments: pure MIME, no DB ------------------------------------------


def _wrap(inner: EmailMessage, times: int) -> EmailMessage:
    for _ in range(times):
        outer = EmailMessage()
        outer["Subject"] = "Fwd"
        outer.set_content("forwarded")
        outer.add_attachment(inner)
        inner = outer
    return inner


def _with_pdf() -> EmailMessage:
    msg = EmailMessage()
    msg.set_content("original")
    msg.add_attachment(
        b"%PDF-real", maintype="application", subtype="pdf", filename="real.pdf"
    )
    return msg


def test_a_forwarded_email_is_unwrapped_not_stored_as_eml():
    found = list(_attachments(_wrap(_with_pdf(), 1)))
    assert found == [("real.pdf", b"%PDF-real")]


def test_nesting_stops_at_the_depth_cap():
    assert list(_attachments(_wrap(_with_pdf(), MAX_EML_DEPTH)))  # deepest allowed
    assert list(_attachments(_wrap(_with_pdf(), MAX_EML_DEPTH + 1))) == []


def test_a_signature_logo_is_not_a_document():
    msg = EmailMessage()
    msg.set_content("hello")
    msg.add_attachment(
        b"PNG", maintype="image", subtype="png", filename="logo.png",
        disposition="inline", cid="<logo@x>",
    )
    msg.add_attachment(
        b"", maintype="application", subtype="pdf", filename="empty.pdf"
    )
    msg.add_attachment(b"bytes", maintype="application", subtype="pdf")  # no filename
    assert list(_attachments(msg)) == []
