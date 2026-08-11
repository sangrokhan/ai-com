<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# quota

## Purpose
Parses account usage-limit signals out of Claude CLI error text and computes when to retry. The whole system runs on a single personal Claude subscription, so hitting the account's usage limit pauses every worker at once (via `store.system_state`) — this module decides *when* that pause should end, either by extracting a reset time the CLI reported or, if none can be found, by backing off conservatively.

## Key Files
| File | Description |
|------|-------------|
| `reset.py` | `parse_reset_at` (extract reset time from CLI text) and `fallback_backoff` (escalating backoff when no time can be parsed). |

## Public Interface
```python
def parse_reset_at(text: str, *, now: datetime) -> datetime | None:
    """Extract when the account limit resets. Returns None if unknown or already past."""

def fallback_backoff(consecutive: int, *, now: datetime) -> datetime:
    """Used when the reset time cannot be parsed. Never hammer the account."""
```
`parse_reset_at` tries three strategies in order — epoch (`|<9-11 digits>`), ISO 8601 (`YYYY-MM-DD[T ]HH:MM[:SS][Z|±HH:MM]`), and bare clock time (`H(:MM)? am|pm`) — and returns the first candidate that is strictly after `now`.

`fallback_backoff` maps `consecutive` (clamped to `[0, len(steps)-1]`) to one of three fixed steps: 15 min, 30 min, 1 hour.

## For AI Agents

### Working In This Directory
- **This module is pure: no I/O, no clock reads.** The caller always passes `now` explicitly — never call `datetime.now()`/`datetime.utcnow()` inside this file. This is what makes both functions trivially unit-testable and keeps quota logic out of sync with wall-clock flakiness.
- **A parsed reset time already in the past must yield `None`.** All three sub-parsers in `parse_reset_at` are filtered by `candidate > now` in the top-level loop — do not weaken this to `>=` or drop it, since a past timestamp is not actionable and the caller needs to know to fall back to `fallback_backoff` instead of scheduling a retry in the past.
- **A wrong reset time is worse than no reset time.** Too early and the retry loop hammers the account again before the real reset, risking a harder/longer lockout; too late and the whole system idles unnecessarily long. This asymmetry is why each sub-parser is deliberately conservative:
  - `_from_epoch` requires a literal `|` immediately before 9-11 digits (matching the exact CLI error format) specifically to avoid false-positive matches on unrelated numeric sequences.
  - `_from_clock` rejects out-of-range hour (not 1-12) or minute (>59) by returning `None` rather than silently clamping — a silently-clamped bogus time would be a wrong time.
  - If you add a new parsing strategy, follow the same rule: return `None` on any ambiguity rather than guessing.
- `_from_clock` assumes the next occurrence of that clock time is the correct interpretation (rolls forward one day if `candidate <= now`). It has no timezone info of its own and works in whatever tzinfo `now` carries — pass `now` in the timezone the CLI's clock-time strings are meant to be interpreted in.
- `fallback_backoff` output must never regress even if `consecutive` overflows the step count — the `min(max(consecutive, 0), len(_BACKOFF_STEPS) - 1)` clamp keeps it at the largest defined step (1 hour) rather than indexing out of range or growing unboundedly.

### Testing Requirements
- `tests/quota/test_reset.py` covers this file.
- Run with `.venv/bin/pytest tests/quota` — no Docker needed (pure functions, no DB).

### Common Patterns
- Regexes are pre-compiled at module scope (`_EPOCH`, `_ISO`, `_CLOCK`) rather than compiled per call.
- Every public function takes `now` as a keyword-only parameter (`*, now: datetime`) — follow this signature convention for any new function here so callers can't accidentally pass it positionally and mix it up with other args.

## Dependencies

### Internal
- Imports nothing from `aicom`. Its output (`datetime | None`) is expected to be written into `aicom.store.system_state.set_pause` by the caller (worker/scheduler layer), but this module has no direct dependency on `store`.

### External
- Python standard library only (`re`, `datetime`). No third-party dependency.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
