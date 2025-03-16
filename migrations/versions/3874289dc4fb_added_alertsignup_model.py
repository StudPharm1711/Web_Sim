"""Added AlertSignup model

Revision ID: 3874289dc4fb
Revises: 4854407b1a66
Create Date: 2025-03-16 11:16:15.026112

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3874289dc4fb'
down_revision = '4854407b1a66'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('alert_signup',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=150), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('alert_signup')
    # ### end Alembic commands ###
