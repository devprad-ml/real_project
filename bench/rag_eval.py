''' RAG eval: fixed questions with known answers, scored as a Langfuse experiment, so a
change to retrieval or prompts can be compared run against run.

    python -m bench.rag_eval load                 # synthetic documents, prints an API key
    python -m bench.rag_eval run <key> [base url] # the API must be running

Synthetic data only: the experiment sends questions and answers to Langfuse, whatever
LANGFUSE_CAPTURE_CONTENT says. A wrong score can still mean a right answer in other words,
so read the failures before believing the number.
'''

import bench.env  # noqa: F401  (must be first)

import hashlib
import json
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from langfuse import Evaluation

from app import keys
from app.config import get_settings
from app.db import SessionLocal
from app.enums import Role
from app.ingestion import pipeline
from app.ingestion.intake import intake_bytes
from app.observability import tracing
from bench import messy, seed

DATASET = "rag-questions"
QUESTIONS = Path(__file__).with_name("rag_questions.json")
NUMBER_WORDS = {6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven"}


def load() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        messy.make(tmp, 7, ["M-1001", "M-2002"], False)
        for name, _, pages in seed.demo_docs():
            (folder / f"demo_{name}").write_bytes(seed.make_pdf(pages))
        with SessionLocal() as session:
            tenant = seed.new_tenant(session, "eval")
            docs = [
                intake_bytes(
                    session, tenant_id=tenant, data=p.read_bytes(), filename=p.name,
                    source_id=None,
                )
                for p in sorted(folder.iterdir())
                if p.name != "manifest.json"
            ]
            ids = [d.id for d in docs if d is not None]
            session.commit()
    with ThreadPoolExecutor(get_settings().ingest_workers) as pool:
        list(pool.map(pipeline.process, ids))
    tracing.shutdown()
    _, key = keys.create(tenant, "eval", [Role.ADMIN])
    print(f"{len(ids)} documents processed\ntenant {tenant}\nkey    {key}")


def correct(answer: str, expected: dict, counts: dict[str, int]) -> bool:
    low = answer.casefold()
    if expected.get("count_type"):
        want = counts.get(expected["count_type"], 0)
        word = NUMBER_WORDS.get(want, "@")
        return bool(re.search(rf"(?<![\d-]){want}(?!\d)", answer)) or word in low
    return all(any(a.casefold() in low for a in alts) for alts in expected.get("must", [])) and not any(
        w.casefold() in low for w in expected.get("must_not", [])
    )


def run(key: str, base: str) -> None:
    client = tracing.client()
    if client is None:
        sys.exit("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY first.")
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    client.create_dataset(name=DATASET)
    for q in questions:
        client.create_dataset_item(
            dataset_name=DATASET,
            id=hashlib.sha256(f"{DATASET}:{q['q']}".encode()).hexdigest()[:32],
            input={"question": q["q"]},
            expected_output={k: q[k] for k in ("must", "must_not", "count_type") if k in q},
            metadata={"kind": q["kind"], "docs": q.get("docs", [])},
        )

    http = httpx.Client(
        base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=180
    )
    counts: dict[str, int] = {}
    for row in http.get("/intake", params={"limit": 500}).json():
        if row["state"] == "ready":
            counts[row["doc_type"]] = counts.get(row["doc_type"], 0) + 1

    def task(*, item, **_):
        body = http.post("/ask", json={"question": item.input["question"]})
        body.raise_for_status()
        answer = body.json()["answer"]
        return {
            "answer": " ".join(s["text"] for s in answer),
            "cited": sorted({c["filename"] for s in answer for c in s["citations"]}),
        }

    def is_correct(*, output, expected_output, **_):
        ok = correct(output["answer"], expected_output or {}, counts)
        return Evaluation(name="correct", value=1.0 if ok else 0.0)

    def is_cited(*, output, metadata, **_):
        if metadata["kind"] in ("negative", "injection", "list"):
            return []
        return Evaluation(name="cited", value=1.0 if output["cited"] else 0.0)

    result = client.get_dataset(DATASET).run_experiment(
        name="rag", task=task, evaluators=[is_correct, is_cited], max_concurrency=3
    )
    print(result.format())
    client.flush()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # the SDK's summary has emoji; cp1252 raises
    args = sys.argv[1:]
    if args == ["load"]:
        load()
    elif len(args) in (2, 3) and args[0] == "run":
        run(args[1], args[2] if len(args) == 3 else "http://127.0.0.1:8000")
    else:
        sys.exit(__doc__)
