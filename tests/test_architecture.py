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


def test_the_artefact_helpers_are_below_every_layer() -> None:
    """``src/config/artifacts`` imports nothing from the project, at all.

    Two output trees share it — backtest runs and, from Phase 3, live results — and the
    whole reason it exists is that ``src/execution`` must not learn these helpers by
    importing ``src/backtest``. Any project import here re-creates that coupling one
    level down, where it is harder to see.
    """
    assert GRAPH["src.config.artifacts"] == set(), (
        f"artifacts.py must stay dependency-free, found: {sorted(GRAPH['src.config.artifacts'])}"
    )


def test_the_output_helpers_are_the_same_objects_everywhere() -> None:
    """Both stores must call the SAME functions, not equal-looking copies.

    ``src/backtest/store.py`` re-exports them for its existing callers, which makes it easy
    to "helpfully" re-add a local definition later. Two implementations would then be free
    to disagree about what a slug or a timestamp looks like on disk — and the disagreement
    would only show up as two strategies writing to two directories.
    """
    from src.backtest import store
    from src.config import artifacts

    for name in ("slug", "jsonable", "write_json_atomic"):
        assert getattr(store, name) is getattr(artifacts, name), f"{name} is a second copy"


# --- the live store ---------------------------------------------------------


def test_the_live_store_does_not_depend_on_the_backtester() -> None:
    """``src/execution/store.py`` imports nothing from ``src/backtest``.

    The trading process must not need the backtester: the loop runs on a machine that has
    no reason to have run a backtest, and the dependency is easy to acquire by accident
    because the two trees look alike and share their helpers (which is what
    ``src/config/artifacts.py`` is for).
    """
    imports = GRAPH["src.execution.store"]
    offenders = sorted(n for n in imports if n == "src.backtest" or n.startswith("src.backtest."))
    assert offenders == [], f"the live store imports the backtester: {offenders}"


#: What only the loop may do. Names, not receivers — a wrapper named the same would be
#: just as wrong, and the store is the only module that should have these at all.
_STORE_WRITES = frozenset(
    {"append_line", "append_order", "append_tick", "append_trade", "retire_legacy_state",
     "save_latest", "upsert_day"}
)


def test_the_dashboard_never_writes_the_live_store() -> None:
    """The loop writes the live tree; the dashboard only reads it.

    Not a style rule. These files are the loop's memory — the last tick and the position it
    believes it has — and a second writer is how the dashboard and the bot end up
    disagreeing about what happened, with the log as the only evidence and no way to tell
    which of them was right. The read side is deliberately NOT restricted: rendering the
    panel needs the same paths.
    """
    offenders = []
    for name, path, _is_package in _modules():
        if not name.startswith("src.web."):
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr in _STORE_WRITES:
                offenders.append(f"{name}:{node.lineno} .{node.attr}")
    assert offenders == [], f"the web layer writes the live store: {offenders}"


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


# --- the loop ---------------------------------------------------------------


def test_only_the_host_may_import_the_loop() -> None:
    """Nothing but ``src/main.py`` may reach ``src/scheduler``.

    The loop moves money, so the ways INTO it are the security surface. A web route that
    could call a tick would put an order behind an HTTP request; a script that imported it
    would be a second, unleased writer of the live tree. ``src.main`` is the host: it holds
    the lease, owns the wake schedule and is the only entry point a deployment starts.

    Stated as a subset rather than an equality on purpose — the host does not import the
    loop until Phase 5 wires it, and an empty set is the correct answer until then.
    """
    offenders = sorted(_importers_of("src.scheduler", GRAPH) - {"src.main"})
    assert offenders == [], f"these modules drive the loop besides the host: {offenders}"


def test_the_backtester_cannot_reach_the_live_side() -> None:
    """``src/backtest`` must not depend on the loop or the live store.

    A backtest is a pure function of the dataset and the rules. The moment it needs the
    loop's tree it stops being reproducible from the data alone — and the trading machine
    would have to carry the backtester's dependencies to place an order.
    """
    offenders = sorted(
        f"{module} -> {name}"
        for module, imported in GRAPH.items()
        if module.startswith("src.backtest.")
        for name in imported
        for package in ("src.scheduler", "src.execution.store")
        if name == package or name.startswith(package + ".")
    )
    assert offenders == [], f"the backtester reaches the live side: {offenders}"


def test_the_bar_grid_stays_below_both_processes() -> None:
    """``src/data`` may not know about the loop or the execution layer.

    The bar grid answers "which bar has closed" for the chart, the backtest and the loop
    alike. Teaching it about any one of its callers is how the three start disagreeing about
    which bars exist — the failure it was written to prevent.
    """
    imports = GRAPH["src.data.dataset"]
    offenders = sorted(
        n for n in imports if n.startswith(("src.scheduler", "src.execution", "src.web"))
    )
    assert offenders == [], f"src.data.dataset reaches the live side: {offenders}"


