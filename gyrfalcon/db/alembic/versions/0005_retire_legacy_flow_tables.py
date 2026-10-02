"""Retire the previous code-defined flow runtime and design tables."""

from alembic import op
import sqlalchemy as sa


revision = "0005_retire_legacy_flow_tables"
down_revision = "0004_visual_flow_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These tables belonged to the retired @flow design and have no data
    # migration: visual flow definitions and runs now live in fnd_flow_dt_*
    # and fnd_flow_rt_*.
    for table in (
        "fnd_flow_attrs",
        "fnd_flow_definition_versions",
        "fnd_flow_definitions",
        "fnd_flow_run_edges",
        "fnd_flow_run_states",
        "fnd_flow_runs",
        "fnd_flow_events",
        "fnd_flow_deployments",
    ):
        if sa.inspect(op.get_bind()).has_table(table):
            op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError("Legacy flow tables are retired and are not restored")
