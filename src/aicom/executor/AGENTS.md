<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# executor

## Purpose
Spawns the Claude Code CLI headlessly as a subprocess, parses its `stream-json` output, and reports back a `RunOutcome`. This is where the autonomy boundary is physically enforced: `build_argv` constructs the argv that whitelists tools (`--allowedTools`) and confines the filesystem (`--add-dir`), so a jailbroken or confused agent has no path to a dangerous tool that isn't in the whitelist. `secrets.py` keeps plaintext credentials off disk. `fake.py` provides a scripted stand-in used by every non-CLI test.

## Key Files
| File | Description |
|------|-------------|
| `base.py` | `Executor` protocol, `RunRequest`/`RunOutcome` dataclasses. |
| `cli.py` | `ClaudeCliExecutor` — builds argv, spawns `claude`, enforces deadline, drains stdout/stderr. |
| `stream.py` | `StreamParser` — turns raw `stream-json` lines into `ParsedEvent`s; never raises. |
| `secrets.py` | Resolves `{"secret_ref": ...}` into env-var placeholders before a config touches disk. |
| `workspace.py` | `prepare_workspace` / `cleanup_workspace` — per-run plain directory under `workspaces/<agent>/<run_id>/`. |
| `fake.py` | `FakeExecutor` — replays scripted stream-json lines through a real `StreamParser`. |

## Public Interface
```python
# base.py
@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: uuid.UUID
    prompt: str
    workspace: Path
    allowed_tools: tuple[str, ...]
    mcp_config: dict
    env: dict[str, str]
    resume_session_id: str | None
    timeout_seconds: int

@dataclass(frozen=True, slots=True)
class RunOutcome:
    reason: ExitReason
    exit_code: int
    session_id: str | None = None
    cost_usd: float | None = None
    token_in: int = 0
    token_out: int = 0
    usage_limit_text: str | None = None

class Executor(Protocol):
    def run(self, req: RunRequest, on_event: Callable[[ParsedEvent], None]) -> RunOutcome: ...

# cli.py
def build_argv(req: RunRequest, *, binary: str, mcp_config_path: Path) -> list[str]: ...
class ClaudeCliExecutor(Executor):
    def __init__(self, binary: str = "claude") -> None: ...

# stream.py
@dataclass(frozen=True, slots=True)
class ParsedEvent:
    seq: int
    type: str
    payload: dict
class StreamParser:
    def feed(self, line: str) -> ParsedEvent | None: ...
    session_id: str | None
    cost_usd: float | None
    token_in: int
    token_out: int
    saw_usage_limit: bool
    usage_limit_text: str | None

# secrets.py
def env_var_name(ref: str) -> str: ...
def resolve_secrets(mcp_config: dict, lookup: Callable[[str], str | None]) -> tuple[dict, dict[str, str]]: ...
def assert_resolved(config: object) -> None: ...  # raises MissingSecret
class MissingSecret(Exception): ...
class SecretCollision(Exception): ...

# workspace.py
def prepare_workspace(root: Path, agent_name: str, run_id: uuid.UUID) -> Path: ...
def cleanup_workspace(path: Path) -> None: ...

# fake.py
class FakeExecutor(Executor):
    def queue(self, run_id: uuid.UUID, lines: list[str], *, reason: ExitReason | None = None, exit_code: int = 0) -> None: ...
```

## For AI Agents

### Working In This Directory
- **`build_argv`'s trailing `--` before the prompt is load-bearing.** `--mcp-config`, `--allowedTools`, and `--add-dir` are variadic CLI options; without `--` to close the option list, `--mcp-config` greedily swallows the prompt as another config value instead of leaving it as the positional argument. Every real CLI run was silently broken this way before it was caught — only the smoke test surfaced it because unit tests use `FakeExecutor`. `tests/executor/test_cli_args.py` pins the exact argv shape; do not reorder or drop the `--`.
- **`--allowedTools` is the whitelist, `--add-dir` is the filesystem fence.** Never construct an argv that omits either, and never widen `allowed_tools` at this layer to work around a permission error — that decision belongs upstream, at the `AgentConfig`/gate boundary, not in the executor.
- **`StreamParser.feed` must never raise.** The `except Exception` in `_absorb`'s caller is deliberate (see the `noqa: BLE001` comment) — a parser bug on one malformed line must not kill an otherwise-healthy run. If you change parsing logic, preserve the fallback to `{"raw": stripped}` / type `"unparsed"`.
- **`cli.py` enforces a wall-clock deadline, not a read-to-EOF loop.** `proc.wait(timeout=...)` runs concurrently with the stdout/stderr reader threads; a naive "read until EOF, then check timeout" would let a hung child (holding the pipe open) run forever. stderr is drained on its own thread concurrently with stdout specifically to avoid a pipe-buffer deadlock (Finding 2 in the code comments) — do not collapse them back into sequential reads.
- **`secrets.py` is the last line of defense against plaintext-to-disk.** `ClaudeCliExecutor.run` calls `assert_resolved(req.mcp_config)` before writing the MCP config file; a caller that forgot `resolve_secrets()` upstream fails loudly here instead of leaking credentials into a temp file. Do not remove this guard, and do not call `resolve_secrets` and then mutate the result before it reaches `assert_resolved`.
- **`SecretCollision`** exists because `env_var_name` slugifies refs (non-alphanumerics → `_`); two distinct refs (e.g. `broker/alpaca` and `broker-alpaca`) could collide on the same env var. Treat a collision as a config bug to fix by renaming, never by suppressing the exception.
- `fake.py`'s `FakeExecutor` runs scripted lines through a **real** `StreamParser`, not a hand-rolled stub — this keeps integration tests honest about actual parsing behavior. If you add stream-json fields, `FakeExecutor` picks them up for free; don't duplicate parsing logic in the fake.

### Testing Requirements
- `tests/executor/test_cli_args.py` — pins `build_argv` output, including the `--` separator.
- `tests/executor/test_cli_run.py` — deadline/timeout, concurrent stdout/stderr draining, leaked-thread warning path.
- `tests/executor/test_stream.py`, `test_secrets.py`, `test_workspace.py`, `test_fake.py`.
- Run with `.venv/bin/pytest tests/executor` (no Docker needed).
- One smoke test elsewhere shells out to the real `claude` binary; excluded via `-m "not smoke"`.

### Common Patterns
- Dataclasses are `frozen=True, slots=True` value objects (`RunRequest`, `RunOutcome`, `ParsedEvent`) — construct new instances, don't mutate.
- Reader threads are daemon threads joined with a grace period (`_JOIN_GRACE_SECONDS`), then abandoned with a logged warning if a grandchild is still holding the pipe open — this is intentional leakage, not a bug, because callers (the worker) must guard against late `on_event` calls rather than the executor blocking forever.

## Dependencies

### Internal
- Imports `aicom.domain.enums.ExitReason`.
- Imported by `aicom.orchestrator.worker` (constructs `RunRequest`, calls `Executor.run`, uses `resolve_secrets`/`env_var_name`) and by test fixtures across the repo (`FakeExecutor`).

### External
- Python standard library (`subprocess`, `threading`, `tempfile`, `json`, `re`). No third-party runtime dependency in this package itself.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
