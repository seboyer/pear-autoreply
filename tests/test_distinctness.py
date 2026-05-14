"""Distinctness invariant: production must not import the harness.

The repo holds two products that share parsers/matchers/templates by design
but are separately wired:

  - autoreplies.*              — production autoreply pipeline (PLAN.md)
  - autoreplies.harness.*      — testing harness side-car (TESTING_HARNESS_PLAN.md)

The harness depends on production code; production never depends on the
harness. This test walks every production module and asserts no harness
module ends up in `sys.modules` as a side effect.
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
