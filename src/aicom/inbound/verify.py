import hashlib
import hmac
from datetime import datetime

REPLAY_WINDOW_SECONDS = 300


def verify_slack_signature(
    *, signing_secret: str, timestamp: str, body: bytes, signature: str, now: datetime
) -> bool:
    """The interaction endpoint is public. Without this, anyone could forge a
    request and approve real spending."""
    if not signing_secret:
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(now.timestamp()) - sent_at) > REPLAY_WINDOW_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
