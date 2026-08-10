import json

from aicom.executor.stream import StreamParser


def test_parses_lines_and_assigns_sequence_numbers() -> None:
    parser = StreamParser()
    first = parser.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}))
    second = parser.feed(json.dumps({"type": "assistant", "message": {"content": []}}))

    assert first is not None and first.seq == 0 and first.type == "system"
    assert second is not None and second.seq == 1
    assert parser.session_id == "s-1"


def test_blank_and_malformed_lines_do_not_raise() -> None:
    parser = StreamParser()
    assert parser.feed("") is None
    assert parser.feed("   ") is None

    event = parser.feed("{not json")
    assert event is not None
    assert event.type == "unparsed"
    assert event.payload["raw"] == "{not json"


def test_accumulates_cost_and_tokens_from_result_event() -> None:
    parser = StreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 0.42,
                "usage": {"input_tokens": 1200, "output_tokens": 300},
            }
        )
    )
    assert parser.cost_usd == 0.42
    assert parser.token_in == 1200
    assert parser.token_out == 300


def test_detects_usage_limit_from_result_error() -> None:
    parser = StreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "Claude AI usage limit reached|1786000000",
            }
        )
    )
    assert parser.saw_usage_limit is True
    assert parser.usage_limit_text == "Claude AI usage limit reached|1786000000"


def test_ordinary_error_is_not_a_usage_limit() -> None:
    parser = StreamParser()
    parser.feed(json.dumps({"type": "result", "is_error": True, "result": "tool failed"}))
    assert parser.saw_usage_limit is False
