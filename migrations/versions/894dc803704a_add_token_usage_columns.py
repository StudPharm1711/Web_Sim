"""Add token usage columns

Revision ID: 894dc803704a
Revises: e59fdca4f1be
Create Date: 2025-03-10 19:51:22.020668

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '894dc803704a'
down_revision = 'e59fdca4f1be'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('token_prompt_usage_gpt35', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('token_completion_usage_gpt35', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('token_prompt_usage_gpt4', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('token_completion_usage_gpt4', sa.Integer(), nullable=True))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('token_completion_usage_gpt4')
        batch_op.drop_column('token_prompt_usage_gpt4')
        batch_op.drop_column('token_completion_usage_gpt35')
        batch_op.drop_column('token_prompt_usage_gpt35')

    # ### end Alembic commands ###
