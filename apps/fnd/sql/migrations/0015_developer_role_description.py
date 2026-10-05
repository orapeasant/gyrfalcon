"""Clarify Developer role description now that it owns navigation tools."""

from alembic import op
import sqlalchemy as sa


revision = "0015_developer_role_description"
down_revision = "0014_developer_navigation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE fnd_auth_roles SET description = 'Developer access, including navigation configuration.' "
        "WHERE name = 'app_developer'"
    ))
    bind.execute(sa.text(
        "UPDATE fnd_auth_roles SET description = 'Administrator access.' "
        "WHERE name = 'system_admin'"
    ))


def downgrade() -> None:
    raise RuntimeError("Role descriptions are operator-managed and are not reverted.")
