from collections.abc import Sequence

from slack_sdk import WebClient

from aicom.domain.views import ApprovalView, DispatchRef, RunReport
from aicom.notify.blocks import approval_blocks, batch_approval_blocks, run_report_blocks

_STAGE_CHANNEL = {1: "thread", 2: "dm", 3: "digest"}

# Slack rejects messages over 50 blocks. batch_approval_blocks emits 4 blocks
# per approval plus 2 fixed blocks (header + trailing approve-all), so a
# chunk of 10 approvals tops out at 42 blocks — safely under the limit.
_BATCH_CHUNK_SIZE = 10


class SlackNotifier:
    def __init__(self, client: WebClient, channel: str, dm_user_id: str = "") -> None:
        self._client = client
        self._channel = channel
        self._dm_user_id = dm_user_id

    def send_approval_request(self, view: ApprovalView) -> DispatchRef:
        response = self._client.chat_postMessage(
            channel=self._channel,
            blocks=approval_blocks(view),
            text=f"Sign-off needed: {view.kind.value} — {view.task_title}",
        )
        return DispatchRef(channel=response["channel"], ts=response["ts"])

    def send_batch_approval_request(self, views: Sequence[ApprovalView]) -> DispatchRef:
        views = list(views)
        chunks = [
            views[i : i + _BATCH_CHUNK_SIZE] for i in range(0, len(views), _BATCH_CHUNK_SIZE)
        ] or [[]]

        first_ref: DispatchRef | None = None
        for chunk in chunks:
            response = self._client.chat_postMessage(
                channel=self._channel,
                blocks=batch_approval_blocks(chunk),
                text=f"{len(chunk)} items awaiting sign-off",
            )
            ref = DispatchRef(channel=response["channel"], ts=response["ts"])
            if first_ref is None:
                first_ref = ref
        assert first_ref is not None
        return first_ref

    def send_reminder(self, view: ApprovalView, stage: int) -> None:
        target = _STAGE_CHANNEL.get(stage, "digest")
        text = f"Reminder ({stage}): {view.kind.value} — {view.task_title} still awaiting sign-off"
        if target == "thread":
            if view.slack_channel is not None and view.slack_ts is not None:
                self._client.chat_postMessage(
                    channel=view.slack_channel, thread_ts=view.slack_ts, text=text
                )
            else:
                # No persisted ref to thread onto — fall back to a normal
                # channel post rather than silently dropping the reminder.
                self._client.chat_postMessage(channel=self._channel, text=text)
        elif target == "dm" and self._dm_user_id:
            self._client.chat_postMessage(channel=self._dm_user_id, text=text)
        else:
            self._client.chat_postMessage(channel=self._channel, text=text)

    def send_run_report(self, report: RunReport) -> None:
        self._client.chat_postMessage(
            channel=self._channel,
            blocks=run_report_blocks(report),
            text=f"{report.task_title} → {report.status.value}",
        )

    def send_system_notice(self, text: str) -> None:
        self._client.chat_postMessage(channel=self._channel, text=text)
