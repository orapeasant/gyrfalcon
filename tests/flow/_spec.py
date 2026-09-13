"""Spec-test helpers: skip-until-implemented and late symbol resolution.

Kept out of conftest.py so test modules can import it directly (pytest puts the
test directory on sys.path, so a plain absolute import works without a package).
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest


def requires(*symbols: str, section: str = ""):
    """Skip a module unless every `module:attr` symbol is importable.

    Usage at module top:
        pytestmark = requires("gyrfalcon.flow.states:State", section="§1.3")
    """
    missing: list[str] = []
    for spec in symbols:
        mod_name, _, attr = spec.partition(":")
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            missing.append(spec)
            continue
        if attr and not hasattr(mod, attr):
            missing.append(spec)

    reason = f"not implemented yet: {', '.join(missing)}"
    if section:
        reason += f" (spec {section})"
    return pytest.mark.skipif(bool(missing), reason=reason)


def sym(path: str) -> Any:
    """Resolve `module:attr` at call time, so collection never hard-fails."""
    mod_name, _, attr = path.partition(":")
    mod = importlib.import_module(mod_name)
    return getattr(mod, attr) if attr else mod
