import re
from collections.abc import Callable

_SLUG = re.compile(r"[^A-Za-z0-9]+")


class MissingSecret(Exception):
    pass


def env_var_name(ref: str) -> str:
    return "AICOM_SECRET_" + _SLUG.sub("_", ref).strip("_").upper()


def resolve_secrets(
    mcp_config: dict, lookup: Callable[[str], str | None]
) -> tuple[dict, dict[str, str]]:
    """Replace {"secret_ref": name} nodes with ${ENV_VAR} placeholders and
    return the env vars to inject. Plaintext never reaches disk or the DB."""
    env: dict[str, str] = {}

    def walk(node: object) -> object:
        if isinstance(node, dict):
            ref = node.get("secret_ref")
            if isinstance(ref, str) and len(node) == 1:
                value = lookup(ref)
                if value is None:
                    raise MissingSecret(f"unresolved secret_ref: {ref}")
                name = env_var_name(ref)
                env[name] = value
                return "${" + name + "}"
            return {key: walk(val) for key, val in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    sanitized = walk(mcp_config)
    assert isinstance(sanitized, dict)
    return sanitized, env
