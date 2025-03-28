"""Merging migration heads

Revision ID: 6bad7e07c614
Revises: 48095d40a142, 62a36057a543
Create Date: 2025-03-28 11:39:35.613123

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6bad7e07c614'
down_revision = ('48095d40a142', '62a36057a543')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
