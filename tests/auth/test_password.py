import time

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


def test_issued_session_verifies() -> None:
    token = issue_session(SECRET)
    assert verify_session(token, SECRET, max_age_seconds=60) is True


def test_session_from_another_secret_rejected() -> None:
    token = issue_session("other-secret")
    assert verify_session(token, SECRET, max_age_seconds=60) is False


def test_tampered_session_rejected() -> None:
    token = issue_session(SECRET) + "x"
    assert verify_session(token, SECRET, max_age_seconds=60) is False


def test_expired_session_rejected() -> None:
    token = issue_session(SECRET)
    time.sleep(1.1)
    assert verify_session(token, SECRET, max_age_seconds=1) is False


def test_garbage_token_rejected_without_raising() -> None:
    assert verify_session("not-a-token", SECRET, max_age_seconds=60) is False
