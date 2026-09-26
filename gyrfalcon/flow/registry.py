"""Definition registry — the code-first analogue of a stored flow definition.

A `@flow`-decorated function is itself the definition; this module is just an
index of the ones that have been imported, so the API and UI have something to
list without a separate authoring format.

`discover_flows()` is how a file becomes known to a process that didn't write
it inline: drop a `.py` file in `~/.gyrfalcon/flows/` and any `@flow`/`@activity`
in it registers itself on import — no manifest, no `register()` call, mirroring
`get_skills_dir()` exactly. This is deliberately **not** the plugin system
(`gyrfalcon/plugins.py`): plugins register new capabilities *with* the agent
(tools, hooks, CLI commands) and need a manifest because the loader has to
know what kind of thing it received. A flow needs nothing from a loader — the
decorator already is the complete definition — so requiring plugin ceremony
around it would be borrowing a mechanism built for a different problem.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from gyrfalcon.flow.templates import Activity, Flow

logger = logging.getLogger("gyrfalcon.flow.registry")

_REGISTRY: dict[str, Flow] = {}
_ACTIVITIES: dict[tuple[str, str], Activity] = {}
_LOCK = threading.Lock()

#: Files that failed to import on the last `discover_flows()`, keyed by
#: filename. Kept rather than only logged: a flow file that raises on import is
#: otherwise indistinguishable from one that was never added — the list is just
#: silently short, which is a genuinely confusing thing to debug from the UI.
_IMPORT_ERRORS: dict[str, str] = {}


def register(template: Flow) -> None:
    with _LOCK:
        _REGISTRY[template.name] = template


def register_activity(template: Activity) -> None:
    if template.version is None:
        return
    with _LOCK:
        _ACTIVITIES[(template.name, template.version)] = template


def get_activity(name: str, version: str) -> Activity | None:
    with _LOCK:
        return _ACTIVITIES.get((name, version))


def list_activities() -> list[dict[str, str]]:
    with _LOCK:
        return [
            {"name": name, "version": version}
            for name, version in sorted(_ACTIVITIES)
        ]


def get_definition(name: str) -> Flow | None:
    with _LOCK:
        return _REGISTRY.get(name)


def list_definitions() -> list[dict[str, Any]]:
    with _LOCK:
        templates = list(_REGISTRY.values())
    return [
        {
            "name": t.name,
            "version": t.version,
            "description": t.description,
            "retries": t.retries,
            "timeout_seconds": t.timeout_seconds,
            "tags": list(t.tags),
            "editable": _editable_source_path(t, raise_on_ineligible=False) is not None,
        }
        for t in templates
    ]


def _import_single_file(path: Path) -> None:
    """The per-file half of `discover_flows`, factored out so a single edited
    or duplicated file can be re-imported without walking the whole
    directory — `save_source` and `duplicate_source` both need exactly this,
    not a second implementation of it."""
    spec = importlib.util.spec_from_file_location(
        f"gyrfalcon_user_flow_{path.stem}", str(path)
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load {path.name}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec, matching what a real `import` does: code run
    # during exec_module can rely on `sys.modules[__name__]` being present
    # (e.g. a `from __future__ import annotations` dataclass resolves its
    # stringized field types via `sys.modules[cls.__module__].__dict__`,
    # which raises AttributeError on None if this step is skipped).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise


def discover_flows(flows_dir: Optional[Path] = None) -> int:
    """Import every .py file in the flows directory. Returns the count imported.

    Each file is loaded standalone (not as part of a package) since these are
    arbitrary user files on disk, not something shipped inside gyrfalcon —
    there is no "frozen bundle" case to handle here, unlike tool discovery.
    """
    if flows_dir is None:
        from gyrfalcon.gyrfalcon_constants import get_flows_dir
        flows_dir = get_flows_dir()

    if not flows_dir.exists():
        return 0

    imported = 0
    errors: dict[str, str] = {}
    for py_file in sorted(flows_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            _import_single_file(py_file)
            imported += 1
        except Exception as e:
            # `repr` rather than `str`: a bare ModuleNotFoundError stringifies
            # to "No module named 'x'" with no hint of what kind of failure it
            # was, and that distinction is the whole value of showing it.
            errors[py_file.name] = f"{type(e).__name__}: {e}"
            logger.warning(f"Failed to import flow file {py_file.name}: {e}", exc_info=True)

    with _LOCK:
        _IMPORT_ERRORS.clear()
        _IMPORT_ERRORS.update(errors)
    return imported


def get_import_errors() -> dict[str, str]:
    """Filename → error, from the most recent `discover_flows()`."""
    with _LOCK:
        return dict(_IMPORT_ERRORS)


# ══════════════════════════════════════════════════════════════════════════
# Dashboard actions on a Definition — spec §14.9 (edit), §14.12 (run/delete/
# duplicate). Scoped deliberately narrow: only a flow whose source lives
# under `get_flows_dir()` is mutable from here. A flow shipped inside
# gyrfalcon itself, or defined inline in a REPL/notebook with no backing
# file, has no honest "save" to offer — `inspect.getsourcefile` returns
# None or a path outside the flows dir, and every action below refuses
# rather than silently doing nothing or writing somewhere unexpected.
# ══════════════════════════════════════════════════════════════════════════

def _editable_source_path(template: Flow, raise_on_ineligible: bool = True) -> Optional[Path]:
    import inspect

    from gyrfalcon.gyrfalcon_constants import get_flows_dir

    try:
        raw = inspect.getsourcefile(template.fn)
    except TypeError:
        raw = None

    if not raw:
        if raise_on_ineligible:
            raise ValueError(
                f"{template.name!r} has no editable source file — it was "
                f"defined inline, not loaded from a file."
            )
        return None

    path = Path(raw).resolve()
    flows_dir = get_flows_dir().resolve()
    if path.parent != flows_dir:
        if raise_on_ineligible:
            raise ValueError(
                f"{template.name!r} is not a user-authored flow (its source "
                f"is outside {flows_dir}) and cannot be edited from the "
                f"dashboard."
            )
        return None
    return path


def get_source(name: str) -> dict:
    """The pencil button's "open": the file text plus its path, for the
    editor §14.9 describes."""
    template = get_definition(name)
    if template is None:
        raise ValueError(f"No definition named {name!r}")
    path = _editable_source_path(template)
    return {"path": str(path), "content": path.read_text(encoding="utf-8")}


def save_source(name: str, content: str) -> None:
    """The pencil button's "save": write the file, then re-import it through
    the same path `discover_flows` uses — not a second validator.

    A save that breaks the import is rolled back rather than left half
    applied: the previous content is restored and re-imported, so a bad
    edit leaves both the file on disk and the in-memory registry exactly
    where they were, never a working registry pointing at a broken file or
    a broken file with no registered flow behind it.

    Renaming the flow through an edit — changing the `name=` a decorator
    declares — is refused for the same reason §14.10 refuses re-pointing a
    deployment at a different flow: this endpoint is addressed by name, and
    "edit" changing what that name means out from under any deployment or
    running instance that references it is a different operation
    (duplicate-then-delete) wearing an edit's clothing.

    Detecting that requires popping `name` out of the registry *before* the
    new content is imported — a stale success (the old registration just
    sitting there, untouched, because nothing new claimed that key) would
    otherwise look identical to the file still defining it. A rename does
    leave one stray entry behind under its new name until the next full
    `discover_flows()`; refusing the save immediately is the fix that
    matters, and cleaning up a name nobody asked for and the save already
    rejected is not.
    """
    template = get_definition(name)
    if template is None:
        raise ValueError(f"No definition named {name!r}")
    path = _editable_source_path(template)
    previous = path.read_text(encoding="utf-8")

    with _LOCK:
        _REGISTRY.pop(name, None)

    path.write_text(content, encoding="utf-8")
    try:
        _import_single_file(path)
        if get_definition(name) is None:
            raise ValueError(
                f"The edited file no longer defines a flow named {name!r} "
                f"— renaming through this editor isn't supported. "
                f"Duplicate under the new name instead."
            )
    except Exception as e:
        path.write_text(previous, encoding="utf-8")
        try:
            _import_single_file(path)  # restore the registry to match disk
        except Exception:
            logger.error(f"Could not re-import {path.name} after rolling back a failed save")
        if get_definition(name) is None:
            # Last resort: the restored file failed to re-register name too
            # (disk race, or a plugin importing the file elsewhere) — put the
            # known-good template straight back rather than leave a gap where
            # a working definition existed a moment ago.
            with _LOCK:
                _REGISTRY[name] = template
        raise ValueError(str(e)) from e


def delete_source(name: str) -> None:
    """Deletes the backing file and unregisters the flow. Any deployment
    still naming it degrades the way an unregistered flow already does
    elsewhere — `Runner.tick()` logs and skips it, `Runner.run_now()` raises
    a clear error — so nothing new has to be taught to that path; the REST
    layer surfaces the affected deployment count so deleting isn't a
    surprise later."""
    template = get_definition(name)
    if template is None:
        raise ValueError(f"No definition named {name!r}")
    path = _editable_source_path(template)
    path.unlink(missing_ok=True)
    with _LOCK:
        _REGISTRY.pop(name, None)


def duplicate_source(name: str, new_name: str) -> Path:
    """Copies the definition into a new file under a new name.

    Deliberately does not parse or rewrite the `@flow(...)` decorator —
    text-editing an arbitrary Python decorator call is exactly the kind of
    "mostly works" mechanism that breaks on the one file shaped differently
    than expected. Instead it reuses `Flow.with_options()` (§1.2's own
    "callers reconfigure without redefining" API), appending one line that
    rebinds the module-level flow to a copy with the new name — the same
    idiom a person would type by hand, not string surgery on their source.

    The copied file still carries the *original* decorator, `name=` and
    all — that's what "does not rewrite the decorator" means — so importing
    it re-executes `@flow(name=<old name>)` before the footer's rename ever
    runs, and that re-registers `<old name>` under the *copy's* function
    object for the moment in between. Left alone, that clobbers the
    original's registry entry — its `inspect.getsourcefile` would resolve
    to the copy's path, not its own, silently breaking Edit/Delete on the
    original the next time either runs. So the original's template is
    saved before importing the copy and force-restored after, regardless
    of what the copy's own decorator did to that key along the way.
    """
    template = get_definition(name)
    if template is None:
        raise ValueError(f"No definition named {name!r}")
    src_path = _editable_source_path(template)

    if get_definition(new_name) is not None:
        raise ValueError(f"A definition named {new_name!r} already exists.")
    dest_path = src_path.parent / f"{new_name}.py"
    if dest_path.exists():
        raise ValueError(f"{dest_path.name} already exists in the flows directory.")

    original_source = src_path.read_text(encoding="utf-8")
    fn_identifier = template.fn.__name__
    footer = (
        f"\n\n# --- gyrfalcon: duplicated from {name!r} as {new_name!r} ---\n"
        f"{fn_identifier} = {fn_identifier}.with_options(name={new_name!r})\n"
    )
    dest_path.write_text(original_source + footer, encoding="utf-8")
    try:
        _import_single_file(dest_path)
    except Exception as e:
        dest_path.unlink(missing_ok=True)
        raise ValueError(f"Could not import the duplicate: {e}") from e
    finally:
        with _LOCK:
            _REGISTRY[name] = template  # see docstring: undo the transient clobber
    if get_definition(new_name) is None:
        dest_path.unlink(missing_ok=True)
        raise ValueError(
            f"Duplicated file imported but did not register {new_name!r} — "
            f"the original may define more than one flow."
        )
    return dest_path


def run_definition_now(name: str, parameters: Optional[dict] = None) -> str:
    """Run a definition directly, with no deployment involved — the
    Definitions page's own Run button (§14.12), for trying a flow with
    ad-hoc parameters rather than through a saved, scheduled binding.

    Mirrors `Runner.run_now()`'s shape deliberately: reserve a run slot
    (unbounded — there is no deployment to carry a concurrency limit here),
    launch on its own thread via `contextvars.copy_context()` so the calling
    principal's identity survives the thread boundary (the same rule
    CLAUDE.md calls out as this codebase's recurring bug class), and return
    the run id rather than the result — a flow run is not assumed to be
    quick just because nobody scheduled it.
    """
    import contextvars
    import threading

    from gyrfalcon.flow.store import get_store

    template = get_definition(name)
    if template is None:
        raise ValueError(f"No definition named {name!r}")

    params = dict(parameters or {})
    run_id = get_store().reserve_run_slot(
        name, None, parameters={k: repr(v) for k, v in params.items()}, tags=[],
    )
    if run_id is None:
        # Unreachable with limit=None, kept so a future change to
        # reserve_run_slot's contract fails loudly here rather than
        # returning None as if it were a run id.
        raise ValueError(f"Could not reserve a run slot for {name!r}")

    def _run() -> None:
        try:
            template(**params, return_type="state", run_id=run_id)
        except Exception:
            logger.error(f"Manual run of {name!r} raised", exc_info=True)

    ctx = contextvars.copy_context()
    threading.Thread(
        target=lambda: ctx.run(_run), daemon=True, name=f"definition-run-{name[:20]}",
    ).start()
    return run_id
