"""add expense time column

Revision ID: b6d1c8f2a4a7
Revises: 310f09d739f8
Create Date: 2026-06-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6d1c8f2a4a7'
down_revision: Union[str, Sequence[str], None] = '310f09d739f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('expenses', sa.Column('time', sa.Time(), nullable=True))
    op.execute("UPDATE expenses SET time = CAST(date AS time)")
    op.alter_column('expenses', 'time', nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('expenses', 'time')