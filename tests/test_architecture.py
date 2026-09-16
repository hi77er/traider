"""Architecture invariants — the things that must stay true as the code grows.

Everything here is about the shape of the project rather than its behaviour, and
each test exists because the failure it prevents is silent. A layering inversion,
a second importer of the trading logic, or a loop that ends up owned by the web
server would all be *invisible* until they hurt: they do not raise, they do not
fail a request, and no functional test notices them.

The import graph is read statically (``ast``), never by importing the modules, so
these tests cannot be fooled by import order and cannot cause side effects.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

from src import main as loop_host
from src.web import app as web_app

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _modules() -> Iterator[Tuple[str, Path, bool]]:
    """Every module under ``src`` as ``(dotted_name, path, is_package)``."""
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        yield ".".join(parts), path, is_package


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_names(name: str, is_package: bool, tree: ast.Module) -> Set[str]:
    """The dotted names this module imports, with relatives resolved.

    Relative imports are resolved rather than skipped: this codebase imports
    absolutely almost everywhere, so ``from ..web import x`` reaching out of the
    execution layer is exactly the kind of thing that would otherwise slip past a
    naive scan.
    """
    package = name if is_package else ".".join(name.split(".")[:-1])
    package_parts = package.split(".") if package else []

    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package_parts) - node.level + 1
                if keep < 0:
                    continue
                base = package_parts[:keep]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target:
                names.add(target)
                names.update(f"{target}.{alias.name}" for alias in node.names)
    return names


def _project_imports() -> Dict[str, Set[str]]:
    """``module -> the src.* names it imports``, for every module under ``src``."""
    graph: Dict[str, Set[str]] = {}
    for name, path, is_package in _modules():
        imported = _imported_names(name, is_package, _tree(path))
        graph[name] = {n for n in imported if n == "src" or n.startswith("src.")}
    return graph


def _importers_of(package: str, graph: Dict[str, Set[str]]) -> Set[str]:
    """Modules outside ``package`` that import it or anything beneath it."""
    return {
        module
        for module, imported in graph.items()
        if not module.startswith(package + ".")
        and package not in (module,)
        and any(name == package or name.startswith(package + ".") for name in imported)
    }


GRAPH = _project_imports()


# --- the two processes ------------------------------------------------------


def test_the_web_layer_never_reaches_for_the_loop() -> None:
    """The dashboard must have no path to the loop: not the scheduler, not the host.

    The dashboard is the window you debug a broken loop through, so it has to
    survive one. The moment a route can start the loop, a crashed route and a
    crashed bot become the same event — and you lose the view you needed.
    """
    offenders = {
        module: sorted(
            n for n in GRAPH[module] if n in ("src.main", "src.scheduler") or n.startswith("src.scheduler.")
        )
        for module in GRAPH
        if module.startswith("src.web.")
    }
    offenders = {m: v for m, v in offenders.items() if v}
    assert offenders == {}, f"the web layer imports the loop: {offenders}"


def test_nothing_outside_the_web_layer_imports_the_web_layer() -> None:
    """The dependency arrow points ``execution -> config <- web``, never ``-> web``.

    ``src/execution`` used to import ``src/web/services/trading_service`` to read the
    trading switch, which meant the trading process had to load FastAPI and every
    route module to answer "am I allowed to trade?". The switch state now lives in
    ``src/config/trading_state``; this is what keeps it there.
    """
    offenders = sorted(_importers_of("src.web", GRAPH))
    assert offenders == [], f"these non-web modules import the web layer: {offenders}"


def test_the_web_app_registers_no_startup_hook() -> None:
    """No ``lifespan``/``on_event`` on the app — the dashboard owns no background work.

    Not a style rule: a startup hook is the natural place someone would later start
    the loop "conveniently", which is the one thing that must not happen here.
    """
    tree = _tree(Path(web_app.__file__))
    assert "lifespan" not in ast.dump(tree), "the web app declares a lifespan hook"
    registered = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "on_event"
    ]
    assert registered == [], "the web app registers an on_event (startup/shutdown) hook"


def test_the_switch_state_module_stays_neutral() -> None:
    """``trading_state`` may import ``src.config`` and nothing else from the project.

    It is the module that lets the loop and the dashboard share one fact without
    sharing a layer. One import of anything heavier re-creates the inversion this
    module was created to remove, and nothing else would notice.
    """
    imports = GRAPH["src.config.trading_state"]
    allowed = {n for n in imports if n == "src.config" or n.startswith("src.config.")}
    assert imports == allowed, f"trading_state must only import src.config, found: {sorted(imports - allowed)}"


# --- the shared machine -----------------------------------------------------


def test_the_shared_machine_has_exactly_two_drivers() -> None:
    """Only the two drivers may import ``src.strategy`` from outside the package.

    ``4880eaf`` collapsed the backtest and live paths into ONE machine precisely
    because a second copy is how the two silently drift apart. A third importer
    outside this allow-list means someone has started building that second copy.
    """
    drivers = {"src.backtest.risk_sim", "src.execution.alpaca_broker"}
    # The loop host will drive the machine too (via LiveDriver) — allowed, and the
    # reason this is a set rather than an equality check.
    allowed_hosts = {"src.main"} | {m for m in GRAPH if m.startswith("src.scheduler.")}

    importers = _importers_of("src.strategy", GRAPH)
    assert drivers <= importers, "the shared machine is no longer wired into both drivers"
    assert importers <= (drivers | allowed_hosts), (
        f"unexpected importers of the shared strategy machine: {sorted(importers - drivers - allowed_hosts)}"
    )


def test_the_backtest_is_the_web_processs_business() -> None:
    """The loop never backtests; the backtest stays reachable only from the web layer.

    Pinning the direction keeps the loop's import surface small, which is what makes
    it startable without the dashboard's dependencies.
    """
    importers = _importers_of("src.backtest", GRAPH)
    assert importers, "nothing imports the backtest any more — did it move?"
    assert all(m.startswith("src.web.") for m in importers), (
        f"non-web modules import the backtest: {sorted(m for m in importers if not m.startswith('src.web.'))}"
    )


# --- the host's own guard ---------------------------------------------------


def test_the_loop_refuses_to_be_imported_instead_of_run(monkeypatch) -> None:
    monkeypatch.setattr(loop_host, "__name__", "src.main")
    refusal = loop_host.check_host()
    assert refusal is not None
    assert "imported" in refusal


def test_the_loop_refuses_to_share_a_process_with_the_dashboard(monkeypatch) -> None:
    """The invariant, enforced at runtime rather than only asserted in a document."""
    monkeypatch.setattr(loop_host, "__name__", "__main__")
    monkeypatch.setitem(sys.modules, "src.web.app", web_app)
    refusal = loop_host.check_host()
    assert refusal is not None
    assert "separately" in refusal
    assert loop_host.main() == loop_host.EXIT_WRONG_HOST


def test_the_loop_starts_cleanly_when_it_is_alone(monkeypatch) -> None:
    monkeypatch.setattr(loop_host, "__name__", "__main__")
    monkeypatch.delitem(sys.modules, "src.web.app", raising=False)
    assert loop_host.check_host() is None


def test_the_loop_host_never_pretends_to_be_running(monkeypatch) -> None:
    """Until Phase 6 exists, it must not exit 0.

    A process that reports success while trading nothing is the same lie the
    trading switch refuses to tell — it looks like it works.
    """
    monkeypatch.setattr(loop_host, "__name__", "__main__")
    monkeypatch.delitem(sys.modules, "src.web.app", raising=False)
    assert loop_host.main() != 0
