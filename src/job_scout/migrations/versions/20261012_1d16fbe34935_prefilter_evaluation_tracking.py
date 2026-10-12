"""prefilter evaluation tracking

Revision ID: 1d16fbe34935
Revises: 832d7dbee9d9
Create Date: 2026-10-12 13:24:41.285502

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import job_scout.db.types

# revision identifiers, used by Alembic.
revision: str = "1d16fbe34935"
down_revision: str | Sequence[str] | None = "832d7dbee9d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Old verdicts have no fingerprint and would be stale anyway; `job-scout filter`
    # recomputes them. Clearing them also lets the NOT NULL columns be added without defaults.
    op.execute("DELETE FROM prefilter_results")
    with op.batch_alter_table("prefilter_results", schema=None, recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("evaluated_at", job_scout.db.types.UTCDateTime(timezone=True), nullable=False)
        )
        batch_op.add_column(sa.Column("fingerprint", sa.String(length=64), nullable=False))
        batch_op.add_column(
            sa.Column("group_size", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.create_index(batch_op.f("ix_prefilter_results_passed"), ["passed"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("prefilter_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prefilter_results_passed"))
        batch_op.drop_column("group_size")
        batch_op.drop_column("fingerprint")
        batch_op.drop_column("evaluated_at")
