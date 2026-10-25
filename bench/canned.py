''' Shared by bench.pipeline and bench.crash: canned Claude answers for worker
processes, and a mock downstream to push to.

"Excluding LLM latency" (DESIGN.md §9) means the two Claude calls the pipeline makes,
classify and extract, return instantly with a fixed, gate-passing answer. Everything
else is real: the Postgres queue, chunking, MiniLM embedding, qdrant, association,
routing, and the HTTP push.
'''

import bench.env  # noqa: F401  (must be first)

import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx

from app.config import get_settings

MEMBER = "M-0001"


def patch_claude() -> None:
    ''' Call inside a worker process, before run_forever. '''
    from app.enums import DocumentType
    from app.processing import classify, extract

    def classified(pages):
        return classify.ClassifyResult(doc_type=DocumentType.CLAIMS, confidence=0.95)

    def extracted(doc_type, pages):
        values = {
            "member_id": MEMBER,
            "claim_number": "C-1",
            "service_date": "2026-08-03",
            "billed_amount": "1250.00",
        }
        return extract.ExtractionResult(
            fields=[
                extract.ExtractedField(name=n, value=v, confidence=0.97)
                for n, v in values.items()
            ],
            summary="canned summary",
        )

    classify.run = classified
    extract.run = extracted


def mock_downstream_url() -> str:
    return get_settings().downstream_base_url.rstrip("/")


def ensure_mock_downstream() -> subprocess.Popen | None:
    ''' Start the mock if nothing answers at DOWNSTREAM_BASE_URL. Returns the process
    it started (to stop later), or None if one was already running. '''
    url = mock_downstream_url()
    try:
        httpx.get(f"{url}/_received", timeout=2)
        return None
    except httpx.HTTPError:
        pass
    port = urlparse(url).port or 9000
    import os

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.mock_downstream:app", "--port", str(port),
         "--no-access-log", "--log-level", "warning"],
        env={**os.environ, "MOCK_WEBHOOK_URL": "off"},
    )
    for _ in range(60):
        try:
            httpx.get(f"{url}/_received", timeout=2)
            return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("mock downstream did not start")


def received_keys() -> list[str]:
    return httpx.get(f"{mock_downstream_url()}/_received", timeout=5).json()
