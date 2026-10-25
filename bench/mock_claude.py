''' A stand-in for api.anthropic.com, to load-test /ask without paying for it.

    MOCK_CLAUDE_DELAY=3.0 uvicorn bench.mock_claude:app --port 9100
    ANTHROPIC_BASE_URL=http://localhost:9100 uvicorn app.main:app --no-access-log

The SDK reads ANTHROPIC_BASE_URL itself, so the app needs no test hook. The delay
defaults to 3 s, the Haiku 5.5 medium-effort latency measured in m12.md §8: the point
is to hold each request open as long as a real call would, so the result measures how
our server behaves while it waits, not how fast Claude is.
'''

import asyncio
import os

from fastapi import FastAPI, Request

app = FastAPI(title="Mock Claude")
DELAY = float(os.environ.get("MOCK_CLAUDE_DELAY", "3.0"))


@app.post("/v1/messages")
async def messages(request: Request) -> dict:
    body = await request.json()
    await asyncio.sleep(DELAY)

    # Cite the first search result we were sent, as the real model would.
    results = [
        b
        for m in body.get("messages", [])
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "search_result"
    ]
    block = {"type": "text", "text": "The documents answer this question."}
    if results:
        first = results[0]
        block["citations"] = [
            {
                "type": "search_result_location",
                "cited_text": first["content"][0]["text"],
                "search_result_index": 0,
                "source": first["source"],
                "title": first["title"],
                "start_block_index": 0,
                "end_block_index": 1,
            }
        ]
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "mock"),
        "content": [block],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 2400, "output_tokens": 250},
    }
