import json
import uuid
from dataclasses import dataclass, field

from aicom.domain.enums import ApprovalKind
from aicom.domain.views import ApprovalView
from aicom.notify.slack import SlackNotifier


@dataclass
class _StubSlackClient:
    """Records every chat_postMessage call and replies with fake channel/ts."""

    calls: list[dict] = field(default_factory=list)
    _counter: int = 0

    def chat_postMessage(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        self._counter += 1
        channel = kwargs.get("channel")
        return {"channel": channel, "ts": f"{self._counter}.000"}


def _view(
    kind: ApprovalKind = ApprovalKind.SPEND,
    slack_channel: str | None = None,
    slack_ts: str | None = None,
) -> ApprovalView:
    return ApprovalView(
        approval_id=uuid.uuid4(),
        nonce=f"nonce-{uuid.uuid4()}",
        kind=kind,
        agent_name="researcher",
        task_title="Buy market data",
        proposal="Buy feed X for $20/mo.",
        payload={"amount_usd": 20},
        slack_channel=slack_channel,
        slack_ts=slack_ts,
    )


def test_send_approval_request_returns_channel_and_ts_the_client_replied_with() -> None:
    client = _StubSlackClient()
    notifier = SlackNotifier(client=client, channel="C_TARGET")

    ref = notifier.send_approval_request(_view())

    assert ref.channel == "C_TARGET"
    assert ref.ts == "1.000"


def test_stage_one_reminder_threads_onto_the_view_persisted_dispatch_ref() -> None:
    client = _StubSlackClient()
    notifier = SlackNotifier(client=client, channel="C_TARGET")
    view = _view(slack_channel="C_ORIGINAL", slack_ts="1699999999.000100")

    notifier.send_reminder(view, stage=1)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["channel"] == "C_ORIGINAL"
    assert call["thread_ts"] == "1699999999.000100"


def test_stage_one_reminder_with_no_persisted_ref_posts_top_level_without_raising() -> None:
    client = _StubSlackClient()
    notifier = SlackNotifier(client=client, channel="C_TARGET")
    view = _view(slack_channel=None, slack_ts=None)

    notifier.send_reminder(view, stage=1)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["channel"] == "C_TARGET"
    assert "thread_ts" not in call


def test_batch_request_of_25_views_never_exceeds_50_blocks_per_message() -> None:
    client = _StubSlackClient()
    notifier = SlackNotifier(client=client, channel="C_TARGET")
    views = [_view() for _ in range(25)]

    notifier.send_batch_approval_request(views)

    assert len(client.calls) == 3  # 10 + 10 + 5
    for call in client.calls:
        assert len(call["blocks"]) <= 50


def test_batch_request_returns_dispatch_ref_of_first_message() -> None:
    client = _StubSlackClient()
    notifier = SlackNotifier(client=client, channel="C_TARGET")
    views = [_view() for _ in range(15)]

    ref = notifier.send_batch_approval_request(views)

    assert ref.channel == "C_TARGET"
    assert ref.ts == "1.000"


def test_chunked_batch_approve_all_only_covers_its_own_chunk_nonces() -> None:
    client = _StubSlackClient()
    notifier = SlackNotifier(client=client, channel="C_TARGET")
    views = [_view() for _ in range(15)]

    notifier.send_batch_approval_request(views)

    assert len(client.calls) == 2
    first_chunk_nonces = {v.nonce for v in views[:10]}
    second_chunk_nonces = {v.nonce for v in views[10:]}

    for call, expected_nonces in zip(
        client.calls, [first_chunk_nonces, second_chunk_nonces], strict=True
    ):
        approve_all = next(
            el
            for block in call["blocks"]
            if block["type"] == "actions"
            for el in block["elements"]
            if el["action_id"] == "approve_all"
        )
        payload = json.loads(approve_all["value"])
        assert set(payload["nonces"]) == expected_nonces

        # Per-item single buttons in this message must carry only single
        # nonces from this chunk, never the whole batch's nonces.
        single_nonces = {
            json.loads(el["value"])["nonce"]
            for block in call["blocks"]
            if block["type"] == "actions"
            for el in block["elements"]
            if el["action_id"] == "approve"
        }
        assert single_nonces == expected_nonces
