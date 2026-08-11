import json
from dataclasses import dataclass

_USAGE_LIMIT_MARKERS = ("usage limit reached", "rate limit", "limit exceeded")


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    seq: int
    type: str
    payload: dict


class StreamParser:
    """Turns raw stream-json lines into events. Never raises on bad input:
    a parser bug must not kill a run."""

    def __init__(self) -> None:
        self._seq = 0
        self._session_id: str | None = None
        self._cost_usd: float | None = None
        self._token_in = 0
        self._token_out = 0
        self._usage_limit_text: str | None = None

    def feed(self, line: str) -> ParsedEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise TypeError("not an object")
            type_ = str(payload.get("type", "unknown"))
        except Exception:  # noqa: BLE001 - broad catch is intentional; deeply nested JSON can raise RecursionError
            payload = {"raw": stripped}
            type_ = "unparsed"
        else:
            self._absorb(payload)
        event = ParsedEvent(seq=self._seq, type=type_, payload=payload)
        self._seq += 1
        return event

    def _absorb(self, payload: dict) -> None:
        if session_id := payload.get("session_id"):
            self._session_id = str(session_id)
        if payload.get("type") != "result":
            return
        if (cost := payload.get("total_cost_usd")) is not None:
            self._cost_usd = float(cost)
        usage = payload.get("usage") or {}
        self._token_in += int(usage.get("input_tokens", 0))
        self._token_out += int(usage.get("output_tokens", 0))
        if payload.get("is_error"):
            text = str(payload.get("result", ""))
            if any(marker in text.lower() for marker in _USAGE_LIMIT_MARKERS):
                self._usage_limit_text = text

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def cost_usd(self) -> float | None:
        return self._cost_usd

    @property
    def token_in(self) -> int:
        return self._token_in

    @property
    def token_out(self) -> int:
        return self._token_out

    @property
    def saw_usage_limit(self) -> bool:
        return self._usage_limit_text is not None

    @property
    def usage_limit_text(self) -> str | None:
        return self._usage_limit_text
