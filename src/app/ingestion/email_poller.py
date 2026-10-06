''' IMAP channel. Everything mailbox-specific lives in this file, so a Graph API
connector later replaces only this file. '''

import email.message
import email.utils
from typing import Iterator

from imap_tools import AND, MailBox, MailMessageFlags
from sqlalchemy import select

from app.config import get_settings
from app.ingestion.intake import intake_bytes
from app.models.client import Client
from app.models.source import Src

# An email attached to an email attached to an email... Past this it is not a forward,
# it is a bomb.
MAX_EML_DEPTH = 3

# Recipient headers in priority order. Delivered-To survives forwarding and mailing
# lists better than To, which is why it goes first.
_RECIPIENT_HEADERS = ("delivered-to", "to", "cc")


def _open_mailbox():
    s = get_settings()
    return MailBox(s.imap_host).login(
        s.imap_user, s.imap_password.get_secret_value(), s.imap_mailbox
    )


def _attachments(
    part: email.message.Message, depth: int = 0
) -> Iterator[tuple[str, bytes]]:
    ''' (filename, bytes) for every attachment, unwrapping forwarded emails. We walk the
    stdlib message ourselves because message/rfc822 is a multipart whose payload is a
    list, and walk() would double-count what the recursion already yielded. '''
    if part.get_content_type() == "message/rfc822":
        if depth < MAX_EML_DEPTH:
            for inner in part.get_payload():
                yield from _attachments(inner, depth + 1)
        return
    if part.is_multipart():
        for sub in part.get_payload():
            yield from _attachments(sub, depth)
        return

    filename = part.get_filename()
    payload = part.get_payload(decode=True)
    if not filename or not payload:
        return
    # A signature logo: referenced by cid from the HTML body, never an attachment the
    # sender meant as a document.
    if part.get("Content-ID") and part.get_content_disposition() != "attachment":
        return
    yield filename, payload


def _tenant_for(session, msg) -> Client | None:
    for header in _RECIPIENT_HEADERS:
        for _, addr in email.utils.getaddresses(msg.headers.get(header, ())):
            if not addr:
                continue
            client = session.scalar(
                select(Client).where(Client.intake_address == addr.lower())
            )
            if client is not None:
                return client
    return None


def poll_once(session_factory) -> int:
    ''' Fetch unseen mail, create documents, return how many were new.

    One session per message: a poisonous message rolls back only itself. SEEN is set
    after the commit, never before -- a crash in between re-fetches the message and the
    sources.ref guard skips it. The other order loses the document with no trace. '''
    s = get_settings()
    count = 0
    with _open_mailbox() as mb:
        # UIDs are only unique within one UIDVALIDITY epoch; a rebuilt mailbox restarts
        # them, and without this a new message could collide with an old ref.
        validity = mb.folder.status(s.imap_mailbox)["UIDVALIDITY"]
        for msg in mb.fetch(AND(seen=False), mark_seen=False, bulk=True):
            ref = f"{s.imap_user}:{validity}:{msg.uid}"
            try:
                count += _ingest(session_factory, msg, ref)
            except Exception as exc:  # one bad message must not stall the mailbox
                print(f"intake of message {msg.uid} failed: {exc!r}")
                continue
            mb.flag(msg.uid, MailMessageFlags.SEEN, True)
    return count


def _ingest(session_factory, msg, ref: str) -> int:
    s = get_settings()
    with session_factory() as session:
        if session.scalar(select(Src.id).where(Src.ref == ref)) is not None:
            return 0  # already processed; the flag just did not land last time
        client = _tenant_for(session, msg)
        if client is None:
            # ponytail: dropped and logged. A quarantine mailbox is the upgrade once a
            # misrouted email actually costs something.
            print(f"message {msg.uid}: no tenant for recipients, dropped")
            return 0

        # Filter before inserting the source: app_user has no UPDATE on sources, so
        # raw_meta must be complete when the row is written.
        accepted, skipped = [], []
        for filename, payload in _attachments(msg.obj):
            if len(payload) > s.max_attachment_bytes:
                skipped.append({"filename": filename, "reason": "too_large"})
            else:
                accepted.append((filename, payload))

        source = Src(
            tenant_id=client.id,
            ref=ref,
            channel="email",
            raw_meta={
                "from": msg.from_,
                "subject": msg.subject,
                "message_id": (msg.headers.get("message-id") or ("",))[0],
                "skipped": skipped,
            },
        )
        session.add(source)
        session.flush()

        new = 0
        for filename, payload in accepted:
            doc = intake_bytes(
                session,
                tenant_id=client.id,
                data=payload,
                filename=filename,
                source_id=source.id,
            )
            new += doc is not None
        session.commit()
        return new