def test_the_lease_reader_is_below_both_processes() -> None:
    """``src/config/loop_state.py`` may import ``src.config`` and nothing else.

    It exists so the DASHBOARD can read the loop's lease — is a bot running, and when does
    it next intend to act — without importing ``src.scheduler``, which the first test in
    this section forbids outright and for good reason: the dashboard has to survive a broken
    loop. One import of anything heavier here re-creates that path, and the failure would
    only show up as a dashboard that cannot start because the loop will not.
    """
    imports = GRAPH["src.config.loop_state"]
    allowed = {n for n in imports if n == "src.config" or n.startswith("src.config.")}
    assert imports == allowed, f"loop_state must only import src.config, found: {sorted(imports - allowed)}"


def test_the_loop_builds_its_claim_on_the_neutral_reader() -> None:
    """The write side imports the read side, and keeps no copy of it.

    The loop reaches the reader *through* the writer (it holds a ``Lease`` and refreshes it),
    so the thing to pin is the direction of the arrow plus the absence of a second
    implementation: a ``holder()`` re-written inside the loop package would compile, pass
    every behavioural test, and be free to drift from the one the dashboard reads.
    """
    assert "src.config.loop_state" in GRAPH["src.scheduler.lease"]
    assert "src.scheduler.lease" in GRAPH["src.scheduler.orchestrator"], (
        "the loop refreshes its claim through the writer"
    )
    leaks = sorted(
        n for n in GRAPH["src.config.loop_state"]
        if n.startswith(("src.scheduler", "src.execution", "src.strategy", "src.web"))
    )
    assert leaks == [], f"the reader reaches back into a process: {leaks}"

    paths = {name: path for name, path, _is_package in _modules()}
    defined = {
        node.name
        for node in ast.walk(_tree(paths["src.scheduler.lease"]))
        if isinstance(node, ast.FunctionDef)
    }
    copied = sorted(defined & {"holder", "read", "is_expired", "lease_path", "describe"})
    assert copied == [], f"the loop has its own copy of the reader: {copied}"


def test_the_armed_strategy_rule_has_exactly_one_home() -> None:
    """Which strategy the bot is FOR is asked in one place, by both processes.

    It used to live with the loop, which meant the dashboard could not answer it without
    importing the loop — so the answer would have been re-implemented in the web layer and
    the two would drift. A second definition anywhere under ``src`` fails here.
    """
    homes = []
    for name, path, _is_package in _modules():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.FunctionDef) and node.name == "armed_strategy":
                homes.append(name)
    assert homes == ["src.config.trading_state"], homes
    assert "src.config.trading_state.armed_strategy" in GRAPH["src.scheduler.orchestrator"]
    assert "src.config.trading_state.armed_strategy" in GRAPH["src.scheduler.host"]


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


def test_the_loop_host_refuses_before_it_reads_anything(monkeypatch) -> None:
    """The host guard runs first — before argv, before any config or data file.

    Phase 5 made this matter: ``main`` now parses a command line and resolves the account,
    so the guard has to come before both. A process that must not host the loop has no
    business touching the account on its way to saying so, and the test proves the order
    rather than trusting it: parsing is replaced by something that would explode.
    """
    def explode(argv=None):
        raise AssertionError("argv was parsed before the host was checked")

    monkeypatch.setattr(loop_host, "__name__", "__main__")
    monkeypatch.setitem(sys.modules, "src.web.app", web_app)
    monkeypatch.setattr(loop_host, "parse_args", explode)

    assert loop_host.main(["--once"]) == loop_host.EXIT_WRONG_HOST


def test_the_loop_host_no_longer_promises_nothing(monkeypatch) -> None:
    """``EXIT_NOT_IMPLEMENTED`` is gone, and that is the point of Phase 5.

    It existed to stop the process reporting success while trading nothing — the same lie
    the trading switch refuses to tell. Now that the loop exists, the code that told that
    lie has to go with it, or the next reader would have to work out which one is current.
    """
    assert not hasattr(loop_host, "EXIT_NOT_IMPLEMENTED")
    assert hasattr(loop_host, "EXIT_ALREADY_RUNNING")

    monkeypatch.setattr(loop_host, "__name__", "__main__")
    monkeypatch.delitem(sys.modules, "src.web.app", raising=False)
    options = loop_host.parse_args(["--once"])
    assert options.once is True
    assert loop_host.parse_args([]).once is False
