"""null job_field_confidence for general rows

Revision ID: dc10b8d5784c
Revises: 3a969820e549
Create Date: 2026-08-13 10:29:45.908491

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'dc10b8d5784c'
down_revision = '3a969820e549'
branch_labels = None
depends_on = None


def upgrade():
    # Old rows can't distinguish predicted 'general' from substituted, so drop the
    # score for both rather than keep one that may not belong to the label beside it.
    op.execute(
        "UPDATE analysis_results "
        "SET job_field_confidence = NULL "
        "WHERE job_field = 'general'"
    )


def downgrade():
    # No-op: the discarded margins were never stored anywhere else.
    pass
