"""merge heads

Revision ID: bc405ef4d867
Revises: b6d1c8f2a4a7, bf97b463eb54
Create Date: 2026-06-07 15:37:29.542492

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bc405ef4d867'
down_revision: Union[str, Sequence[str], None] = ('b6d1c8f2a4a7', 'bf97b463eb54')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
