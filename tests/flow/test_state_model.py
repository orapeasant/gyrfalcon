"""Phase 1 — the state model.

Spec: 15-flow.md §1.3, §2.1–2.3, §13.1(2), §13.1(12).

The load-bearing property: `type` is a closed enum the orchestration layer
switches on, `name` is an open string for humans. Agentic vocabulary
("Deliberating", "AwaitingTool") must ride on that without touching the machine.
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires(
    "gyrfalcon.flow.states:State",
    "gyrfalcon.flow.states:StateType",
    section="§1.3, §2.1",
)


# ── §2.1 the closed enum ──────────────────────────────────────────────────────

EXPECTED_TYPES = {
    "SCHEDULED", "PENDING", "RUNNING", "PAUSED", "CANCELLING",
    "COMPLETED", "FAILED", "CANCELLED", "CRASHED",
}
EXPECTED_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "CRASHED"}


class TestStateTypes:
    def test_exactly_nine_types(self):
        StateType = sym("gyrfalcon.flow.states:StateType")
        assert {t.name for t in StateType} == EXPECTED_TYPES

    def test_terminal_set_is_explicit(self):
        StateType = sym("gyrfalcon.flow.states:StateType")
        TERMINAL = sym("gyrfalcon.flow.states:TERMINAL_STATES")
        assert {t.name for t in TERMINAL} == EXPECTED_TERMINAL
        assert StateType.RUNNING not in TERMINAL

    @pytest.mark.parametrize("type_name,final", [
        ("SCHEDULED", False), ("PENDING", False), ("RUNNING", False),
        ("PAUSED", False), ("CANCELLING", False),
        ("COMPLETED", True), ("FAILED", True), ("CANCELLED", True), ("CRASHED", True),
    ])
    def test_is_final_is_terminal_membership(self, type_name, final):
        State = sym("gyrfalcon.flow.states:State")
        StateType = sym("gyrfalcon.flow.states:StateType")
        assert State(type=getattr(StateType, type_name)).is_final() is final


# ── §2.2 names mapped onto types ──────────────────────────────────────────────

NAME_TO_TYPE = [
    ("Scheduled", "SCHEDULED"),
    ("Late", "SCHEDULED"),
    ("AwaitingRetry", "SCHEDULED"),
    ("AwaitingConcurrencySlot", "SCHEDULED"),
    ("Resuming", "SCHEDULED"),
    ("Pending", "PENDING"),
    ("Submitting", "PENDING"),
    ("InfrastructurePending", "PENDING"),
    ("Running", "RUNNING"),
    ("Retrying", "RUNNING"),
    ("Paused", "PAUSED"),
    ("Suspended", "PAUSED"),
    ("Cancelling", "CANCELLING"),
    ("Cancelled", "CANCELLED"),
    ("Completed", "COMPLETED"),
    ("Cached", "COMPLETED"),
    ("RolledBack", "COMPLETED"),
    ("Failed", "FAILED"),
    ("TimedOut", "FAILED"),
    ("Crashed", "CRASHED"),
]


class TestStateNames:
    @pytest.mark.parametrize("name,type_name", NAME_TO_TYPE)
    def test_constructor_produces_expected_type(self, name, type_name):
        states = sym("gyrfalcon.flow.states")
        ctor = getattr(states, name, None)
        assert ctor is not None, f"missing state constructor {name}()"
        state = ctor()
        assert state.type.name == type_name
        assert state.name == name

    def test_retrying_is_running_not_a_separate_type(self):
        """A retry is observable vocabulary over RUNNING, not a new state type."""
        states = sym("gyrfalcon.flow.states")
        assert states.Retrying().type is states.StateType.RUNNING
        assert states.Retrying().is_final() is False

    def test_cached_is_a_successful_terminal_state(self):
        states = sym("gyrfalcon.flow.states")
        cached = states.Cached()
        assert cached.type is states.StateType.COMPLETED
        assert cached.is_final() is True

    def test_rolled_back_is_completed_not_failed(self):
        """§6.3: the run succeeded; its effects were undone."""
        states = sym("gyrfalcon.flow.states")
        assert states.RolledBack().type is states.StateType.COMPLETED

    def test_custom_agentic_names_need_no_enum_change(self):
        """§13.1(2): the whole point of type-vs-name."""
        states = sym("gyrfalcon.flow.states")
        State, StateType = states.State, states.StateType
        for label in ("Deliberating", "AwaitingTool", "Escalated"):
            s = State(type=StateType.RUNNING, name=label)
            assert s.name == label
            assert s.type is StateType.RUNNING
            assert s.is_final() is False


# ── §2.3 the distinctions that matter ─────────────────────────────────────────

class TestLoadBearingDistinctions:
    def test_failed_and_crashed_are_different_types(self):
        """Your code raised vs the world broke. Different retry/alerting/blame."""
        states = sym("gyrfalcon.flow.states")
        assert states.Failed().type is not states.Crashed().type

    def test_timed_out_is_failed_typed_but_distinctly_named(self):
        states = sym("gyrfalcon.flow.states")
        assert states.TimedOut().type is states.StateType.FAILED
        assert states.TimedOut().name == "TimedOut"

    def test_paused_and_suspended_share_a_type_but_differ_in_reschedule(self):
        """§2.3(2): pause_reschedule is the discriminator; suspend exits the process."""
        states = sym("gyrfalcon.flow.states")
        paused, suspended = states.Paused(), states.Suspended()
        assert paused.type is suspended.type is states.StateType.PAUSED
        assert paused.state_details.pause_reschedule is False
        assert suspended.state_details.pause_reschedule is True

    def test_cancelling_is_non_terminal_and_cancelled_is_terminal(self):
        """§2.3(3): cancellation is a two-phase commit against infrastructure."""
        states = sym("gyrfalcon.flow.states")
        assert states.Cancelling().is_final() is False
        assert states.Cancelled().is_final() is True


# ── §1.3 StateDetails is an open sidecar ──────────────────────────────────────

class TestStateDetails:
    @pytest.mark.parametrize("field", [
        "scheduled_time", "cache_key", "cache_expiration", "pause_timeout",
        "pause_reschedule", "pause_key", "run_input_keyset", "retriable",
        "traceparent", "child_flow_run_id",
    ])
    def test_documented_fields_present(self, field):
        StateDetails = sym("gyrfalcon.flow.states:StateDetails")
        assert field in StateDetails.model_fields

    def test_new_orchestration_metadata_needs_no_state_migration(self):
        """§1.3: StateDetails is where new features land without touching State."""
        states = sym("gyrfalcon.flow.states")
        s = states.Running()
        s.state_details.agent_token_budget = 10_000   # must not raise
        assert s.state_details.agent_token_budget == 10_000


class TestStateData:
    def test_data_holds_inline_value_or_metadata_pointer(self):
        """§6.1: discriminated union keeps the state table small."""
        states = sym("gyrfalcon.flow.states")
        Meta = sym("gyrfalcon.flow.results:ResultRecordMetadata")

        inline = states.Completed(data={"rows": 3})
        assert inline.data == {"rows": 3}

        pointed = states.Completed(data=Meta(storage_key="s3://bucket/key"))
        assert isinstance(pointed.data, Meta)
        assert pointed.data.storage_key == "s3://bucket/key"
