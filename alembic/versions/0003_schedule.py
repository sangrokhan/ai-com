"""schedule entity

Revision ID: cf199e620a2f
Revises: b41c7f0a9e12
Create Date: 2026-08-11 11:23:23.986186

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cf199e620a2f'
down_revision: str | Sequence[str] | None = 'b41c7f0a9e12'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'schedule',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('agent_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('cron', sa.String(length=120), nullable=False),
        sa.Column('timezone', sa.String(length=64), nullable=False),
        sa.Column('title_template', sa.String(length=300), nullable=False),
        sa.Column('goal_template', sa.Text(), nullable=False),
        sa.Column('next_due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_fired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_skipped_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_skip_reason', sa.String(length=200), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['agent_id'], ['agent.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_id', 'name', name='uq_schedule_agent_name'),
    )
    op.create_index(op.f('ix_schedule_enabled'), 'schedule', ['enabled'], unique=False)
    op.create_index(op.f('ix_schedule_next_due_at'), 'schedule', ['next_due_at'], unique=False)
    op.add_column('task', sa.Column('schedule_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_task_schedule_id'), 'task', ['schedule_id'], unique=False)
    op.create_foreign_key(
        'fk_task_schedule_id_schedule',
        'task',
        'schedule',
        ['schedule_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.drop_column('task', 'schedule')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'task',
        sa.Column('schedule', sa.VARCHAR(length=120), autoincrement=False, nullable=True),
    )
    op.drop_constraint('fk_task_schedule_id_schedule', 'task', type_='foreignkey')
    op.drop_index(op.f('ix_task_schedule_id'), table_name='task')
    op.drop_column('task', 'schedule_id')
    op.drop_index(op.f('ix_schedule_next_due_at'), table_name='schedule')
    op.drop_index(op.f('ix_schedule_enabled'), table_name='schedule')
    op.drop_table('schedule')
