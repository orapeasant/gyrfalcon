"""Phases 8–9 — pause/suspend, and the agentic half.

Spec: 15-flow.md §7 (context), §8 (pause/suspend), §13.3 (the split), §13.4 (AgentStepPolicy).

§8 is called "the single most reusable piece": a typed, schema-rendered,
timeout-bounded, resumable approval gate between an agent's proposal and an
irreversible action. §13.4 is the differentiator, and every rule there must be
implementable with zero engine changes.
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires(
    "gyrfalcon.flow:pause_flow_run",
    "gyrfalcon.flow.inputs:RunInput",
    section="§8",
)


# ── §7 context ────────────────────────────────────────────────────────────────

class TestRunContext:
    def test_get_run_context_raises_outside_a_run(self):
        get_run_context = sym("gyrfalcon.flow.context:get_run_context")
        MissingContextError = sym("gyrfalcon.flow.exceptions:MissingContextError")
        with pytest.raises(MissingContextError):
            get_run_context()

    def test_context_round_trips_across_a_process_boundary(self):
        """§7(1): design for this on day one or distributed execution is a retrofit."""
        serialize = sym("gyrfalcon.flow.context:serialize_context")
        hydrate = sym("gyrfalcon.flow.context:hydrated_context")
        flow = sym("gyrfalcon.flow:flow")

        captured = {}

        @flow
        def f():
            captured["payload"] = serialize()
            return 1

        f()
        with hydrate(captured["payload"]):
            ctx = sym("gyrfalcon.flow.context:get_run_context")()
            assert ctx is not None

    def test_shipped_context_is_marked_detached(self):
        hydrate = sym("gyrfalcon.flow.context:hydrated_context")
        serialize = sym("gyrfalcon.flow.context:serialize_context")
        flow = sym("gyrfalcon.flow:flow")
        captured = {}

        @flow
        def f():
            captured["payload"] = serialize()

        f()
        with hydrate(captured["payload"]):
            ctx = sym("gyrfalcon.flow.context:get_run_context")()
            assert ctx.detached is True


# ── §8 pause / suspend ────────────────────────────────────────────────────────

class TestPauseGate:
    def test_pause_produces_a_paused_state_that_is_not_terminal(self):
        states = sym("gyrfalcon.flow.states")
        p = states.Paused()
        assert p.type.name == "PAUSED"
        assert p.is_final() is False

    def test_suspend_always_reschedules_pause_does_not(self):
        """§2.3(2): suspend exits the process — required for multi-day approvals."""
        states = sym("gyrfalcon.flow.states")
        assert states.Paused().state_details.pause_reschedule is False
        assert states.Suspended().state_details.pause_reschedule is True

    def test_run_input_is_a_schema_the_server_can_render(self):
        RunInput = sym("gyrfalcon.flow.inputs:RunInput")

        class Approval(RunInput):
            approve: bool = False
            notes: str = ""

        schema = Approval.model_json_schema()
        assert set(schema["properties"]) == {"approve", "notes"}

    def test_with_initial_data_attaches_a_description(self):
        RunInput = sym("gyrfalcon.flow.inputs:RunInput")

        class Approval(RunInput):
            approve: bool = False

        seeded = Approval.with_initial_data(description="**Preview:**\nrm -rf /tmp/x")
        assert "Preview" in seeded.description

    def test_pause_key_makes_a_reentered_pause_idempotent(self):
        """§8: a re-entered pause must not re-prompt."""
        should_prompt = sym("gyrfalcon.flow.pause:should_prompt")
        seen = {"approval-1"}
        assert should_prompt("approval-1", seen) is False
        assert should_prompt("approval-2", seen) is True

    def test_pause_timeout_is_recorded_on_the_state(self):
        """An approval that is never answered must not wait forever."""
        build = sym("gyrfalcon.flow.pause:build_paused_state")
        state = build(timeout=3600, reschedule=False, pause_key="k1")
        assert state.state_details.pause_timeout == 3600
        assert state.state_details.pause_key == "k1"

    def test_resume_carries_typed_input_back_into_the_run(self):
        resume = sym("gyrfalcon.flow:resume_flow_run")
        RunInput = sym("gyrfalcon.flow.inputs:RunInput")

        class Approval(RunInput):
            approve: bool = False

        result = resume("fr-1", run_input=Approval(approve=True))
        assert result.run_input.approve is True


# ── §13.3 the deterministic/agentic split ─────────────────────────────────────

class TestAgentBoundary:
    def test_an_agent_step_is_a_flow_run_not_a_task_run(self):
        """§13.3: only flow runs are externally cancellable, pausable, budget-limitable."""
        agent_step = sym("gyrfalcon.flow.agent:agent_step")

        @agent_step
        def researcher(question: str) -> str:
            return "answer"

        assert researcher.is_flow is True
        assert researcher.is_task is False

    def test_one_model_call_is_one_task(self):
        """§13.3(1): a flaky API burns one retry, not a replay of the agent loop."""
        agent = sym("gyrfalcon.flow.agent")
        assert agent.model_call.is_task is True
        assert agent.model_call.retries >= 1

    def test_agent_output_must_validate_or_fail_as_an_ordinary_task_failure(self):
        """§13.3(3): the boundary is a validated schema object or nothing."""
        validate = sym("gyrfalcon.flow.agent:validate_output")
        RunInput = sym("gyrfalcon.flow.inputs:RunInput")

        class Out(RunInput):
            total: int

        assert validate(Out, {"total": 5}).total == 5

        state = validate(Out, {"total": "not-an-int"}, return_type="state")
        assert state.type.name == "FAILED"
        assert state.state_details.retriable is True, "validation failure is an ordinary retryable task failure"

    def test_subflow_records_an_encapsulating_edge_to_its_children(self):
        """§13.3: 'the agent did these six tool calls' must be renderable."""
        edges = sym("gyrfalcon.flow.futures:encapsulating_edges")
        agent_step = sym("gyrfalcon.flow.agent:agent_step")

        @agent_step
        def researcher():
            return "done"

        run = researcher(return_type="state")
        assert edges(run.state_details.child_flow_run_id or run.id) is not None


# ── §13.4 AgentStepPolicy — zero engine changes ───────────────────────────────

class TestAgentStepPolicy:
    def test_policy_is_registered_and_ordered(self):
        policy = sym("gyrfalcon.flow.policies:AgentStepPolicy")
        names = [r.__name__ for r in policy.priority()]
        assert {
            "EnforceTokenBudget", "RequireApprovalAboveCost", "DetectToolLoop",
            "CapAgentIterations", "RouteToStrongerModel", "RecordAgentDecision",
        } <= set(names)

    def test_token_budget_exceeded_aborts(self):
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.policies:EnforceTokenBudget")
        states = sym("gyrfalcon.flow.states")

        result = run_rules([Rule], states.Running(), states.Running(),
                           ctx={"tokens_spent": 50_000, "token_budget": 10_000})
        assert result.status.name == "ABORT"

    def test_token_budget_within_limit_accepts(self):
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.policies:EnforceTokenBudget")
        states = sym("gyrfalcon.flow.states")

        result = run_rules([Rule], states.Running(), states.Running(),
                           ctx={"tokens_spent": 100, "token_budget": 10_000})
        assert result.status.name == "ACCEPT"

    def test_costly_tool_call_is_rejected_into_paused(self):
        """§13.4: the approval gate in front of an expensive/irreversible action."""
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.policies:RequireApprovalAboveCost")
        states = sym("gyrfalcon.flow.states")

        result = run_rules([Rule], states.Running(), states.Running(),
                           ctx={"estimated_cost": 25.0, "approval_threshold": 5.0})
        assert result.status.name == "REJECT"
        assert result.state.type.name == "PAUSED"

    def test_repeated_tool_cycle_aborts(self):
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.policies:DetectToolLoop")
        states = sym("gyrfalcon.flow.states")

        looping = ["read_file", "terminal"] * 6
        result = run_rules([Rule], states.Running(), states.Running(),
                           ctx={"recent_tool_calls": looping})
        assert result.status.name == "ABORT"

    def test_varied_tool_calls_do_not_trip_loop_detection(self):
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.policies:DetectToolLoop")
        states = sym("gyrfalcon.flow.states")

        result = run_rules([Rule], states.Running(), states.Running(),
                           ctx={"recent_tool_calls": ["a", "b", "c", "d", "e", "f"]})
        assert result.status.name == "ACCEPT"

    def test_iteration_cap_rejects_into_a_named_failure(self):
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.policies:CapAgentIterations")
        states = sym("gyrfalcon.flow.states")

        result = run_rules([Rule], states.Running(), states.Running(),
                           ctx={"iterations": 99, "max_iterations": 20})
        assert result.status.name == "REJECT"
        assert result.state.type.name == "FAILED"
        assert result.state.name == "IterationCapReached"

    def test_escalation_renames_without_changing_the_type(self):
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.policies:RouteToStrongerModel")
        states = sym("gyrfalcon.flow.states")

        result = run_rules([Rule], states.Failed(), states.Retrying(),
                           ctx={"retries": 2, "escalate_after": 1})
        assert result.state.name == "EscalatingModel"
        assert result.state.type.name == "RUNNING"

    def test_decisions_are_emitted_for_the_audit_trail(self):
        """§13.4: RecordAgentDecision fires in after_transition."""
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.policies:RecordAgentDecision")
        states = sym("gyrfalcon.flow.states")

        emitted: list[dict] = []
        run_rules([Rule], states.Running(), states.Completed(),
                  ctx={"emit": emitted.append})
        assert any(e.get("event", "").startswith("gyrfalcon.agent.") for e in emitted)

    def test_agent_rules_require_no_engine_changes(self):
        """§13.4: the proof that the orchestration boundary earns its round trip."""
        Rule = sym("gyrfalcon.flow.orchestration:BaseOrchestrationRule")
        policy = sym("gyrfalcon.flow.policies:AgentStepPolicy")
        for rule in policy.priority():
            assert issubclass(rule, Rule), f"{rule.__name__} must be an ordinary rule"


class TestPauseResumeCycle:
    """The engine actually settles a pause and re-enters the flow, rather than
    only exercising pause_flow_run() in isolation.
    """

    def test_paused_run_is_visible_as_paused_not_stuck_running(self, tmp_path):
        """Regression: the paused state was never persisted, so a store-backed
        viewer (the run list, the human-task inbox) saw the run as stuck
        RUNNING forever, contradicting the pending-task inbox.
        """
        import threading
        import time as _time

        flow = sym("gyrfalcon.flow:flow")
        pause_flow_run = sym("gyrfalcon.flow:pause_flow_run")
        resume_flow_run = sym("gyrfalcon.flow:resume_flow_run")
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        set_store = sym("gyrfalcon.flow.store:set_store")
        engine = sym("gyrfalcon.flow.engine:_BaseRunEngine")

        store = RunStore(tmp_path / "flow.db")
        set_store(store)
        engine.persist = True
        try:
            @flow
            def approval():
                return pause_flow_run(key="k", timeout=5)

            result = {}
            t = threading.Thread(target=lambda: result.__setitem__("s", approval(return_type="state")))
            t.start()

            run_id = None
            for _ in range(200):
                rows, total = store.list_runs(name="approval")
                if total:
                    run_id = rows[0]["id"]
                    if rows[0]["state_type"] == "PAUSED":
                        break
                _time.sleep(0.01)

            assert store.get_run(run_id)["state_type"] == "PAUSED"

            resume_flow_run(run_id, run_input={"ok": True})
            t.join(timeout=5)
            assert not t.is_alive()
            assert result["s"].name == "Completed"
            assert store.get_run(run_id)["state_type"] == "COMPLETED"
        finally:
            engine.persist = False
            set_store(None)
            store.close()

    def test_explicit_flow_run_id_override_does_not_hang(self):
        """Regression: an explicit flow_run_id different from the engine's own
        run_id made the engine poll a key nobody could ever answer, and the
        timeout check used the same wrong key, so it also never expired —
        an unconditional hang.
        """
        import threading

        flow = sym("gyrfalcon.flow:flow")
        pause_flow_run = sym("gyrfalcon.flow:pause_flow_run")
        resume_flow_run = sym("gyrfalcon.flow:resume_flow_run")

        @flow
        def approval():
            return pause_flow_run(key="k", flow_run_id="custom-correlation-id", timeout=5)

        result = {}
        t = threading.Thread(target=lambda: result.__setitem__("s", approval(return_type="state")))
        t.start()
        t.join(timeout=0.3)
        assert t.is_alive(), "should still be waiting on the pause"

        resume_flow_run("custom-correlation-id", run_input={"ok": True})
        t.join(timeout=5)
        assert not t.is_alive(), "engine hung polling the wrong id"
        assert result["s"].name == "Completed"

    def test_unanswered_pause_times_out_rather_than_hanging_forever(self):
        flow = sym("gyrfalcon.flow:flow")
        pause_flow_run = sym("gyrfalcon.flow:pause_flow_run")

        @flow
        def never_answered():
            pause_flow_run(key="k", timeout=0.1)
            return "unreachable"

        state = never_answered(return_type="state")
        assert state.name == "PauseTimedOut"
        assert state.type.name == "FAILED"

    def test_suspended_pause_stops_rather_than_polling(self):
        """§8: suspend always reschedules; the engine must not block on it."""
        flow = sym("gyrfalcon.flow:flow")
        suspend_flow_run = sym("gyrfalcon.flow:suspend_flow_run")

        @flow
        def suspends():
            return suspend_flow_run(key="k", timeout=5)

        state = suspends(return_type="state")
        assert state.name == "Suspended"
        assert state.type.name == "PAUSED"

    def test_none_is_a_legitimate_answer_distinct_from_unanswered(self):
        """resume_flow_run(run_id) with no run_input must still count as answered."""
        check_answer = sym("gyrfalcon.flow.pause:check_answer")
        resume_flow_run = sym("gyrfalcon.flow:resume_flow_run")
        UNANSWERED = sym("gyrfalcon.flow.pause:_UNANSWERED")

        assert check_answer("never-touched") is UNANSWERED
        resume_flow_run("touched-with-none")
        assert check_answer("touched-with-none") is None
