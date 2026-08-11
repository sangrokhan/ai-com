import time

import pytest

from aicom.auth.password import (
    check_password,
    issue_session,
    verify_session,
)

SECRET = "test-secret"


def test_correct_password_accepted() -> None:
    assert check_password("hunter2", "hunter2") is True


def test_wrong_password_rejected() -> None:
    assert check_password("wrong", "hunter2") is False


def test_empty_configured_password_rejects_everything() -> None:
    # An unset password must not mean "any password works".
    assert check_password("", "") is False
    assert check_password("anything", "") is False


def test_non_ascii_wrong_password_rejected_without_raising() -> None:
    # hmac.compare_digest raises TypeError on non-ASCII str input; comparing
    # as UTF-8 bytes must avoid that so a wrong non-ASCII password is a clean
    # rejection, not an unhandled 500.
    assert check_password("pässwörd", "hunter2") is False


def test_non_ascii_correct_password_accepted() -> None:
    # A non-ASCII console password (e.g. Korean) must be usable at all.
    assert check_password("한글", "한글") is True


def test_issued_session_verifies() -> None:
    token = issue_session(SECRET)
    assert verify_session(token, SECRET, max_age_seconds=60) is True


def test_session_from_another_secret_rejected() -> None:
    token = issue_session("other-secret")
    assert verify_session(token, SECRET, max_age_seconds=60) is False


def test_tampered_session_rejected() -> None:
    token = issue_session(SECRET) + "x"
    assert verify_session(token, SECRET, max_age_seconds=60) is False


def test_expired_session_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # itsdangerous stamps and compares in integer seconds, so a real sleep()
    # near a second boundary makes the elapsed integer age flaky (it can land
    # on exactly max_age, which is not "expired"). Fake the clock instead of
    # sleeping so the test is deterministic under any load.
    token = issue_session(SECRET)
    real_time = time.time
    monkeypatch.setattr("itsdangerous.timed.time.time", lambda: real_time() + 5)
    assert verify_session(token, SECRET, max_age_seconds=1) is False


def test_garbage_token_rejected_without_raising() -> None:
    assert verify_session("not-a-token", SECRET, max_age_seconds=60) is False
