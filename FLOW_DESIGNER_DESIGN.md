# Flow designer design

Status: agreed design based on the September 2026 discussion. The version 2 canvas and visual flow runtime are implemented in this repository. The numbered runtime specification is [15-flow.md](../gyrfalcon_docs/spec/gyrfalcon/15-flow.md), Part XXII.

## Saved-agent Chat sessions

- Running a saved Agent from the Agents page first asks for confirmation that Gyrfalcon will open Chat with a new session. The confirmation does not collect an initial prompt. Confirming creates an empty chat session associated with the saved agent and then navigates to that session.
- The user's first message starts the ordinary agentic loop. The session's `agent_id` causes Chat to build the agent from its configured instructions, model, skills, toolsets, plugins, and attached MCP servers, so the user can continue interacting with that agent in the same conversation. Chat labels the session as an Agent session.
- This is separate from an agent-backed Activity inside a flow. Flow Activity sessions remain linked to their run and node visit as described below; they are not regular dashboard Agent chat sessions.

## Editor

Use React Flow (`@xyflow/react`) for the graph surface. Its viewport supports pan and zoom, and its node, edge, and context menu APIs cover the requested interactions. References: [viewport](https://reactflow.dev/learn/concepts/the-viewport), [drag and drop](https://reactflow.dev/examples/interaction/drag-and-drop), [context menu](https://reactflow.dev/examples/interaction/context-menu), and [subflows](https://reactflow.dev/learn/layouting/sub-flows).

The editor has three columns:

- The opened flow uses the available viewport for the canvas. Its name follows Designer in the application breadcrumbs, with Back, Save, and Publish on the same bar. A separate flow ID and metadata row is omitted.
- Left: the entire palette and each section can collapse. Start, Activity, Notification, Process, and End are compact draggable icons; Notification uses a chat bubble. Hovering identifies the node type. Registered Python activities remain available in an expandable section with a code editor for user Python modules; one module can contain many decorated activities. Clicking the expandable flow attribute area selects the whole flow in the right inspector. Attributes cannot be dragged onto the canvas.
- Center: pannable, zoomable canvas with draggable icon-only nodes. Hovering a node identifies it; clicking opens its inspector on the right. The aerial view has an unlabeled drag handle and can be resized, minimized, closed, and reopened. Canvas right-click menus replace the browser menu. Right-clicking empty canvas offers Rearrange items, which places nodes in flow order and fits the diagram to the viewport. Dropping a node auto-connects it to the current path. Connections can subsequently be deleted, relinked, and edited. Canvas positions and viewport are saved with the draft.
- Dragging from a source handle onto a target node body creates a link using an unused declared transient. If every transient is already linked, the first transient's existing link is relinked to the dropped target. A node's outgoing links are listed in the right inspector with direct selection and delete controls.
- Right: inspector for the selected flow, node, or connection/transient. Clicking a Process, Activity, Start, or End loads its assigned attributes here. The inspector edits the selected object's properties, attributes, and allowed values.

Right-clicking a node shows its declared transient values, connection actions, selectable references to flow attributes, and Delete node. Right-clicking a connection annotation offers the source's declared transient values and Delete link. Ctrl-clicking two nodes creates or relinks a connection; the inspector then selects its transient. A connection label shows the chosen transient value. The transient reference is chosen from declared outputs; users cannot type an arbitrary reference. Double-clicking an inline Process opens its inner flow as the current canvas view; Back to flow returns to its parent.

Save Draft persists work in progress and reports structural design issues without discarding the draft. Publishing still requires a valid graph. Unsaved flow or Python code changes prompt for Save Draft, Save and Publish when available, Discard, or Stay before leaving the editor.

## Graph semantics

- A flow has exactly one Start and one or more End activities. End activities have declared transient values. An End that produces more than one value in one execution causes a flow error.
- An Activity declares a named list of allowed transient values in the designer. Its Python code emits exactly one value from that list per execution. The matching outgoing connection is followed. Missing, unknown, or ambiguous matches are execution errors.
- A Notification node can be placed, linked, and configured with design attributes and transient values like other canvas nodes. It will support both send-and-continue and send-and-wait modes. Recipients are named users, groups, or roles. In wait mode, the entire flow stays at the Notification until the first authorized response or a required timeout link selects its outgoing transient. Delivery succeeds if at least one recipient receives it; if none do, the flow fails.
- The flow, each Activity, and each Process can declare named attributes with a type, default value, and candidate values. Runtime values must belong to the candidate list when one is defined. The attribute window supports create, delete, and rename operations. Endpoint inputs merge flow-level attributes and a separate input schema; the rule for a name in both is still to be confirmed. The purpose of the previously mentioned Python file name or annotation name remains to be clarified.
- A Process is an Activity whose implementation is a flow. It can contain an inline flow or reference a saved flow. A referenced Process shows the latest child version while designing. Publishing the parent pins the child version in the parent snapshot.
- Incoming value and context are passed to the Process's inner Start activity. The inner End activity's declared transient values are the Process's outward transient values. The inner End stores final Process attribute values. Exactly one outward value is produced for each Process execution.
- Existing saved graphs containing only Python nodes and sequential edges need a migration or compatibility conversion before they can be edited or published under the new format.

## Python activity modules

Group related Activity functions in one Python module. A module may register many `@activity` functions, and a flow may use functions from many modules. The designer should select an Activity by its stable registered name and version, with its source file and function name shown as helpful metadata. A file path should not be the saved graph's Activity identity: moving a file would otherwise break the graph. Activity names should be qualified by domain to avoid collisions across files (for example, `orders.validate` and `mail.send`).

The current registry already supports multiple decorated functions in a file; `gyrfalcon/flow/sample_activities.py` demonstrates this. Plain helper functions can share code without becoming canvas activities. The current loader discovers direct `.py` files in the profile's `flows/` directory, so modules should live there until package discovery is added.

Proposed authoring shape (the `ActivityContext` and `ActivityResult` interfaces still need implementation):

```python
# flows/orders.py
from gyrfalcon.flow import activity
from gyrfalcon.flow.graph_context import ActivityContext, ActivityResult


def normalize_order(value: dict) -> dict:
    return {**value, "total": float(value["total"])}


@activity(name="orders.validate", version="1")
def validate_order(ctx: ActivityContext) -> ActivityResult:
    order = normalize_order(ctx.incoming_value)
    outcome = "approved" if order["total"] <= ctx.attr("approval_limit") else "review"
    return ActivityResult(transient=outcome, attributes={"order_total": order["total"]})


@activity(name="orders.notify", version="1")
def notify_order(ctx: ActivityContext) -> ActivityResult:
    # Read prior context and write only attributes declared for this node.
    return ActivityResult(transient="sent", attributes={"notification_sent": True})
```

The designer declares each node's allowed transient names and attribute constraints. The runtime checks that a function returns one declared transient and that written attribute values match their declared type and candidate list. The graph execution API can adapt existing `@activity` functions that return plain values so older graphs remain usable.

## Persistence and execution

- `fnd_flow_attrs` stores design time attributes for flows, activities, and processes, including default values. Published definitions need an immutable attribute snapshot associated with their version; editing a draft must not change a published version.
- A corresponding runtime attribute table stores values scoped to a unique flow instance and Activity or Process instance. The Activity's Python code can read the prior value and context and write its runtime attributes. A Process End writes the final Process attributes.
- At the start of each Activity attempt, persist the exact execution context made available to it: incoming value, completed prior outputs, applicable flow/Process/Activity runtime attribute values, and the pinned graph and implementation versions. This is the Activity's retry checkpoint; retries must not recompute it from changed defaults or rerun earlier successful nodes.
- A failed Activity remains at its failed node until an operator retries it. A manual retry creates a new attempt/node-visit record under the same flow run ID and invokes the same pinned Activity with the saved retry checkpoint. On success, execution follows that Activity's selected transient and continues; completed prior visits remain completed. Preserve failed-attempt details for inspection. Automatic retry is not part of the initial behavior, but may later apply a configured retry policy using the same checkpoint.
- Checkpoint restoration recovers workflow inputs and attributes, but cannot determine whether an external side effect completed when its response was lost. Activities that perform such operations must make retries safe, for example by sending the same stable idempotency key on each attempt or by checking the external system's status and skipping an operation that already succeeded.
- A flow instance receives a unique run ID when its daemon starts it. Nested Process nodes share that run ID and use a stable nested node path to distinguish their state and attributes.
- Attribute and transient reads and writes must be tenant scoped, with SQL in `gyrfalcon/db/sql.py` and schema changes through Alembic, following the repository's shared database rules.
- Every node except End must have an outgoing connection. Saving reports a design error when one is missing. Connections may return to earlier nodes to form a loop. The node that owns a backward connection can declare `max_loop`, `max_timeout`, or both as attributes. Reaching the count limit follows its dedicated count-limit transient; exceeding the elapsed-time limit follows its dedicated timeout transient. A timeout such as 24 hours is measured from the first traversal of that loop, with its start time and deadline persisted on the runtime flow instance so a restart does not reset it. The timeout is checked when execution next reaches that loop node's decision; it does not interrupt an active Activity. If both limits are set, the first one reached determines the exit transient. Publishing validates one Start, at least one End, reachable nodes, permitted connections, declared transient names, attribute types, bounded loops, and child Process references. It pins referenced child versions and rejects recursive Process references.

### Agreed visual flow daemon direction

- A deployment can start a published visual flow manually, on a schedule, or through a REST/webhook endpoint identified by the deployment's short name. A Python caller can also start a run. Invocation creates a unique run ID and returns it immediately. An optional idempotency key returns that same run ID on a repeated call. A deployment pins its published flow version until explicitly updated. OAuth service-account calls must be attributed to the specific service account and tenant, rather than to the shared `LOCAL` principal. Code-defined `@flow` deployments and their run history can be retired without compatibility or retention.
- The daemon assigns each run to a worker in an OS-process pool. The Start node initializes that run's context. A worker executes nodes in order, including inline Process flows, until an End node completes the flow or execution stops. A decision selects exactly one outgoing transient; this design does not launch parallel branches. A failed Activity stops the flow immediately and waits for an operator to retry it from its saved checkpoint; there is no automatic retry initially. A finished worker returns to the pool.
- An Activity can run Python, a Gyrfalcon agent, or an external A2A agent. Agent Activities can pause for human input; the reply contributes to the structured return value and declared transient. The node selects full available context or a prior node's output, plus allowed attributes. Pin the agent identity/version and fail the node with captured details if resolution fails. Every agent-backed node visit creates a dedicated `ai_sessions` record with its flow run and node visit IDs; the node visit stores that session ID. Agent turns and human replies go into `ai_session_messages` tagged with the node visit ID. A loop or retry creates another visit and session; a resumed wait reuses the same session. Tool permissions and human handoff template selection remain open.
- Use `fnd_flow_dt_` for `definitions`, `definition_versions`, `attrs`, `notif`, and `deployments`. `fnd_flow_dt_notif` stores reusable notification templates and recipient configuration; published flows snapshot the template. Use `fnd_flow_rt_` for `statuses`, `events`, `node_statuses`, `attrs`, and `notif`. `fnd_flow_rt_statuses` has one row per run; a rewind starts a new run. `fnd_flow_rt_node_statuses` stores one row per actual node visit, including loops, retries, inline Process nodes, and Notification waits. Nested inline Process nodes use a stable nested node path. The caller idempotency key is stored on the run status row, with uniqueness enforced there.
- Flow Notification content, including chat and email, is stored as `ai_session_messages` rows with the owning node visit ID. Each Notification visit creates a source=`flow_notification` `ai_sessions` row so its messages have a session ID; a waiting reply reuses that row. Email rows need a channel/type marker, direction, sender and To/Cc/Bcc recipients, subject, plain text `content`, optional HTML, message IDs and headers, and attachment references. `fnd_flow_rt_notif` references the message row and records per-recipient/channel delivery attempts, retries, and outcomes. Agent trajectory and prompt-context builders must select agent turns explicitly; notification rows must not be injected into agent prompts or exposed through unrelated chat history. Existing `fnd_emails` mailbox records remain a separate ingestion/transport concern.
- The old `fnd_flow_*` definition, deployment, event, run, state, edge, and attribute tables are retired without data migration. The new `fnd_flow_dt_*` and `fnd_flow_rt_*` tables replace them at cutover. The current canvas still queries old definition tables, so those are dropped when the canvas moves to the new design tables. `fnd_flow_rt_statuses` records visual flow runs; `ai_sessions` records agent runs and message-bearing Notification interactions.
- A read-only React Flow runtime diagram displays the published graph, the path actually traversed, current or final node states, and where execution is waiting or stopped.
- Every node visit stores the activity context checkpoint that was visible at node entry: incoming value, run inputs, available prior outputs, referenced flow attributes, and the node's runtime attributes. Successful visits also store the node attribute values after completion. The run inspector shows the published graph with taken transitions and visit status; selecting a node shows its checkpoint, resulting attributes, output, and failure details. Repeated visits can be inspected separately.
- An administrator may bounce the daemon, forcibly terminating its worker processes. An Activity interrupted this way retains its start checkpoint and waits for operator retry as a new attempt under the same run ID; completed prior nodes are not rerun. A persisted Notification wait survives the bounce.
- Notification wait mode stops progress of the whole flow until a response or timeout. It persists the wait and releases its worker. A response or timeout dispatches the same run ID to an available worker, which restores the saved context and follows the selected outgoing transition without rerunning Start or completed nodes.

## Implementation status

The canvas edits version 2 visual graphs and calls the versioned design-time APIs. `fnd_flow_dt_*` and `fnd_flow_rt_*` tables are defined in SQLAlchemy metadata and Alembic revisions; the visual `GraphStore` publishes immutable graph snapshots and deployment pins. The visual executor supports Python, Gyrfalcon-agent, A2A, Notification and nested Process nodes, persisted context, bounded loop decisions, and wait/resume. Failed activities can be manually retried under the same run ID using their saved start context; automatic retry remains deferred. A process-pool daemon claims queued runs, handles schedules and wait timeouts, and records worker failures and bounce events. The dashboard exposes deployment, invocation, runtime inspection, response, and daemon-management endpoints; `gyrfalcon flow-daemon run` starts the standalone worker service. Flow Instances renders the pinned graph and per-visit attribute/context details. Its Run sample flow action publishes and queues a ten-Activity order flow with four exclusive decision forks for exercising branch and join behavior.

Validation performed for this implementation: Python modules compile with `uv run python -m compileall -q gyrfalcon gyrfalcon_cli`, and `git diff --check` reports no whitespace errors. The Alembic revision has not been applied: the local PostgreSQL 17 cluster is down and owned by `nobody`, and the configured `.env` DSN points to a remote Supabase database. The remote database was not changed. Start the local cluster with appropriate PostgreSQL administrator privileges, then run the schema upgrade before starting the daemon.
