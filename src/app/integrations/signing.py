''' HMAC signatures for the downstream webhook. The mock downstream signs with the same
`sign`, so a mismatch between sender and receiver cannot hide behind a mock.

Signed message: f"{timestamp}.{raw body}". Signing the timestamp is what lets the
receiver enforce a replay window; an unsigned timestamp could just be rewritten.
'''

import hashlib
import hmac
import time

REPLAY_WINDOW_SECONDS = 5 * 60


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(
    secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None
) -> bool:
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - ts) > REPLAY_WINDOW_SECONDS:
        return False
    # Constant-time, on bytes: == leaks how many leading characters matched, and
    # compare_digest refuses non-ASCII str (a junk header would raise, not fail).
    return hmac.compare_digest(sign(secret, timestamp, body).encode(), signature.encode())
