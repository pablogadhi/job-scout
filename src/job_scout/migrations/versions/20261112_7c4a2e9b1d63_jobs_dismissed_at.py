"""jobs dismissed_at

Revision ID: 7c4a2e9b1d63
Revises: 3b8e1d5a9c27
Create Date: 2026-11-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import job_scout.db.types

# revision identifiers, used by Alembic.
revision: str = "7c4a2e9b1d63"
down_revision: str | Sequence[str] | None = "3b8e1d5a9c27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("dismissed_at", job_scout.db.types.UTCDateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("dismissed_at")
