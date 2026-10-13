"""re-clean double-escaped descriptions

Data only. Descriptions stored before html_to_text handled double-escaped HTML still hold
literal tags ("<div><p>..."), and a job already stored is never re-mapped on a later fetch.
Running html_to_text over the stored text once more cleans them. Not reversible.

Revision ID: f680c1d27195
Revises: a3a84e7eab32
Create Date: 2026-10-12 19:04:01.092348

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from job_scout.fetch.normalize import html_to_text, looks_double_escaped

# revision identifiers, used by Alembic.
revision: str = "f680c1d27195"
down_revision: str | Sequence[str] | None = "a3a84e7eab32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, description_text FROM jobs WHERE description_text LIKE '%<%>%'")
    ).all()
    for job_id, text in rows:
        if looks_double_escaped(text):
            conn.execute(
                sa.text("UPDATE jobs SET description_text = :text WHERE id = :id"),
                {"text": html_to_text(text), "id": job_id},
            )


def downgrade() -> None:
    pass
