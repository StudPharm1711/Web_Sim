from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '05d7fb4a2cb3'
down_revision = '6bad7e07c614'
branch_labels = None
depends_on = None

def upgrade():
    # Only add the column if it doesn’t exist.
    # If your DB already has it, this won't break anything as long as your local environment is "stamped".
    op.add_column('subscribers', sa.Column('stored_payment_method_id', sa.Integer()))

def downgrade():
    op.drop_column('subscribers', 'stored_payment_method_id')
