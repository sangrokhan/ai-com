"""add run.gate_error

Revision ID: 2c7a4c540d55
Revises: cf199e620a2f
Create Date: 2026-08-13 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2c7a4c540d55'
down_revision: str | Sequence[str] | None = 'cf199e620a2f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Stamped by the gate MCP subprocess (a separate process from the
    # worker) when it could not durably record an approval, so the worker
    # can surface that failure to the operator (Slack + logs) even if the
    # agent's run otherwise finishes SUCCEEDED. See aicom.gate.server and
    # aicom.store.runs.record_gate_error.
    op.add_column('run', sa.Column('gate_error', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('run', 'gate_error')
