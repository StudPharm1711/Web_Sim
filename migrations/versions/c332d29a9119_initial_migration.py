"""Initial migration

Revision ID: c332d29a9119
Revises: 
Create Date: 2025-02-28 13:39:31.357349

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c332d29a9119'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=150), nullable=False),
    sa.Column('email', sa.String(length=150), nullable=False),
    sa.Column('password', sa.String(length=150), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=True),
    sa.Column('discipline', sa.String(length=100), nullable=True),
    sa.Column('stripe_customer_id', sa.String(length=100), nullable=True),
    sa.Column('subscription_id', sa.String(length=100), nullable=True),
    sa.Column('subscription_status', sa.String(length=50), nullable=True),
    sa.Column('is_admin', sa.Boolean(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('username')
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('user')
    # ### end Alembic commands ###
