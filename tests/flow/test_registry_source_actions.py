"""Definitions-page actions on a code-first flow: get/save its source, delete
it, duplicate it, run it directly. Spec §14.9 (edit), §14.12 (run/delete/
duplicate — Agent-card parity, applied to Definitions the way §14.10 applies
it to Deployments).

Every action here is scoped to a flow whose source lives under the user's
flows directory; a flow registered inline (as in most other flow tests) has
no backing file and must be refused, not silently no-op.
"""

from __future__ import annotations

import time

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.registry:save_source", section="§14.9/§14.12")


@pytest.fixture()
def flows_dir(tmp_path, monkeypatch):
    """Point get_flows_dir() at a throwaway directory, the same way
    test_registry.py's own default-directory test does, so
    _editable_source_path resolves against it."""
    monkeypatch.setattr(
        "gyrfalcon.gyrfalcon_constants.get_gyrfalcon_home", lambda: tmp_path
    )
    get_flows_dir = sym("gyrfalcon.gyrfalcon_constants:get_flows_dir")
    return get_flows_dir()


@pytest.fixture()
def registered_source_flow(flows_dir):
    """A @flow file actually written to the flows directory and imported —
    unlike other flow tests' inline @flow, this one has a real backing file
    for get_source/save_source/delete_source/duplicate_source to act on."""
    discover_flows = sym("gyrfalcon.flow.registry:discover_flows")
    unique = f"src_test_flow_{id(flows_dir)}"
    path = flows_dir / f"{unique}.py"
    path.write_text(
        "from gyrfalcon.flow import flow\n\n"
        f'@flow(name="{unique}")\n'
        f"def {unique}(who='world'):\n"
        "    return f'pong: {who}'\n"
    )
    discover_flows(flows_dir)
    yield unique, path


@pytest.fixture()
def run_store(tmp_path):
    """flow_runs store, needed only by the run_definition_now tests."""
    RunStore = sym("gyrfalcon.flow.store:RunStore")
    set_store = sym("gyrfalcon.flow.store:set_store")
    s = RunStore(tmp_path / "flow.db")
    set_store(s)
    yield s
    set_store(None)
    s.close()


@pytest.fixture(autouse=True)
def _persist(request):
    if "run_store" not in request.fixturenames:
        yield
        return
    engine = sym("gyrfalcon.flow.engine:_BaseRunEngine")
    engine.persist = True
    yield
    engine.persist = False


class TestEditableBoundary:
    def test_inline_flow_has_no_editable_source(self):
        """A flow decorated directly in this test file has no file under the
        flows directory — every action must refuse it, not silently do
        nothing to a file that isn't there."""
        flow = sym("gyrfalcon.flow:flow")
        get_source = sym("gyrfalcon.flow.registry:get_source")

        @flow(name="src_test_inline_flow")
        def inline(): ...

        with pytest.raises(ValueError, match="editable source|not.*user-authored"):
            get_source("src_test_inline_flow")

    def test_list_definitions_reports_editable_flag(self, registered_source_flow):
        list_definitions = sym("gyrfalcon.flow.registry:list_definitions")
        name, _ = registered_source_flow
        entry = next(d for d in list_definitions() if d["name"] == name)
        assert entry["editable"] is True

    def test_unknown_definition_raises_not_silently_returns_none(self):
        get_source = sym("gyrfalcon.flow.registry:get_source")
        with pytest.raises(ValueError, match="No definition"):
            get_source("no-such-flow")


class TestGetAndSaveSource:
    def test_get_source_returns_path_and_content(self, registered_source_flow):
        get_source = sym("gyrfalcon.flow.registry:get_source")
        name, path = registered_source_flow
        result = get_source(name)
        assert result["path"] == str(path)
        assert "pong" in result["content"]

    def test_save_source_writes_file_and_reimports(self, registered_source_flow):
        get_source = sym("gyrfalcon.flow.registry:get_source")
        save_source = sym("gyrfalcon.flow.registry:save_source")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")
        name, path = registered_source_flow

        new_content = get_source(name)["content"].replace("pong", "PONGED")
        save_source(name, new_content)

        assert "PONGED" in path.read_text()
        assert get_definition(name) is not None
        assert get_definition(name)(who="x") == "PONGED: x"

    def test_save_source_with_syntax_error_rolls_back(self, registered_source_flow):
        get_source = sym("gyrfalcon.flow.registry:get_source")
        save_source = sym("gyrfalcon.flow.registry:save_source")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")
        name, path = registered_source_flow
        original = get_source(name)["content"]

        with pytest.raises(ValueError):
            save_source(name, "this is not python (((")

        assert path.read_text() == original
        assert get_definition(name) is not None, "registry must still work after a bad save"

    def test_save_source_that_renames_the_flow_is_refused(self, registered_source_flow):
        """Changing what name the file registers is a different operation
        (duplicate) wearing an edit's clothing — refused, not silently
        applied."""
        get_source = sym("gyrfalcon.flow.registry:get_source")
        save_source = sym("gyrfalcon.flow.registry:save_source")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")
        name, path = registered_source_flow
        original = get_source(name)["content"]

        renamed = original.replace(name, f"{name}_renamed")
        with pytest.raises(ValueError, match="no longer defines"):
            save_source(name, renamed)

        # Rolled back: the original name still resolves.
        assert get_definition(name) is not None
        assert path.read_text() == original

    def test_save_source_on_unknown_definition_raises(self):
        save_source = sym("gyrfalcon.flow.registry:save_source")
        with pytest.raises(ValueError, match="No definition"):
            save_source("no-such-flow", "content")


