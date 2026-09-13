"""Built-in tools must load however the app is packaged.

Regression: discovery globbed gyrfalcon/tools/*.py off disk. Inside a PyInstaller
bundle those sources live in the archive, so the glob found nothing, the registry
stayed empty, and the agent reported "only the `memory` tool is exposed to me".
"""


import pytest

import gyrfalcon.model_tools as mt


@pytest.fixture(autouse=True)
def reset_discovery():
    original = mt._discovered
    mt._discovered = False
    yield
    mt._discovered = original


def _tool_names():
    return {d.get("function", d).get("name") for d in mt.get_tool_definitions()}


class TestDiscovery:
    def test_source_tree_discovery(self):
        mt.discover_builtin_tools()
        names = _tool_names()
        assert "terminal" in names
        assert len(names) >= 10

    def test_falls_back_when_sources_are_not_on_disk(self, tmp_path):
        """Simulates the frozen bundle: the directory has no .py files."""
        missing = tmp_path / "not-a-real-tools-dir"
        mt.discover_builtin_tools(tools_dir=missing)

        names = _tool_names()
        assert "terminal" in names, "package fallback did not import tool modules"
        assert len(names) >= 10

    def test_empty_dir_still_falls_back(self, tmp_path):
        empty = tmp_path / "tools"
        empty.mkdir()
        mt.discover_builtin_tools(tools_dir=empty)
        assert "terminal" in _tool_names()

    def test_package_discovery_imports_modules(self):
        assert mt._discover_from_package() > 0

    def test_logs_when_nothing_imported(self, monkeypatch, caplog, tmp_path):
        monkeypatch.setattr(mt, "_discover_from_package", lambda: 0)
        with caplog.at_level("ERROR"):
            mt.discover_builtin_tools(tools_dir=tmp_path / "nope")
        assert any("no tools" in r.message.lower() or "No built-in tool modules" in r.message
                   for r in caplog.records)
