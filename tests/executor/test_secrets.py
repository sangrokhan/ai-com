import pytest

from aicom.executor.secrets import MissingSecret, resolve_secrets


def test_secret_refs_become_env_vars_and_never_appear_inline() -> None:
    config = {
        "broker": {
            "command": "npx",
            "args": ["broker-mcp"],
            "env": {"API_KEY": {"secret_ref": "broker/alpaca"}},
        }
    }
    sanitized, env = resolve_secrets(config, lambda ref: "s3cr3t" if ref == "broker/alpaca" else None)

    assert env == {"AICOM_SECRET_BROKER_ALPACA": "s3cr3t"}
    assert sanitized["broker"]["env"]["API_KEY"] == "${AICOM_SECRET_BROKER_ALPACA}"
    assert "s3cr3t" not in str(sanitized)


def test_missing_secret_is_a_hard_error() -> None:
    config = {"x": {"env": {"K": {"secret_ref": "nope"}}}}
    with pytest.raises(MissingSecret, match="nope"):
        resolve_secrets(config, lambda _ref: None)


def test_config_without_refs_passes_through_unchanged() -> None:
    config = {"gate": {"command": "python", "args": ["-m", "aicom.gate.server"]}}
    sanitized, env = resolve_secrets(config, lambda _ref: None)
    assert sanitized == config
    assert env == {}
