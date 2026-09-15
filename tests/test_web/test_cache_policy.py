"""The dashboard must never be older than the server it is talking to.

Two reports came from getting this wrong, and both looked like bugs in the code:

* a **removed button stayed on screen** and did nothing when clicked. The script that
  used to handle it was gone; the markup that drew it came from the browser's cache.
  ``app.js`` was served ``no-store`` while the HTML was served with an ``ETag`` and no
  ``Cache-Control`` — so the browser re-fetched the script and reused the document,
  producing a page whose markup was older than its script, which no code path can
  reconcile;
* after a server restart the browser kept running the **previous build**, because the
  static response answered ``304 Not Modified`` and the file's mtime had not changed.

So both are pinned here: the documents and the assets say ``no-store``, and the markup
is fetched fresh rather than revalidated.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.web.app import app

client = TestClient(app)


def test_the_dashboard_document_is_never_cached():
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store", "a stale document is a stale UI"


def test_the_assets_match_the_document():
    """A fresh script beside a cached page is the failure mode, so both must say it."""
    for path in ("/static/app.js", "/static/style.css"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-store", path


def test_the_policy_is_set_by_content_type_not_by_path():
    """Pages live at several paths (`/`, `/market`, `/report`); the rule follows the
    response, so a new page cannot forget it."""
    for path in ("/", "/market"):
        r = client.get(path)
        if r.status_code != 200:
            continue
        assert r.headers.get("content-type", "").startswith("text/html")
        assert r.headers.get("cache-control") == "no-store", path


def test_json_api_responses_are_left_alone():
    """The policy is about the shell, not about the data: an API response may be cached
    by whatever the caller likes."""
    r = client.get("/api/v1/trading")
    assert r.status_code == 200
    assert r.headers.get("cache-control") != "no-store"
