"""jobs unavailable_at

Revision ID: 3b8e1d5a9c27
Revises: f9c70c31f5c9
Create Date: 2026-11-12 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import job_scout.db.types

# revision identifiers, used by Alembic.
revision: str = "3b8e1d5a9c27"
down_revision: str | Sequence[str] | None = "f9c70c31f5c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "unavailable_at", job_scout.db.types.UTCDateTime(timezone=True), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("unavailable_at")
