import uuid
from collections.abc import Sequence

from slack_sdk import WebClient

from aicom.domain.views import ApprovalView, DispatchRef, RunReport
from aicom.notify.blocks import approval_blocks, batch_approval_blocks, run_report_blocks

_STAGE_CHANNEL = {1: "thread", 2: "dm", 3: "digest"}


class SlackNotifier:
    def __init__(self, client: WebClient, channel: str, dm_user_id: str = "") -> None:
        self._client = client
        self._channel = channel
        self._dm_user_id = dm_user_id
        # Tracks where each approval's original message landed, so stage-1
        # reminders can thread onto it instead of posting a fresh top-level
        # message. Keyed by approval_id since send_reminder only receives an
        # ApprovalView, not the DispatchRef returned at send time.
        self._dispatch_by_approval: dict[uuid.UUID, DispatchRef] = {}

    def send_approval_request(self, view: ApprovalView) -> DispatchRef:
        response = self._client.chat_postMessage(
            channel=self._channel,
            blocks=approval_blocks(view),
            text=f"Sign-off needed: {view.kind.value} — {view.task_title}",
        )
        ref = DispatchRef(channel=response["channel"], ts=response["ts"])
        self._dispatch_by_approval[view.approval_id] = ref
        return ref

    def send_batch_approval_request(self, views: Sequence[ApprovalView]) -> DispatchRef:
        response = self._client.chat_postMessage(
            channel=self._channel,
            blocks=batch_approval_blocks(views),
            text=f"{len(views)} items awaiting sign-off",
        )
        ref = DispatchRef(channel=response["channel"], ts=response["ts"])
        for view in views:
            self._dispatch_by_approval[view.approval_id] = ref
        return ref

    def send_reminder(self, view: ApprovalView, stage: int) -> None:
        target = _STAGE_CHANNEL.get(stage, "digest")
        text = f"Reminder ({stage}): {view.kind.value} — {view.task_title} still awaiting sign-off"
        if target == "thread":
            ref = self._dispatch_by_approval.get(view.approval_id)
            if ref is not None:
                self._client.chat_postMessage(
                    channel=ref.channel, thread_ts=ref.ts, text=text
                )
            else:
                # No known original message to thread onto — fall back to a
                # normal channel post rather than silently dropping the
                # reminder.
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
