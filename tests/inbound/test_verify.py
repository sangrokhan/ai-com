import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from aicom.inbound.verify import verify_slack_signature

SECRET = "shhh"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
BODY = b"payload=%7B%22ok%22%3Atrue%7D"


def _sign(timestamp: str, body: bytes, secret: str = SECRET) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_valid_signature_accepted() -> None:
    ts = str(int(NOW.timestamp()))
    assert verify_slack_signature(
        signing_secret=SECRET, timestamp=ts, body=BODY, signature=_sign(ts, BODY), now=NOW
    )


def test_forged_signature_rejected() -> None:
    ts = str(int(NOW.timestamp()))
    assert not verify_slack_signature(
        signing_secret=SECRET,
        timestamp=ts,
        body=BODY,
        signature=_sign(ts, BODY, secret="wrong"),
        now=NOW,
    )


def test_tampered_body_rejected() -> None:
    ts = str(int(NOW.timestamp()))
    assert not verify_slack_signature(
        signing_secret=SECRET,
        timestamp=ts,
        body=b"payload=evil",
        signature=_sign(ts, BODY),
        now=NOW,
    )


def test_replay_outside_five_minute_window_rejected() -> None:
    old = NOW - timedelta(minutes=6)
    ts = str(int(old.timestamp()))
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp=ts, body=BODY, signature=_sign(ts, BODY), now=NOW
    )


def test_garbage_timestamp_rejected() -> None:
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp="nope", body=BODY, signature="v0=abc", now=NOW
    )
