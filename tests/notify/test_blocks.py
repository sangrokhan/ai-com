import json
import uuid

from aicom.domain.enums import ApprovalKind
from aicom.domain.views import ApprovalView
from aicom.notify.blocks import approval_blocks, batch_approval_blocks


def _view(kind: ApprovalKind = ApprovalKind.SPEND) -> ApprovalView:
    return ApprovalView(
        approval_id=uuid.uuid4(),
        nonce="nonce-abc",
        kind=kind,
        agent_name="researcher",
        task_title="Buy market data",
        proposal="Buy feed X for $20/mo. Alternative: free tier. Reversible: yes.",
        payload={"amount_usd": 20},
    )


def test_approval_blocks_carry_nonce_in_both_buttons() -> None:
    view = _view()
    blocks = approval_blocks(view)
    actions = next(b for b in blocks if b["type"] == "actions")

    values = {json.loads(el["value"])["nonce"] for el in actions["elements"]}
    action_ids = {el["action_id"] for el in actions["elements"]}
    assert values == {"nonce-abc"}
    assert action_ids == {"approve", "reject"}


def test_approval_blocks_show_kind_agent_task_and_proposal() -> None:
    blocks = approval_blocks(_view())
    text = json.dumps(blocks)
    assert "spend" in text
    assert "researcher" in text
    assert "Buy market data" in text
    assert "Alternative: free tier" in text


def test_batch_blocks_hold_one_button_pair_per_item_plus_approve_all() -> None:
    views = [_view(), _view(ApprovalKind.PUBLISH)]
    blocks = batch_approval_blocks(views)
    text = json.dumps(blocks)

    assert text.count('"action_id": "approve"') == 2
    assert '"action_id": "approve_all"' in text
    payload = json.loads(
        next(
            b
            for b in blocks
            if b["type"] == "actions"
            and any(el["action_id"] == "approve_all" for el in b["elements"])
        )["elements"][0]["value"]
    )
    assert set(payload["nonces"]) == {v.nonce for v in views}
