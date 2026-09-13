# TRAIDER — working agreements

## Version control: the user forgets to commit, so prompt them

- **Commit and push in small, logical steps** rather than one large batch at the end.
- At a natural **completion point** — a feature finished, a bug fixed and verified,
  tests green, docs updated — add **ONE short line** reminding the user to commit and
  push, and offer to run it. Do **not** repeat the reminder on every reply mid-task.
- At the **start of any task**, check `git status --short`. If there is uncommitted work
  left over from an earlier session, say so up front and offer to commit it before
  starting something new.
- When asked to commit/push: review `git status`, stage deliberately, write a real
  message describing the actual change, then verify afterwards — no secrets, runtime
  data or editor junk staged, and the remote really contains the expected files.

## Check this before any "commit everything"

- Rules that ignore **runtime** directories MUST be **root-anchored**: `/data/`, not
  `data/`. A bare pattern matches a directory of that name at **any depth**, so `data/`
  had also been ignoring the entire `src/data/` **source** package — it was never
  committed, and the pushed tree could not import.
- Before committing, check for source hidden by an over-broad ignore rule:
  `git status --short --ignored | grep '^!!'` and `git ls-files <dir>/`.

## Commands

- Python: `.venv/bin/python` — never bare `python`. Python 3.9, so no `str | None`
  (use `Optional[...]`).
- Tests: `.venv/bin/python -m pytest tests/ -q`
- Server: `.venv/bin/python -m uvicorn src.web.app:app --host 0.0.0.0 --port 8000`
  (if the user stopped it themselves, do not restart it — wait for them)
