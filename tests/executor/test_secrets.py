import pytest

from aicom.executor.secrets import (
    MissingSecret,
    SecretCollision,
    assert_resolved,
    resolve_secrets,
)


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


def test_distinct_refs_that_slugify_to_the_same_env_var_raise() -> None:
    config = {
        "a": {"env": {"K1": {"secret_ref": "broker/alpaca"}}},
        "b": {"env": {"K2": {"secret_ref": "broker-alpaca"}}},
    }
    with pytest.raises(SecretCollision):
        resolve_secrets(config, lambda _ref: "s3cr3t")


def test_same_ref_used_twice_does_not_collide() -> None:
    config = {
        "a": {"env": {"K1": {"secret_ref": "broker/alpaca"}}},
        "b": {"env": {"K2": {"secret_ref": "broker/alpaca"}}},
    }
    sanitized, env = resolve_secrets(config, lambda _ref: "s3cr3t")
    assert env == {"AICOM_SECRET_BROKER_ALPACA": "s3cr3t"}
    assert sanitized["a"]["env"]["K1"] == "${AICOM_SECRET_BROKER_ALPACA}"
    assert sanitized["b"]["env"]["K2"] == "${AICOM_SECRET_BROKER_ALPACA}"


def test_assert_resolved_raises_on_residual_secret_ref() -> None:
    config = {"x": {"env": {"K": {"secret_ref": "still/raw"}}}}
    with pytest.raises(MissingSecret, match="still/raw"):
        assert_resolved(config)


def test_assert_resolved_passes_on_sanitized_config() -> None:
    config = {"x": {"env": {"K": "${AICOM_SECRET_STILL_RAW}"}}}
    assert_resolved(config)  # must not raise
