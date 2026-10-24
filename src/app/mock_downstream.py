''' A stand-in for the downstream claims system, so the push and webhook paths can run
for real on one laptop.

    uvicorn app.mock_downstream:app --port 9000

Env: MOCK_FAIL_RATE (0..1, share of pushes answered 503), MOCK_WEBHOOK_URL (default
http://localhost:8000/webhooks/downstream). State is in memory: a restart forgets.
It signs with `app.integrations.signing.sign`, the same code the receiver verifies with.
'''

import json
import os
import random
import time
import uuid

import httpx
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
)

from app.config import get_settings
from app.integrations.signing import sign

app = FastAPI(title="Mock downstream")
# Idempotency-Key -> the first response. A replayed key gets that back, nothing else.
seen: dict[str, dict] = {}

WEBHOOK_TRIES = 5


def verdict_for(metadata: dict) -> dict:
    ''' Rejects what a real payer would: a document bound to no member. '''
    if metadata.get("member_id") is None:
        return {
            "external_id": metadata["external_id"],
            "status": "rejected",
            "reason": "member not eligible",
        }
    return {"external_id": metadata["external_id"], "status": "accepted"}


def send_verdict(verdict: dict) -> None:
    ''' Signed callback. Retries non-2xx, as real webhook senders do: a 409 means the
    receiver has not committed the push yet. '''
    url = os.environ.get("MOCK_WEBHOOK_URL", "http://localhost:8000/webhooks/downstream")
    secret = get_settings().downstream_webhook_secret.get_secret_value()
    body = json.dumps(verdict).encode()
    for attempt in range(WEBHOOK_TRIES):
        time.sleep(2**attempt)
        ts = str(int(time.time()))  # re-signed per try: the timestamp moves on
        try:
            r = httpx.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Timestamp": ts,
                    "X-Signature": sign(secret, ts, body),
                },
                timeout=10,
            )
            if r.is_success:
                return
            print(f"webhook {verdict['external_id']}: {r.status_code}")
        except httpx.HTTPError as exc:
            print(f"webhook {verdict['external_id']}: {exc!r}")
    print(f"webhook {verdict['external_id']}: gave up")


@app.post("/documents", status_code=202)
def receive(
    background: BackgroundTasks,
    metadata: str = Form(...),
    file: UploadFile = File(...),
    idempotency_key: str = Header(...),
) -> dict:
    if random.random() < float(os.environ.get("MOCK_FAIL_RATE", "0")):
        raise HTTPException(503, detail="mock outage")
    if idempotency_key in seen:
        return seen[idempotency_key]
    meta = json.loads(metadata)
    seen[idempotency_key] = {"id": str(uuid.uuid4()), "external_id": meta["external_id"]}
    background.add_task(send_verdict, verdict_for(meta))
    return seen[idempotency_key]
