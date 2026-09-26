"""Phases 4–5 — the orchestration boundary.

Spec: 15-flow.md §4.1–4.4, §13.1(4,5), §13.2.

"This is the most important section for gyrfalcon." The four-way propose/response
is the entire external control plane; REJECT-with-substitution is what delivers a
server-side decision to a client that never asked for it.
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires(
    "gyrfalcon.flow.orchestration:propose_state",
    "gyrfalcon.flow.orchestration:SetStateStatus",
    section="§4.2",
)


# ── §4.2 the propose/response protocol ────────────────────────────────────────

class TestProposeProtocol:
    def test_accept_adopts_server_identity(self):
        propose = sym("gyrfalcon.flow.orchestration:propose_state")
        Response = sym("gyrfalcon.flow.orchestration:SetStateResponse")
        Status = sym("gyrfalcon.flow.orchestration:SetStateStatus")
        states = sym("gyrfalcon.flow.states")

        proposed = states.Running()
        server_state = states.Running()
        server_state.id = "srv-1"

        client = _StubClient([Response(status=Status.ACCEPT, state=server_state)])
        result = propose(client, run_id="fr-1", state=proposed)

        assert result.id == "srv-1", "client must adopt the server's id/timestamp"

    def test_reject_substitutes_the_servers_state(self):
        """§4.2: the server counter-proposes and the client adopts it as reality."""
        propose = sym("gyrfalcon.flow.orchestration:propose_state")
        Response = sym("gyrfalcon.flow.orchestration:SetStateResponse")
        Status = sym("gyrfalcon.flow.orchestration:SetStateStatus")
        states = sym("gyrfalcon.flow.states")

        client = _StubClient([Response(status=Status.REJECT, state=states.Cached())])
        result = propose(client, run_id="fr-1", state=states.Running())

        assert result.name == "Cached"
        assert result.type.name == "COMPLETED"

    def test_reject_with_paused_raises_pause(self):
        propose = sym("gyrfalcon.flow.orchestration:propose_state")
        Response = sym("gyrfalcon.flow.orchestration:SetStateResponse")
        Status = sym("gyrfalcon.flow.orchestration:SetStateStatus")
        Pause = sym("gyrfalcon.flow.exceptions:Pause")
        states = sym("gyrfalcon.flow.states")

        client = _StubClient([Response(status=Status.REJECT, state=states.Paused())])
        with pytest.raises(Pause):
            propose(client, run_id="fr-1", state=states.Running())

    def test_abort_raises_and_does_not_retry(self):
        propose = sym("gyrfalcon.flow.orchestration:propose_state")
        Response = sym("gyrfalcon.flow.orchestration:SetStateResponse")
        Status = sym("gyrfalcon.flow.orchestration:SetStateStatus")
        Abort = sym("gyrfalcon.flow.exceptions:Abort")
        states = sym("gyrfalcon.flow.states")

        client = _StubClient([Response(status=Status.ABORT, details={"reason": "budget exhausted"})])
        with pytest.raises(Abort, match="budget exhausted"):
            propose(client, run_id="fr-1", state=states.Running())
        assert client.calls == 1, "ABORT must stop entirely, not re-propose"

    def test_wait_sleeps_then_reproposes_until_resolved(self):
        """§4.2: WAIT loops internally on delay_seconds."""
        propose = sym("gyrfalcon.flow.orchestration:propose_state")
        Response = sym("gyrfalcon.flow.orchestration:SetStateResponse")
        Status = sym("gyrfalcon.flow.orchestration:SetStateStatus")
        states = sym("gyrfalcon.flow.states")

        slept: list[float] = []
        client = _StubClient([
            Response(status=Status.WAIT, details={"delay_seconds": 3}),
            Response(status=Status.WAIT, details={"delay_seconds": 3}),
            Response(status=Status.ACCEPT, state=states.Running()),
        ])

        propose(client, run_id="fr-1", state=states.Running(), sleep=slept.append)

        assert slept == [3, 3]
        assert client.calls == 3


# ── §4.1 two consistency models ───────────────────────────────────────────────

class TestConsistencyModels:
    def test_flow_transitions_go_through_the_server(self):
        """§4.1: flow state is authoritative server-side — that buys external control."""
        engine = sym("gyrfalcon.flow.engine:FlowRunEngine")
        assert getattr(engine, "state_is_server_authoritative", None) is True

    def test_task_transitions_are_local_and_emit_an_event(self):
        """§4.1: no round trip; the server learns via an emitted event."""
        engine = sym("gyrfalcon.flow.engine:TaskRunEngine")
        assert getattr(engine, "state_is_server_authoritative", None) is False

    def test_task_authority_is_overridable_per_step(self):
        """§13.2: make it a per-step property, not a global architecture choice."""
        activity = sym("gyrfalcon.flow:activity")

        @activity(server_authoritative_state=True)
        def agent_step():
            return 1

        assert agent_step.server_authoritative_state is True


# ── §4.3 rules ────────────────────────────────────────────────────────────────

class TestOrchestrationRules:
    def test_rule_gates_on_from_and_to_state_types(self):
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.orchestration:BaseOrchestrationRule")
        states = sym("gyrfalcon.flow.states")
        StateType = states.StateType
        fired = []

        class OnlyRunningToCompleted(Rule):
            FROM_STATES = [StateType.RUNNING]
            TO_STATES = [StateType.COMPLETED]

            async def before_transition(self, initial, proposed, ctx):
                fired.append("fired")

        run_rules([OnlyRunningToCompleted], states.Pending(), states.Completed())
        assert fired == [], "must not fire when the initial type is outside FROM_STATES"

        run_rules([OnlyRunningToCompleted], states.Running(), states.Completed())
        assert fired == ["fired"]

    @pytest.mark.parametrize("verb", [
        "reject_transition", "delay_transition", "abort_transition", "rename_state",
    ])
    def test_rule_exposes_the_four_verbs(self, verb):
        Rule = sym("gyrfalcon.flow.orchestration:BaseOrchestrationRule")
        assert hasattr(Rule, verb)

    def test_rename_state_keeps_the_type(self):
        """§4.3: change the label, not the machine."""
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.orchestration:BaseOrchestrationRule")
        states = sym("gyrfalcon.flow.states")

        class Renamer(Rule):
            FROM_STATES = TO_STATES = ["*"]

            async def before_transition(self, initial, proposed, ctx):
                self.rename_state("EscalatingModel")

        result = run_rules([Renamer], states.Running(), states.Running())
        assert result.state.name == "EscalatingModel"
        assert result.state.type is states.StateType.RUNNING

    def test_rules_nest_before_outside_in_after_inside_out(self, recorder):
        """§4.3: AsyncExitStack ordering is what makes cleanup correct."""
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.orchestration:BaseOrchestrationRule")
        states = sym("gyrfalcon.flow.states")

        def make(label):
            class R(Rule):
                FROM_STATES = TO_STATES = ["*"]

                async def before_transition(self, i, p, c):
                    recorder.record(f"before:{label}")

                async def after_transition(self, i, v, c):
                    recorder.record(f"after:{label}")
            return R

        run_rules([make("outer"), make("inner")], states.Running(), states.Completed())
        assert recorder.labels == [
            "before:outer", "before:inner", "after:inner", "after:outer",
        ]

    def test_cleanup_compensates_when_an_inner_rule_aborts(self, recorder):
        """§4.3: a slot acquired in before_transition is released if an inner rule aborts."""
        run_rules = sym("gyrfalcon.flow.orchestration:run_rules")
        Rule = sym("gyrfalcon.flow.orchestration:BaseOrchestrationRule")
        states = sym("gyrfalcon.flow.states")

        class AcquiresSlot(Rule):
            FROM_STATES = TO_STATES = ["*"]

            async def before_transition(self, i, p, c):
                recorder.record("acquire")

            async def cleanup(self, i, v, c):
                recorder.record("release")

        class Aborts(Rule):
            FROM_STATES = TO_STATES = ["*"]

            async def before_transition(self, i, p, c):
                self.abort_transition("nope")

        run_rules([AcquiresSlot, Aborts], states.Running(), states.Completed())
        assert recorder.labels == ["acquire", "release"]


# ── §4.4 policies are ordered data ────────────────────────────────────────────

class TestPolicies:
    def test_policy_priority_is_an_ordered_list(self):
        policy = sym("gyrfalcon.flow.policies:CoreFlowPolicy")
        assert isinstance(policy.priority(), list)

    def test_cache_retrieval_precedes_concurrency_slots(self):
        """§4.4: a cached result does no work, so a full slot pool must not block it."""
        policy = sym("gyrfalcon.flow.policies:CoreTaskPolicy")
        names = [r.__name__ for r in policy.priority()]
        assert names.index("CacheRetrieval") < names.index("SecureTaskConcurrencySlots")

    def test_policies_are_swappable_without_engine_changes(self):
        """§13.2: first-class registration, not an internal list."""
        registry = sym("gyrfalcon.flow.policies:registry")
        Rule = sym("gyrfalcon.flow.orchestration:BaseOrchestrationRule")

        class MyRule(Rule):
            FROM_STATES = TO_STATES = ["*"]

        registry.register("custom", [MyRule])
        assert registry.get("custom") == [MyRule]


# ── stub ──────────────────────────────────────────────────────────────────────

class _StubClient:
    """Returns queued responses in order; counts calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def set_state(self, run_id, state, force=False):
        self.calls += 1
        return self._responses.pop(0)
