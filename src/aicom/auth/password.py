"""Password check and session cookie primitives.

One operator, one password. Account management would be ceremony; what
matters is that an unset password locks the door rather than opening it.
"""

import hmac

from itsdangerous import URLSafeTimedSerializer

SESSION_COOKIE = "aicom_session"
_SALT = "aicom-console-session"
_PAYLOAD = "operator"


def check_password(candidate: str, expected: str) -> bool:
    """Constant-time comparison. An empty configured password rejects everything.

    Compares UTF-8 bytes rather than str: hmac.compare_digest raises TypeError
    when either str argument contains a non-ASCII character, which would turn
    a non-ASCII password attempt into an unhandled 500 instead of a 401 -- and
    would make a non-ASCII console password impossible to log in with at all.
    """
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def issue_session(secret: str) -> str:
    return URLSafeTimedSerializer(secret, salt=_SALT).dumps(_PAYLOAD)


def verify_session(token: str, secret: str, max_age_seconds: int) -> bool:
    if not token or not secret:
        return False
    try:
        URLSafeTimedSerializer(secret, salt=_SALT).loads(token, max_age=max_age_seconds)
    except Exception:  # noqa: BLE001
        # Bad signature, expiry, or outright garbage: a malformed cookie is a
        # rejected cookie, never a 500.
        return False
    return True
