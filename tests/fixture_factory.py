"""Test access to the deterministic synthetic Confluence export fixture."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "full-export"
BUILDER_PATH = PROJECT_ROOT / "scripts" / "build_synthetic_export.py"


def _load_builder() -> ModuleType:
    module_name = "confluence_synthetic_fixture_builder"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fixture builder: {BUILDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()


def expected_artifacts() -> dict[str, bytes]:
    return BUILDER.build_artifacts()


def materialized_paths() -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(FIXTURE_ROOT).as_posix()
            for path in FIXTURE_ROOT.rglob("*")
            if path.is_file()
        )
    )
