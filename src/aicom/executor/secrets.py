import re
from collections.abc import Callable

_SLUG = re.compile(r"[^A-Za-z0-9]+")


class MissingSecret(Exception):
    pass


class SecretCollision(Exception):
    """Two distinct secret_ref values slugify to the same env var name."""


def env_var_name(ref: str) -> str:
    return "AICOM_SECRET_" + _SLUG.sub("_", ref).strip("_").upper()


def resolve_secrets(
    mcp_config: dict, lookup: Callable[[str], str | None]
) -> tuple[dict, dict[str, str]]:
    """Replace {"secret_ref": name} nodes with ${ENV_VAR} placeholders and
    return the env vars to inject. Plaintext never reaches disk or the DB."""
    env: dict[str, str] = {}
    name_to_ref: dict[str, str] = {}

    def walk(node: object) -> object:
        if isinstance(node, dict):
            ref = node.get("secret_ref")
            if isinstance(ref, str) and len(node) == 1:
                value = lookup(ref)
                if value is None:
                    raise MissingSecret(f"unresolved secret_ref: {ref}")
                name = env_var_name(ref)
                existing_ref = name_to_ref.get(name)
                if existing_ref is not None and existing_ref != ref:
                    raise SecretCollision(
                        f"secret_ref {ref!r} and {existing_ref!r} both slugify to "
                        f"env var {name}; rename one of them"
                    )
                name_to_ref[name] = ref
                env[name] = value
                return "${" + name + "}"
            return {key: walk(val) for key, val in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    sanitized = walk(mcp_config)
    assert isinstance(sanitized, dict)
    return sanitized, env


def assert_resolved(config: object) -> None:
    """Raise MissingSecret if `config` still contains a raw {"secret_ref": ...}
    node. Guards the boundary right before a config is written to disk: a
    caller bug that forgets to call resolve_secrets() must not silently leak
    plaintext credentials into a file."""
    if isinstance(config, dict):
        ref = config.get("secret_ref")
        if isinstance(ref, str) and len(config) == 1:
            raise MissingSecret(
                f"config still contains an unresolved secret_ref: {ref!r} "
                "(resolve_secrets() must run before this config is written to disk)"
            )
        for value in config.values():
            assert_resolved(value)
    elif isinstance(config, list):
        for item in config:
            assert_resolved(item)
