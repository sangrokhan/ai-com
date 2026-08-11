import json
from collections.abc import Sequence

from aicom.domain.views import ApprovalView, RunReport


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150]}}


def _section(markdown: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": markdown[:2900]}}


def _decision_buttons(nonce: str) -> dict:
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "approve",
                "style": "primary",
                "text": {"type": "plain_text", "text": "Approve"},
                "value": json.dumps({"nonce": nonce}),
            },
            {
                "type": "button",
                "action_id": "reject",
                "style": "danger",
                "text": {"type": "plain_text", "text": "Reject"},
                "value": json.dumps({"nonce": nonce}),
            },
        ],
    }


def approval_blocks(view: ApprovalView) -> list[dict]:
    return [
        _header(f"Sign-off needed: {view.kind.value}"),
        _section(f"*{view.agent_name}* · _{view.task_title}_"),
        _section(view.proposal),
        _section(f"```{json.dumps(view.payload, indent=2, ensure_ascii=False)}```"),
        _decision_buttons(view.nonce),
    ]


def batch_approval_blocks(views: Sequence[ApprovalView]) -> list[dict]:
    blocks: list[dict] = [_header(f"{len(views)} items awaiting sign-off")]
    for view in views:
        blocks.append(_section(f"*{view.kind.value}* · {view.agent_name} · _{view.task_title}_"))
        blocks.append(_section(view.proposal))
        blocks.append(_decision_buttons(view.nonce))
        blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve_all",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve all"},
                    "value": json.dumps({"nonces": [v.nonce for v in views]}),
                }
            ],
        }
    )
    return blocks


def run_report_blocks(report: RunReport) -> list[dict]:
    cost = f" · ${report.cost_usd:.2f}" if report.cost_usd is not None else ""
    return [
        _section(f"*{report.agent_name}* · _{report.task_title}_ → `{report.status.value}`{cost}"),
        _section(report.summary),
    ]
