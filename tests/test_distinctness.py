"""Distinctness invariant: boundaries between production, harness, and workers.

The repo holds two products that share parsers/matchers/templates by design
but are separately wired:

  - autoreplies.*              — production autoreply pipeline (PLAN.md)
  - autoreplies.harness.*      — testing harness side-car (TESTING_HARNESS_PLAN.md)
  - autoreplies.workers.*      — production poller + send worker

Invariants enforced here:
1. Production must not import the harness (unchanged).
2. Harness must not import workers.poller (the boundary now runs both directions —
   harness keeps running alongside the new production poller during migration).
"""

import importlib
import pkgutil
import sys
import types
from collections.abc import Iterator

import autoreplies


def _iter_production_modules(pkg: types.ModuleType) -> Iterator[str]:
    """Yield all production module names, never descending into autoreplies.harness.

    pkgutil.walk_packages imports subpackages to enumerate their contents, which
    would leak autoreplies.harness into sys.modules even when we skip it. This
    manual recursive walk avoids importing the harness namespace entirely.
    """
    for module_info in pkgutil.iter_modules(pkg.__path__, prefix=pkg.__name__ + "."):
        if module_info.name.startswith("autoreplies.harness"):
            continue
        yield module_info.name
        if module_info.ispkg:
            subpkg = importlib.import_module(module_info.name)
            yield from _iter_production_modules(subpkg)


def _iter_harness_modules(pkg: types.ModuleType) -> Iterator[str]:
    """Yield all harness module names, skipping __main__ (it calls sys.exit on import)."""
    for module_info in pkgutil.iter_modules(pkg.__path__, prefix=pkg.__name__ + "."):
        if not module_info.name.startswith("autoreplies.harness"):
            continue
        if module_info.name.endswith(".__main__"):
            continue
        yield module_info.name
        if module_info.ispkg:
            subpkg = importlib.import_module(module_info.name)
            yield from _iter_harness_modules(subpkg)


def test_production_does_not_import_harness() -> None:
    # Start clean: drop any harness modules a previous test may have loaded.
    for name in list(sys.modules):
        if name.startswith("autoreplies.harness"):
            del sys.modules[name]

    for module_name in _iter_production_modules(autoreplies):
        importlib.import_module(module_name)

    leaked = sorted(name for name in sys.modules if name.startswith("autoreplies.harness"))
    assert leaked == [], (
        "Production code transitively imported the harness — distinctness violated. "
        f"Leaked modules: {leaked}"
    )


def test_harness_does_not_import_workers_poller() -> None:
    """Harness must not import the production workers.poller module.

    Both run in parallel during the migration window; pulling in the production
    poller from the harness would create a coupling that complicates future cleanup.
    """
    workers_poller_prefix = "autoreplies.workers.poller"
    # Drop any poller modules a previous test may have loaded.
    for name in list(sys.modules):
        if name.startswith(workers_poller_prefix):
            del sys.modules[name]

    harness_pkg = importlib.import_module("autoreplies.harness")
    for module_name in _iter_harness_modules(harness_pkg):
        importlib.import_module(module_name)

    leaked = sorted(
        name for name in sys.modules if name.startswith(workers_poller_prefix)
    )
    assert leaked == [], (
        "Harness code transitively imported workers.poller — boundary violated. "
        f"Leaked modules: {leaked}"
    )
