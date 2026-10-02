"""Link agent sessions to the flow run and node visit that started them."""

from alembic import op
import sqlalchemy as sa


revision = "0003_agent_flow_links"
down_revision = "0002_flow_session_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_sessions", sa.Column(
        "run_status", sa.Text(), nullable=False,
        server_default=sa.text("'created'"),
    ))
    op.add_column("ai_sessions", sa.Column("run_error", sa.Text()))
    op.add_column("ai_sessions", sa.Column("flow_run_status_id", sa.Text()))
    op.add_column("ai_sessions", sa.Column("flow_node_status_id", sa.Text()))
    op.create_index(
        "idx_sessions_flow_run", "ai_sessions",
        ["tenant_id", "environment_id", "flow_run_status_id"],
    )
    op.create_index(
        "uq_sessions_flow_node", "ai_sessions",
        ["tenant_id", "environment_id", "flow_node_status_id"], unique=True,
    )
    op.create_index(
        "idx_sessions_run_status", "ai_sessions",
        ["tenant_id", "environment_id", "run_status", sa.text("last_active DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_sessions_run_status", table_name="ai_sessions")
    op.drop_index("uq_sessions_flow_node", table_name="ai_sessions")
    op.drop_index("idx_sessions_flow_run", table_name="ai_sessions")
    op.drop_column("ai_sessions", "flow_node_status_id")
    op.drop_column("ai_sessions", "flow_run_status_id")
    op.drop_column("ai_sessions", "run_error")
    op.drop_column("ai_sessions", "run_status")
