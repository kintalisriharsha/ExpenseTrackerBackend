"""merge heads

Revision ID: d311f0e78bee
Revises: bc405ef4d867
Create Date: 2026-06-07 15:37:37.853259

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd311f0e78bee'
down_revision: Union[str, Sequence[str], None] = 'bc405ef4d867'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
