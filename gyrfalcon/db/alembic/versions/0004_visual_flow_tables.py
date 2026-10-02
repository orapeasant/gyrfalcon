"""Create visual flow design and runtime tables without retiring legacy data."""

from alembic import op

revision = "0004_visual_flow_tables"
down_revision = "0003_agent_flow_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE TABLE fnd_flow_dt_definitions (\n\tid TEXT NOT NULL, \n\ttenant_id TEXT NOT NULL, \n\tuser_id TEXT NOT NULL, \n\tname TEXT NOT NULL, \n\tdraft JSONB NOT NULL, \n\tcreated_at FLOAT(53) NOT NULL, \n\tupdated_at FLOAT(53) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, id), \n\tUNIQUE (tenant_id, name)\n)')
    op.execute('CREATE TABLE fnd_flow_dt_definition_versions (\n\ttenant_id TEXT NOT NULL, \n\tdefinition_id TEXT NOT NULL, \n\tversion BIGINT NOT NULL, \n\tgraph JSONB NOT NULL, \n\tattribute_snapshot JSONB NOT NULL, \n\tnotification_snapshot JSONB NOT NULL, \n\tpublished_at FLOAT(53) NOT NULL, \n\tpublished_by TEXT NOT NULL, \n\tPRIMARY KEY (tenant_id, definition_id, version), \n\tFOREIGN KEY(tenant_id, definition_id) REFERENCES fnd_flow_dt_definitions (tenant_id, id)\n)')
    op.execute('CREATE TABLE fnd_flow_dt_attrs (\n\ttenant_id TEXT NOT NULL, \n\tdefinition_id TEXT NOT NULL, \n\tscope_path TEXT NOT NULL, \n\tattribute_id TEXT NOT NULL, \n\tname TEXT NOT NULL, \n\tvalue_type TEXT NOT NULL, \n\tdefault_value JSONB, \n\tcandidates JSONB, \n\tPRIMARY KEY (tenant_id, definition_id, scope_path, attribute_id), \n\tUNIQUE (tenant_id, definition_id, scope_path, name), \n\tFOREIGN KEY(tenant_id, definition_id) REFERENCES fnd_flow_dt_definitions (tenant_id, id)\n)')
    op.execute("CREATE TABLE fnd_flow_dt_notif (\n\tid TEXT NOT NULL, \n\ttenant_id TEXT NOT NULL, \n\tuser_id TEXT NOT NULL, \n\tname TEXT NOT NULL, \n\tchannel TEXT NOT NULL, \n\trecipient_kind TEXT NOT NULL, \n\trecipient_ref TEXT NOT NULL, \n\tagent_id TEXT, \n\tsubject_template TEXT, \n\tbody_template TEXT, \n\tconfig JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tcreated_at FLOAT(53) NOT NULL, \n\tupdated_at FLOAT(53) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, id), \n\tUNIQUE (tenant_id, name)\n)")
    op.execute("CREATE TABLE fnd_flow_dt_deployments (\n\tid TEXT NOT NULL, \n\ttenant_id TEXT NOT NULL, \n\tuser_id TEXT NOT NULL, \n\tname TEXT NOT NULL, \n\tshort_name TEXT NOT NULL, \n\tdefinition_id TEXT NOT NULL, \n\tversion BIGINT NOT NULL, \n\tschedule TEXT, \n\tinput_schema JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tparameters JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tallowed_service_account_ids JSONB DEFAULT '[]'::jsonb NOT NULL, \n\tpaused SMALLINT DEFAULT 0 NOT NULL, \n\tnext_run_at FLOAT(53), \n\tcreated_at FLOAT(53) NOT NULL, \n\tupdated_at FLOAT(53) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, id), \n\tUNIQUE (tenant_id, name), \n\tUNIQUE (tenant_id, short_name), \n\tCHECK (short_name ~ '^[a-z0-9][a-z0-9_-]*$'), \n\tFOREIGN KEY(tenant_id, definition_id, version) REFERENCES fnd_flow_dt_definition_versions (tenant_id, definition_id, version)\n)")
    op.execute('CREATE INDEX idx_flow_dt_deployments_schedule ON fnd_flow_dt_deployments (paused, next_run_at)')
    op.execute("CREATE TABLE fnd_flow_rt_statuses (\n\tid TEXT NOT NULL, \n\ttenant_id TEXT NOT NULL, \n\tenvironment_id TEXT DEFAULT 'default' NOT NULL, \n\tuser_id TEXT NOT NULL, \n\tdefinition_id TEXT NOT NULL, \n\tversion BIGINT NOT NULL, \n\tdeployment_id TEXT, \n\ttrigger_kind TEXT NOT NULL, \n\ttrigger_ref TEXT, \n\tcaller_key TEXT, \n\tinput_hash TEXT, \n\tparameters JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tstate TEXT NOT NULL, \n\tcontext JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tloop_state JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tcurrent_node_path TEXT, \n\tresult JSONB, \n\terror JSONB, \n\tlease_owner TEXT, \n\tlease_until FLOAT(53), \n\tcreated_at FLOAT(53) NOT NULL, \n\tstarted_at FLOAT(53), \n\tupdated_at FLOAT(53) NOT NULL, \n\tfinished_at FLOAT(53), \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, environment_id, id), \n\tUNIQUE (tenant_id, caller_key), \n\tFOREIGN KEY(tenant_id, definition_id, version) REFERENCES fnd_flow_dt_definition_versions (tenant_id, definition_id, version), \n\tFOREIGN KEY(tenant_id, deployment_id) REFERENCES fnd_flow_dt_deployments (tenant_id, id)\n)")
    op.execute('CREATE INDEX idx_flow_rt_statuses_definition ON fnd_flow_rt_statuses (tenant_id, definition_id, created_at DESC)')
    op.execute('CREATE INDEX idx_flow_rt_statuses_lease ON fnd_flow_rt_statuses (state, lease_until)')
    op.execute('CREATE INDEX idx_flow_rt_statuses_state ON fnd_flow_rt_statuses (tenant_id, environment_id, state, created_at DESC)')
    op.execute("CREATE TABLE fnd_flow_rt_node_statuses (\n\tid TEXT NOT NULL, \n\ttenant_id TEXT NOT NULL, \n\tenvironment_id TEXT DEFAULT 'default' NOT NULL, \n\trun_id TEXT NOT NULL, \n\tnode_path TEXT NOT NULL, \n\tnode_id TEXT NOT NULL, \n\tnode_kind TEXT NOT NULL, \n\tvisit_seq BIGINT NOT NULL, \n\tattempt BIGINT DEFAULT 1 NOT NULL, \n\tstate TEXT NOT NULL, \n\tinput_value JSONB, \n\toutput_value JSONB, \n\ttransient TEXT, \n\terror JSONB, \n\twait_deadline FLOAT(53), \n\ttimeout_transient TEXT, \n\tresponse_value JSONB, \n\tresponse_user_id TEXT, \n\tai_session_id TEXT, \n\tstarted_at FLOAT(53) NOT NULL, \n\tupdated_at FLOAT(53) NOT NULL, \n\tfinished_at FLOAT(53), \n\tPRIMARY KEY (id), \n\tUNIQUE (tenant_id, environment_id, id), \n\tUNIQUE (tenant_id, environment_id, run_id, visit_seq), \n\tFOREIGN KEY(tenant_id, environment_id, run_id) REFERENCES fnd_flow_rt_statuses (tenant_id, environment_id, id), \n\tFOREIGN KEY(tenant_id, environment_id, ai_session_id) REFERENCES ai_sessions (tenant_id, environment_id, id)\n)")
    op.execute('CREATE INDEX idx_flow_rt_nodes_path ON fnd_flow_rt_node_statuses (tenant_id, environment_id, run_id, node_path, visit_seq DESC)')
    op.execute('CREATE INDEX idx_flow_rt_nodes_state ON fnd_flow_rt_node_statuses (tenant_id, environment_id, state, updated_at DESC)')
    op.execute("CREATE TABLE fnd_flow_rt_events (\n\tid TEXT NOT NULL, \n\ttenant_id TEXT NOT NULL, \n\tenvironment_id TEXT DEFAULT 'default' NOT NULL, \n\trun_id TEXT NOT NULL, \n\tvisit_id TEXT, \n\tevent_type TEXT NOT NULL, \n\tpayload JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tcreated_at FLOAT(53) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(tenant_id, environment_id, run_id) REFERENCES fnd_flow_rt_statuses (tenant_id, environment_id, id), \n\tFOREIGN KEY(tenant_id, environment_id, visit_id) REFERENCES fnd_flow_rt_node_statuses (tenant_id, environment_id, id)\n)")
    op.execute('CREATE INDEX idx_flow_rt_events_run ON fnd_flow_rt_events (tenant_id, environment_id, run_id, created_at)')
    op.execute("CREATE TABLE fnd_flow_rt_attrs (\n\ttenant_id TEXT NOT NULL, \n\tenvironment_id TEXT DEFAULT 'default' NOT NULL, \n\trun_id TEXT NOT NULL, \n\tscope_path TEXT NOT NULL, \n\tname TEXT NOT NULL, \n\tvalue JSONB, \n\tupdated_at FLOAT(53) NOT NULL, \n\tPRIMARY KEY (tenant_id, environment_id, run_id, scope_path, name), \n\tFOREIGN KEY(tenant_id, environment_id, run_id) REFERENCES fnd_flow_rt_statuses (tenant_id, environment_id, id)\n)")
    op.execute('CREATE UNIQUE INDEX uq_session_messages_scope_id ON ai_session_messages (tenant_id, environment_id, id)')
    op.execute("CREATE TABLE fnd_flow_rt_notif (\n\tid TEXT NOT NULL, \n\ttenant_id TEXT NOT NULL, \n\tenvironment_id TEXT DEFAULT 'default' NOT NULL, \n\trun_id TEXT NOT NULL, \n\tvisit_id TEXT NOT NULL, \n\tmessage_id TEXT NOT NULL, \n\trecipient_kind TEXT NOT NULL, \n\trecipient_ref TEXT NOT NULL, \n\tchannel TEXT NOT NULL, \n\tattempt BIGINT DEFAULT 1 NOT NULL, \n\tstate TEXT NOT NULL, \n\tprovider_id TEXT, \n\terror JSONB, \n\tcreated_at FLOAT(53) NOT NULL, \n\tupdated_at FLOAT(53) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(tenant_id, environment_id, run_id) REFERENCES fnd_flow_rt_statuses (tenant_id, environment_id, id), \n\tFOREIGN KEY(tenant_id, environment_id, visit_id) REFERENCES fnd_flow_rt_node_statuses (tenant_id, environment_id, id), \n\tFOREIGN KEY(tenant_id, environment_id, message_id) REFERENCES ai_session_messages (tenant_id, environment_id, id), \n\tUNIQUE (tenant_id, environment_id, visit_id, message_id, recipient_kind, recipient_ref, channel, attempt)\n)")
    op.execute('CREATE INDEX idx_flow_rt_notif_visit ON fnd_flow_rt_notif (tenant_id, environment_id, visit_id, state)')
    op.create_foreign_key("fk_ai_sessions_flow_run_status", "ai_sessions", "fnd_flow_rt_statuses",
                          ["tenant_id", "environment_id", "flow_run_status_id"],
                          ["tenant_id", "environment_id", "id"])
    op.create_foreign_key("fk_ai_sessions_flow_node_status", "ai_sessions", "fnd_flow_rt_node_statuses",
                          ["tenant_id", "environment_id", "flow_node_status_id"],
                          ["tenant_id", "environment_id", "id"])
    op.create_foreign_key("fk_ai_session_messages_flow_node_status", "ai_session_messages",
                          "fnd_flow_rt_node_statuses",
                          ["tenant_id", "environment_id", "flow_node_status_id"],
                          ["tenant_id", "environment_id", "id"])


def downgrade() -> None:
    op.drop_constraint("fk_ai_session_messages_flow_node_status", "ai_session_messages", type_="foreignkey")
    op.drop_constraint("fk_ai_sessions_flow_node_status", "ai_sessions", type_="foreignkey")
    op.drop_constraint("fk_ai_sessions_flow_run_status", "ai_sessions", type_="foreignkey")
    op.drop_table("fnd_flow_rt_notif")
    op.drop_index("uq_session_messages_scope_id", table_name="ai_session_messages")
    op.drop_table("fnd_flow_rt_attrs")
    op.drop_table("fnd_flow_rt_events")
    op.drop_table("fnd_flow_rt_node_statuses")
    op.drop_table("fnd_flow_rt_statuses")
    op.drop_table("fnd_flow_dt_deployments")
    op.drop_table("fnd_flow_dt_notif")
    op.drop_table("fnd_flow_dt_attrs")
    op.drop_table("fnd_flow_dt_definition_versions")
    op.drop_table("fnd_flow_dt_definitions")
