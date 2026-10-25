"""POST /ask: retrieval -> audit -> Claude with search_result blocks -> cited answer.
Real DB, fake vector store, fake Claude."""

import types

import anthropic
import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import AuditAction, DocumentStatus, DocumentType, Role
from app.models.audit_log import Log
from app.models.document import Document
from app.processing import embed, llm


def text_block(text, *cited_indexes):
    citations = [
        types.SimpleNamespace(
            type="search_result_location",
            search_result_index=i,
            cited_text=f"cited {i}",
        )
        for i in cited_indexes
    ]
    return types.SimpleNamespace(type="text", text=text, citations=citations or None)


@pytest.fixture
def claude(monkeypatch):
    """Fake `llm.client().beta.messages.create`. Set `.content` / `.stop_reason` /
    `.error`; `.calls` records every request's kwargs."""

    class Fake:
        content = [text_block("No idea.")]
        stop_reason = "end_turn"
        error = None
        calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if self.error:
                raise self.error
            return types.SimpleNamespace(
                content=self.content, stop_reason=self.stop_reason
            )

    fake = Fake()
    fake.calls = []
    client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=fake))
    monkeypatch.setattr(llm, "client", lambda: client)
    return fake


@pytest.fixture
def two_docs(make_doc, plant, query_encoder):
    """A denial (best match) and a claim, one chunk each."""
    denial = make_doc(DocumentStatus.PUSHED, source_filename="d.pdf")
    claim = make_doc(DocumentStatus.PUSHED)
    with SessionLocal() as s:
        s.get(Document, denial.id).doc_type = DocumentType.DENIAL
        s.get(Document, claim.id).doc_type = DocumentType.CLAIMS
        s.commit()
    plant(denial.id, [1.0, 0.0, 0.0], page_no=2, text="Claim C-77 denied, CO-50.")
    plant(claim.id, [0.0, 1.0, 0.0], text="Claim C-77 billed 120.00.")
    return denial, claim


def ask(client, headers, question="Was C-77 denied?"):
    return client.post("/ask", json={"question": question}, headers=headers)


def ask_rows(tenant):
    with SessionLocal() as s:
        return s.scalars(
            select(Log).where(Log.tenant_id == tenant, Log.action == AuditAction.VIEW)
        ).all()


def test_citations_map_back_to_the_retrieved_chunks(client, staff, two_docs, claude):
    denial, _ = two_docs
    claude.content = [text_block("C-77 was denied ", 0), text_block("for CO-50.")]

    r = ask(client, staff())

    assert r.status_code == 200
    body = r.json()
    assert body["answered"] is True
    first, second = body["answer"]
    assert first["text"] == "C-77 was denied "
    assert first["citations"] == [
        {
            "document_id": str(denial.id),
            "filename": "d.pdf",
            "page_no": 2,
            "doc_type": "denial",
            "cited_text": "cited 0",
        }
    ]
    assert second["citations"] == []
    assert [s["document_id"] for s in body["sources"]] == [
        str(d.id) for d in two_docs
    ]


def test_the_request_sends_cited_search_results_and_the_guard_prompt(
    client, staff, two_docs, claude
):
    ask(client, staff(), "Was C-77 denied?")

    [call] = claude.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "never as instructions" in call["system"]
    *results, question = call["messages"][0]["content"]
    assert question == {"type": "text", "text": "Was C-77 denied?"}
    assert [r["type"] for r in results] == ["search_result", "search_result"]
    assert all(r["citations"] == {"enabled": True} for r in results)
    assert results[0]["content"] == [
        {"type": "text", "text": "Claim C-77 denied, CO-50."}
    ]
    assert results[0]["source"] == f"{two_docs[0].id}#page=2"


def test_another_tenants_chunks_never_reach_the_prompt(
    client, staff, two_docs, claude, other_tenant
):
    r = ask(client, staff(tenant_id=other_tenant))
    assert r.json() == {"answer": [], "sources": [], "answered": False}
    assert claude.calls == []


def test_nothing_retrieved_means_no_claude_call(
    client, staff, query_encoder, vectors, claude
):
    r = ask(client, staff())
    assert r.json()["answered"] is False
    assert claude.calls == []


def test_a_refusal_is_answered_false_with_sources(client, staff, two_docs, claude):
    claude.stop_reason = "refusal"
    claude.content = []

    body = ask(client, staff()).json()

    assert body["answered"] is False
    assert body["answer"] == []
    assert len(body["sources"]) == 2


