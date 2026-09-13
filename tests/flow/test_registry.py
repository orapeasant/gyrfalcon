"""discover_flows() — the manifest-free way a flow file becomes known to a
process that didn't define it inline.

Deliberately not the plugin system: a flow is a complete definition the moment
it's decorated, with nothing to register with a loader, so it gets its own
directory (mirroring get_skills_dir()) instead of riding on plugin ceremony.
"""

from __future__ import annotations

from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.registry:discover_flows", section="registry")


class TestDiscoverFlows:
    def test_imports_a_flow_file_and_registers_its_flows(self, tmp_path):
        discover_flows = sym("gyrfalcon.flow.registry:discover_flows")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")

        (tmp_path / "my_flows.py").write_text(
            "from gyrfalcon.flow import flow\n\n"
            "@flow\n"
            "def discovered_flow_a(x=1):\n"
            "    return x\n"
        )

        count = discover_flows(tmp_path)
        assert count == 1
        assert get_definition("discovered_flow_a") is not None

    def test_multiple_files_are_all_imported(self, tmp_path):
        discover_flows = sym("gyrfalcon.flow.registry:discover_flows")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")

        (tmp_path / "a.py").write_text(
            "from gyrfalcon.flow import flow\n\n"
            "@flow\ndef discovered_flow_b():\n    return 1\n"
        )
        (tmp_path / "b.py").write_text(
            "from gyrfalcon.flow import flow\n\n"
            "@flow\ndef discovered_flow_c():\n    return 2\n"
        )

        count = discover_flows(tmp_path)
        assert count == 2
        assert get_definition("discovered_flow_b") is not None
        assert get_definition("discovered_flow_c") is not None

    def test_no_manifest_or_register_function_required(self, tmp_path):
        """The whole point: unlike a plugin, nothing beyond the decorator."""
        discover_flows = sym("gyrfalcon.flow.registry:discover_flows")

        assert not (tmp_path / "plugin.yaml").exists()
        (tmp_path / "plain.py").write_text(
            "from gyrfalcon.flow import flow\n\n"
            "@flow\ndef discovered_flow_d():\n    return 1\n"
        )
        assert discover_flows(tmp_path) == 1

    def test_underscore_prefixed_files_are_skipped(self, tmp_path):
        discover_flows = sym("gyrfalcon.flow.registry:discover_flows")

        (tmp_path / "_helpers.py").write_text("raise RuntimeError('must not import')")
        assert discover_flows(tmp_path) == 0

    def test_missing_directory_returns_zero_not_an_error(self, tmp_path):
        discover_flows = sym("gyrfalcon.flow.registry:discover_flows")
        assert discover_flows(tmp_path / "does-not-exist") == 0

    def test_a_broken_flow_file_does_not_block_the_others(self, tmp_path):
        discover_flows = sym("gyrfalcon.flow.registry:discover_flows")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")

        (tmp_path / "broken.py").write_text("this is not valid python (((")
        (tmp_path / "good.py").write_text(
            "from gyrfalcon.flow import flow\n\n"
            "@flow\ndef discovered_flow_e():\n    return 1\n"
        )

        count = discover_flows(tmp_path)
        assert count == 1
        assert get_definition("discovered_flow_e") is not None

    def test_default_directory_is_gyrfalcon_home_flows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "gyrfalcon.gyrfalcon_constants.get_gyrfalcon_home", lambda: tmp_path
        )
        get_flows_dir = sym("gyrfalcon.gyrfalcon_constants:get_flows_dir")
        assert get_flows_dir() == tmp_path / "flows"
        assert (tmp_path / "flows").is_dir()

    def test_discovered_flow_actually_runs(self, tmp_path):
        """Not just registered — callable, exactly like one defined inline."""
        discover_flows = sym("gyrfalcon.flow.registry:discover_flows")
        get_definition = sym("gyrfalcon.flow.registry:get_definition")

        (tmp_path / "runnable.py").write_text(
            "from gyrfalcon.flow import flow, task\n\n"
            "@task(retries=1)\n"
            "def discovered_task(x):\n    return x * 2\n\n"
            "@flow\n"
            "def discovered_flow_f(n=3):\n    return discovered_task(n)\n"
        )
        discover_flows(tmp_path)
        template = get_definition("discovered_flow_f")
        assert template(5) == 10
