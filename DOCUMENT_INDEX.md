# Document index

Where to look, and what each document is for.

| Document | What it is for |
| --- | --- |
| [`README.md`](README.md) | Front door: what TRAIDER is, its status, the architecture, and how to run the two processes |
| [`QUICKSTART.md`](QUICKSTART.md) | Setup from a clean checkout, and the two commands that start the dashboard and the bot |
| [`TRAIDER_PLAN.md`](TRAIDER_PLAN.md) | The phase plan, the file tree, and the full settings reference |
| [`docs/execution-loop.md`](docs/execution-loop.md) | The live loop: agreed design, the tick order, the gate policy, and its build order |
| [`DEPENDENCY_GRAPH.md`](DEPENDENCY_GRAPH.md) | What has to exist before what, and which pieces block which |
| [`CHECKLIST.md`](CHECKLIST.md) | Task tracker, with what is done and what is deferred |
| [`SUMMARY.txt`](SUMMARY.txt) | One-page overview for a reader who wants the shape of the project |

## Code, not prose

Some of what used to live in documents is now enforced by the code, which is the
better place for it:

- **The two-process split** — `src/process_info.py` names both roles, `src/main.py`
  refuses to start if the dashboard is loaded inside it, and
  `tests/test_architecture.py` fails if a web module gains a path to the loop. See
  README, "Two processes".
- **"One strategy, two drivers"** — `tests/test_strategy_parity.py` replays history
  through the live driver and asserts it produces the trades the backtest reported, and
  `tests/test_architecture.py` pins which modules may import the shared machine.
- **Which settings are applied** — the risk layer's rule (empty means NOT APPLIED) is
  in `src/config/settings.py` and `src/config/effective.py`, not in a table.

## Archived

[`docs/archive/`](docs/archive/) holds superseded documents kept for reference rather
than deleted. They describe an earlier state of the project and are **not** current.
