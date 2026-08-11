from collections.abc import Sequence
from dataclasses import dataclass, field

from aicom.domain.views import ApprovalView, DispatchRef, RunReport


@dataclass(slots=True)
class FakeNotifier:
    approvals: list[ApprovalView] = field(default_factory=list)
    batches: list[list[ApprovalView]] = field(default_factory=list)
    reminders: list[tuple[ApprovalView, int]] = field(default_factory=list)
    reports: list[RunReport] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    _counter: int = 0

    def send_approval_request(self, view: ApprovalView) -> DispatchRef:
        self.approvals.append(view)
        self._counter += 1
        return DispatchRef(channel="C1", ts=f"{self._counter}.000")

    def send_batch_approval_request(self, views: Sequence[ApprovalView]) -> DispatchRef:
        self.batches.append(list(views))
        self._counter += 1
        return DispatchRef(channel="C1", ts=f"{self._counter}.000")

    def send_reminder(self, view: ApprovalView, stage: int) -> None:
        self.reminders.append((view, stage))

    def send_run_report(self, report: RunReport) -> None:
        self.reports.append(report)

    def send_system_notice(self, text: str) -> None:
        self.notices.append(text)
