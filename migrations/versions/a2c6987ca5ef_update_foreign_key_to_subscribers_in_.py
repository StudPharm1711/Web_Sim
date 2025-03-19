"""Update foreign key to subscribers in Feedback

Revision ID: a2c6987ca5ef
Revises: 754a6553c673
Create Date: 2025-03-19 18:22:05.336215

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a2c6987ca5ef'
down_revision = '754a6553c673'
branch_labels = None
depends_on = None


def upgrade():
    # Rename the table "user" to "subscribers"
    op.rename_table('user', 'subscribers')

    # Update the foreign key in the "feedback" table to reference subscribers.id
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.drop_constraint('feedback_user_id_fkey', type_='foreignkey')
        batch_op.create_foreign_key(None, 'subscribers', ['user_id'], ['id'])


def downgrade():
    # Revert the foreign key change in "feedback"
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.drop_constraint(None, type_='foreignkey')
        batch_op.create_foreign_key('feedback_user_id_fkey', 'user', ['user_id'], ['id'])

    # Rename the table back from "subscribers" to "user"
    op.rename_table('subscribers', 'user')
