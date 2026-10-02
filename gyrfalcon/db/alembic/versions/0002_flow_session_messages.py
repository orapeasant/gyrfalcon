"""Allow session history to hold flow agent turns and notification content."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0002_flow_session_messages"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_session_messages", sa.Column("flow_node_status_id", sa.Text()))
    op.add_column("ai_session_messages", sa.Column(
        "message_kind", sa.Text(), nullable=False,
        server_default=sa.text("'conversation'"),
    ))
    op.add_column("ai_session_messages", sa.Column("channel", sa.Text()))
    op.add_column("ai_session_messages", sa.Column("direction", sa.Text()))
    op.add_column("ai_session_messages", sa.Column("subject", sa.Text()))
    op.add_column("ai_session_messages", sa.Column("sender", sa.Text()))
    op.add_column("ai_session_messages", sa.Column("recipients", JSONB()))
    op.add_column("ai_session_messages", sa.Column("body_html", sa.Text()))
    op.add_column("ai_session_messages", sa.Column("external_message_id", sa.Text()))
    op.add_column("ai_session_messages", sa.Column("in_reply_to", sa.Text()))
    op.add_column("ai_session_messages", sa.Column("headers", JSONB()))
    op.add_column("ai_session_messages", sa.Column("attachment_refs", JSONB()))
    op.create_index(
        "idx_session_messages_flow_node", "ai_session_messages",
        ["tenant_id", "environment_id", "flow_node_status_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("idx_session_messages_flow_node", table_name="ai_session_messages")
    for name in (
        "attachment_refs", "headers", "in_reply_to", "external_message_id",
        "body_html", "recipients", "sender", "subject", "direction",
        "channel", "message_kind", "flow_node_status_id",
    ):
        op.drop_column("ai_session_messages", name)