class TestDeleteSource:
    def test_delete_removes_file_and_unregisters(self, registered_source_flow):
        delete_source = sym("gyrfalcon.flow.registry:delete_source")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")
        name, path = registered_source_flow

        delete_source(name)
        assert not path.exists()
        assert get_definition(name) is None

    def test_delete_unknown_definition_raises(self):
        delete_source = sym("gyrfalcon.flow.registry:delete_source")
        with pytest.raises(ValueError, match="No definition"):
            delete_source("no-such-flow")


class TestDuplicateSource:
    def test_duplicate_creates_an_independent_second_flow(self, registered_source_flow):
        duplicate_source = sym("gyrfalcon.flow.registry:duplicate_source")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")
        name, path = registered_source_flow
        new_name = f"{name}_copy"

        dest = duplicate_source(name, new_name)

        assert dest.exists()
        assert get_definition(new_name) is not None
        assert get_definition(new_name).name == new_name
        assert get_definition(new_name)(who="dup") == "pong: dup"

    def test_duplicate_does_not_corrupt_the_original(self, registered_source_flow):
        """Regression: the copy's file still carries the original decorator's
        `name=`, so importing the copy transiently re-registers the
        *original* name under the copy's function object before the
        rename footer runs. The original's registry entry — and therefore
        its own get_source/delete_source — must be unaffected afterward."""
        duplicate_source = sym("gyrfalcon.flow.registry:duplicate_source")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")
        get_source = sym("gyrfalcon.flow.registry:get_source")
        name, path = registered_source_flow

        original_template = get_definition(name)
        duplicate_source(name, f"{name}_copy2")

        assert get_definition(name) is original_template
        assert get_source(name)["path"] == str(path)

        # And the original must still be independently deletable/editable
        # afterward — the real symptom the regression produced.
        delete_source = sym("gyrfalcon.flow.registry:delete_source")
        delete_source(f"{name}_copy2")
        delete_source(name)
        assert get_definition(name) is None

    def test_duplicate_onto_an_existing_name_is_refused(self, registered_source_flow):
        duplicate_source = sym("gyrfalcon.flow.registry:duplicate_source")
        name, _ = registered_source_flow
        with pytest.raises(ValueError, match="already exists"):
            duplicate_source(name, name)

    def test_duplicate_of_unknown_definition_raises(self):
        duplicate_source = sym("gyrfalcon.flow.registry:duplicate_source")
        with pytest.raises(ValueError, match="No definition"):
            duplicate_source("no-such-flow", "whatever")


class TestRunDefinitionNow:
    def test_run_definition_now_executes_with_given_parameters(
        self, registered_source_flow, run_store
    ):
        run_definition_now = sym("gyrfalcon.flow.registry:run_definition_now")
        name, _ = registered_source_flow

        run_id = run_definition_now(name, {"who": "direct"})
        assert run_id

        for _ in range(50):
            run = run_store.get_run(run_id)
            if run and run["is_final"]:
                break
            time.sleep(0.02)
        assert run["state_type"] == "COMPLETED"
        assert run["result"] == "pong: direct"

    def test_run_definition_now_on_unknown_definition_raises(self, run_store):
        run_definition_now = sym("gyrfalcon.flow.registry:run_definition_now")
        with pytest.raises(ValueError, match="No definition"):
            run_definition_now("no-such-flow")

    def test_run_definition_now_has_no_concurrency_limit(
        self, registered_source_flow, run_store
    ):
        """Unlike a deployment, a direct run has nothing to carry a
        concurrency_limit — it must not be blocked by another run of the
        same flow already active."""
        states = sym("gyrfalcon.flow.states")
        run_definition_now = sym("gyrfalcon.flow.registry:run_definition_now")
        name, _ = registered_source_flow

        run_store.create_run("already-active", name, "flow")
        run_store.record_transition("already-active", states.Running())

        run_id = run_definition_now(name, {"who": "second"})
        assert run_id and run_id != "already-active"
