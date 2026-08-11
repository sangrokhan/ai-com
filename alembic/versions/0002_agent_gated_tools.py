"""add agent.gated_tools

Revision ID: b41c7f0a9e12
Revises: d7d59d6924a4
Create Date: 2026-08-11 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b41c7f0a9e12'
down_revision: str | Sequence[str] | None = 'd7d59d6924a4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default so existing agent rows get an empty list rather than NULL:
    # the worker treats gated_tools as a denylist and must never see NULL.
    op.add_column(
        'agent',
        sa.Column(
            'gated_tools',
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('agent', 'gated_tools')