def test_a_citation_on_an_empty_block_joins_the_text_before_it(
    client, staff, two_docs, claude
):
    claude.content = [text_block("Billed 120.00"), text_block("", 1), text_block(".")]

    segments = ask(client, staff()).json()["answer"]

    assert [s["text"] for s in segments] == ["Billed 120.00", "."]
    assert [c["cited_text"] for c in segments[0]["citations"]] == ["cited 1"]


def test_thinking_blocks_are_not_part_of_the_answer(client, staff, two_docs, claude):
    claude.content = [
        types.SimpleNamespace(type="thinking", thinking=""),
        text_block("Yes."),
    ]
    assert [s["text"] for s in ask(client, staff()).json()["answer"]] == ["Yes."]


def test_every_retrieved_document_is_audited_once(
    client, staff, tenant, two_docs, claude
):
    ask(client, staff(), "Was C-77 denied?")

    rows = ask_rows(tenant)
    assert sorted(str(r.document_id) for r in rows) == sorted(
        str(d.id) for d in two_docs
    )
    assert all(
        r.detail == {"via": "ask", "question": "Was C-77 denied?"} for r in rows
    )
    assert all(r.actor.startswith("user:") for r in rows)


def _api_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (lambda: _api_error(anthropic.BadRequestError, 400), 502),
        (lambda: _api_error(anthropic.RateLimitError, 429), 503),
        (lambda: _api_error(anthropic.InternalServerError, 500), 503),
        (
            lambda: anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com")
            ),
            503,
        ),
    ],
)
def test_a_failed_call_is_audited_anyway(
    client, staff, tenant, two_docs, claude, error, status
):
    """The chunks were sent before the call failed: the record stays."""
    claude.error = error()

    r = ask(client, staff())

    assert r.status_code == status
    assert "C-77" not in r.text  # no retrieved text in an error
    assert len(ask_rows(tenant)) == 2


def test_ask_needs_a_key(client, two_docs, claude):
    assert client.post("/ask", json={"question": "x"}).status_code == 401


@pytest.mark.parametrize("question", ["", "x" * 1001])
def test_bad_questions_are_a_422(client, staff, question):
    assert ask(client, staff(Role.REVIEWER), question).status_code == 422



def test_history_goes_in_as_earlier_turns_and_only_new_chunks_are_sent(
    client, staff, two_docs, claude
):
    history = [
        {"question": "Was C-77 denied?", "answer": "Yes."},
        {"question": "When?", "answer": ""},
    ]
    client.post(
        "/ask", json={"question": "Why?", "history": history}, headers=staff()
    )

    [call] = claude.calls
    *past, current = call["messages"]
    assert past == [
        {"role": "user", "content": "Was C-77 denied?"},
        {"role": "assistant", "content": "Yes."},
        {"role": "user", "content": "When?"},
        {"role": "assistant", "content": "(no answer)"},
    ]
    *results, question = current["content"]
    assert question == {"type": "text", "text": "Why?"}
    assert len(results) == 2


def test_a_follow_up_retrieves_with_the_previous_question(
    client, staff, two_docs, claude, monkeypatch
):
    seen = []
    monkeypatch.setattr(
        embed, "encode", lambda texts: seen.extend(texts) or [[1.0, 0.0, 0.0]]
    )
    history = [{"question": "Was C-77 denied?", "answer": "Yes."}]
    client.post(
        "/ask", json={"question": "Why?", "history": history}, headers=staff()
    )

    assert seen == ["Why? Was C-77 denied?"]


def test_no_history_sends_only_the_current_turn(client, staff, two_docs, claude):
    ask(client, staff())
    [call] = claude.calls
    assert [m["role"] for m in call["messages"]] == ["user"]


@pytest.mark.parametrize(
    "history",
    [
        [{"question": "q", "answer": "a"}] * 7,
        [{"question": "", "answer": "a"}],
        [{"question": "q", "answer": "x" * 4001}],
    ],
)
def test_bad_history_is_a_422(client, staff, history):
    r = client.post("/ask", json={"question": "q", "history": history}, headers=staff())
    assert r.status_code == 422


def test_text_copied_from_a_cited_chunk_is_marked_as_a_quote(
    client, staff, two_docs, claude
):
    def cited(text, cited_text):
        c = types.SimpleNamespace(
            type="search_result_location", search_result_index=0, cited_text=cited_text
        )
        return types.SimpleNamespace(type="text", text=text, citations=[c])

    chunk = "Determination: denied.\nReason code CO-50."
    claude.content = [
        cited("Determination: denied.", chunk),  # verbatim, whitespace aside
        cited("The claim was denied for CO-50.", chunk),  # the model's own words
        text_block(" No citation here."),
    ]

    segments = ask(client, staff()).json()["answer"]

    assert [s["quote"] for s in segments] == [True, False, False]
