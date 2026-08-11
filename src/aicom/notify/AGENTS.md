<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# notify

## Purpose
Builds and sends the Slack messages that carry approval requests, reminders, and run reports — the human-facing side of the approval gate. `blocks.py` renders `ApprovalView`/`RunReport` domain data into Slack Block Kit JSON; `slack.py` sends it via the Slack Web API; `fake.py` is the in-memory double used by orchestrator/gate tests. The button `value` payloads produced here are exactly what `inbound/app.py` parses back out, so the two modules must agree on shape.

## Key Files
| File | Description |
|------|-------------|
| `base.py` | `Notifier` protocol — the interface the orchestrator and sweeper depend on. |
| `blocks.py` | Pure functions building Block Kit block lists from domain views. |
| `slack.py` | `SlackNotifier` — real implementation over `slack_sdk.WebClient`. |
| `fake.py` | `FakeNotifier` — records calls in lists for assertions in tests. |

## Public Interface
```python
# base.py
class Notifier(Protocol):
    def send_approval_request(self, view: ApprovalView) -> DispatchRef: ...
    def send_batch_approval_request(self, views: Sequence[ApprovalView]) -> DispatchRef: ...
    def send_reminder(self, view: ApprovalView, stage: int) -> None: ...
    def send_run_report(self, report: RunReport) -> None: ...
    def send_system_notice(self, text: str) -> None: ...

# blocks.py
def approval_blocks(view: ApprovalView) -> list[dict]: ...
def batch_approval_blocks(views: Sequence[ApprovalView]) -> list[dict]: ...
def run_report_blocks(report: RunReport) -> list[dict]: ...

# slack.py
class SlackNotifier(Notifier):
    def __init__(self, client: WebClient, channel: str, dm_user_id: str = "") -> None: ...

# fake.py
class FakeNotifier(Notifier):
    approvals: list[ApprovalView]
    batches: list[list[ApprovalView]]
    reminders: list[tuple[ApprovalView, int]]
    reports: list[RunReport]
    notices: list[str]
```

## For AI Agents

### Working In This Directory
- **`approve`/`reject` buttons carry a single nonce under key `"nonce"`; `approve_all` carries a list under `"nonces"`.** See `_decision_buttons` (`value=json.dumps({"nonce": nonce})`) vs. the trailing `approve_all` button in `batch_approval_blocks` (`value=json.dumps({"nonces": [v.nonce for v in views]})`). `inbound/app.py`'s `_collect_nonces` branches on `action_id` expecting exactly these two shapes. **Never conflate them** — e.g. never make a single-item batch use the `"nonce"` key, and never make `approve`/`reject` accept a list. If you add a new decision button, give it a distinct `action_id` and document its value shape here and in `inbound/AGENTS.md`.
- **`_BATCH_CHUNK_SIZE = 10` exists because Slack rejects messages over 50 blocks.** `batch_approval_blocks` emits 4 blocks per approval plus 2 fixed blocks; at 10 approvals per chunk that's 42 blocks, safely under the limit. If you change how many blocks each approval renders (e.g. add a field), recompute the safe chunk size — a message Slack rejects for exceeding the block limit is a sign-off the operator never sees, and an unsigned approval leaves its run parked in `AWAITING_APPROVAL` indefinitely (approvals never expire, so this fails silently, not loudly).
- **An `approve_all` action must only ever be able to claim nonces that were actually included in that message.** `batch_approval_blocks` embeds the exact chunk's nonces in the button value at send time — do not change this to reference nonces by any other means (e.g. "all pending of this kind") that could let a stale or forwarded message claim approvals it never displayed.
- **Reminders thread onto the original approval message.** `SlackNotifier.send_reminder` posts into the same thread via `view.slack_channel`/`view.slack_ts` when `stage == 1` (`"thread"`); stage 2 DMs (`_dm_user_id`), stage 3+ falls through to the channel-wide digest post. These fields on `ApprovalView` (`domain/views.py`) must be populated by whoever persists the result of the original `send_approval_request`/`send_batch_approval_request` call — if the sweeper reads a view where they're still `None`, thread-anchored reminders silently degrade to a bare channel post instead of erroring, so check that the write-back actually happened rather than trusting silence.
- Text is truncated defensively in `blocks.py` (`_header` to 150 chars, `_section` to 2900 chars) because Slack enforces hard limits on these fields — don't remove the truncation without confirming Slack's current limits.

### Testing Requirements
- `tests/notify/test_blocks.py` — block shape, chunking, truncation, button value shapes.
- `tests/notify/test_slack.py` — `SlackNotifier` behavior against a fake/mocked `WebClient`.
- Run with `.venv/bin/pytest tests/notify` (no Docker needed).

### Common Patterns
- `blocks.py` functions are pure (`view/report -> list[dict]`), independently testable without any Slack client — keep new block-building logic here rather than inline in `slack.py`.
- `FakeNotifier` just appends to plain lists/tuples rather than modeling Slack semantics (no thread/channel bookkeeping) — tests assert on call history, not on rendered output.

## Dependencies

### Internal
- Imports `aicom.domain.views` (`ApprovalView`, `DispatchRef`, `RunReport`).
- Imported by `aicom.orchestrator.worker` and `aicom.orchestrator.sweeper` (both depend on the `Notifier` protocol, constructed with either `SlackNotifier` or `FakeNotifier`).

### External
- `slack_sdk.WebClient` (only in `slack.py`); everything else is stdlib (`json`, `dataclasses`).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
