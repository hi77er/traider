"""What the two pages are CALLED, and where each name is shown.

The product has two pages and they answer two different questions: the **Strategy lab** builds a
strategy and measures it; the **Session monitor** reads what the loop did with it and is where
trading is switched. Both facts have to be readable from the screen — a page whose name only
exists in a `<title>` is a page you identify by looking at its panels.

Pinned here:

* each page's title, its heading, and (for the lab) the name beside the logo the two pages share;
* the way from one to the other, in the header rather than only inside a panel: the lab's link to
  the monitor used to live in the Trading panel's head, so removing the panel would have removed
  the only way there;
* that neither old name survives anywhere in the web layer — a half-done rename leaves a menu
  saying one thing and a page heading saying another, which is how "the dashboard" and "the log"
  both ended up in the repo at once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "web" / "templates"
STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"

INDEX = (TEMPLATES / "index.html").read_text(encoding="utf-8")
LOG = (TEMPLATES / "log.html").read_text(encoding="utf-8")
MARKET = (TEMPLATES / "market.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path, name",
    [
        (TEMPLATES / "index.html", "Strategy lab"),
        (TEMPLATES / "log.html", "Session monitor"),
        (TEMPLATES / "market.html", "Market"),
    ],
)
def test_each_page_says_its_own_name_in_the_title(path: Path, name: str):
    assert f"<title>TRAIDER — {name}</title>" in path.read_text(encoding="utf-8")


def test_the_monitor_names_itself_exactly_the_way_the_lab_does():
    """One identity group, three pages: the logo they share, then the name of the one you are on.

    The monitor used to carry its name as a suffix on the product name in a single ``<h1>``, which
    made the two pages' titles stylable by nothing in common. It has the same element as the lab
    now, so the pair cannot drift apart — and the Market page, which put its own name where the
    logo goes, was the last one out.
    """
    for html, name in ((LOG, "Session monitor"),
                       (INDEX, "Strategy lab"),
                       (MARKET, "Market")):
        left = html[html.index('class="header-left"') :]
        left = left[: left.index("</header>")]

        # The wordmark alone: no icon in front of it (each page used to wear its own emoji there).
        assert "<h1>TRAIDER</h1>" in left, f"the shared product wordmark, on {name}"
        assert f'<span class="page-name">{name}</span>' in left
        assert f"TRAIDER — {name}<" not in left, "not a suffix on the product name"


def test_every_page_name_is_one_element_with_one_style():
    """The page's title, centred over it and read AS a title: at the page's own middle rather than
    tucked beside the logo, because the two side groups are different widths on the three pages and
    the middle of the bar has to mean the middle of the page on all of them.

    The look is a display title, not body text: a webfont asked for by every page that shows one,
    capitals opened up with tracking, and no flat colour — the words are clipped out of a light
    gradient (white into a cool highlight), which is why ``color: transparent`` is correct here
    rather than a bug: the glyphs are the window the gradient shows through.
    """
    for html in (INDEX, LOG, MARKET):
        assert html.count('<span class="page-name">') == 1
        assert "fonts.googleapis.com/css2?family=Inter" in html, "the display face, in the head"

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    rule = css[css.index(".page-name {") :]
    rule = rule[: rule.index("}")]

    assert "position: absolute" in rule and "left: 50%" in rule, "the page's middle"
    assert 'font-family: "Inter"' in rule, "the webfont the pages ask for"
    assert "font-size: 21px" in rule, "bigger than the logo it sits above"
    assert "text-transform: uppercase" in rule, "capitals"
    assert "letter-spacing: 0.16em" in rule, "capitals need the air between them"
    assert "linear-gradient(" in rule and "background-clip: text" in rule
    assert "-webkit-background-clip: text" in rule, "the property WebKit honours"
    assert "color: transparent" in rule, "what lets the gradient show through the glyphs"



def test_the_lab_says_which_account_and_whether_trading_is_on():
    """Two read-only chips beside the way to the monitor: the account in play, and the switch.

    Both are answered on the Session monitor, and this page can act on neither — so they inform
    rather than control. They are here because the PAIR is what a person glancing at the builder
    needs: "live" with "on" is real money moving, and before this the account a strategy would
    trade was only visible by opening the other page.

    The chips carry no colour of their own. The only signal is the DOT, which goes red and blinks
    for the two states that cost money — live, and armed — because tinting every state made the
    safe setting as loud as the dangerous one, and "paper + on" is not the warning "live + on" is.

    The account is read from ``execution.env``, the RESOLVED target, and not from the switch's own
    ``env``: that one records the account the switch was last ARMED on and is kept after a stop on
    purpose, so a flip from paper to live while trading is off left it saying "paper" and the chip
    with it — the mode tile on the monitor, which reads the resolved target, said "live" at the
    same moment.
    """
    assert 'id="lab-state"' in INDEX
    bar = INDEX[INDEX.index('class="strategy-bar-main"') :]
    bar = bar[: bar.index("strategy-bar-actions")]
    assert bar.index('href="/log"') < bar.index('id="lab-state"'), "beside the monitor link"

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'api("/api/v1/trading")' in js, "the answer comes from the switch's own endpoint"
    assert "renderLabState();" in js, "and a successful read renders it"
    body = js[js.index("function renderLabState()") :]
    body = body[: body.index("\n}")]
    assert "const env = String(execution.env || trading.env" in body, \
        "the resolved target first — the switch's own env is the last ARMING"
    assert "execution.live === true" in body, "live comes from the resolved flag, not the string"
    assert "trading.on" in body, "the account and the switch"
    watch = js[js.index("async function runDatasetWatch()") :]
    watch = watch[: watch.index("\n}")]
    assert "loadTrading();" in watch, "the chip re-reads on the page's slow cadence, so a flip"
    assert "trading ${on ? \"on\" : \"off\"}" in body
    assert 'class="dot"' in body, "the dot the other chips carry"
    assert '"live alarm"' in body, "live blinks"
    assert '"on alarm"' in body, "and so does armed"
    assert '"paper"' in body and '"off"' in body, "the calm states carry no alarm class"

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".info-chips" in css
    chip = css[css.index(".chip-info {") :]
    chip = chip[: chip.index("}")]
    assert "var(--accent)" not in chip and "var(--green)" not in chip, "no colour per state"
    assert "var(--red)" not in chip, "the chip itself is not tinted either"
    assert ".chip-info.alarm .dot { background: var(--red); animation: dot-blink" in css, \
        "the alarm lives in the dot, on the monitor's own blink"
    assert "prefers-reduced-motion" in css, "motion off still shows the red"
    for gone in (".chip-info.paper", ".chip-info.live", ".chip-info.on", ".chip-info.off"):
        assert gone not in css, f"{gone} is a tint per state, which this no longer does"


def test_the_lab_reaches_the_monitor_from_beside_its_strategy_picker():
    """One page to the other, from the picker rather than the header's navigation.

    The strategy chosen in the bar is what the loop trades, so reading its session is the next
    thing asked about it. In the header it was one more place to go among the others — and that is
    where the link first landed only because the Trading panel that held it was removed.

    The monitor's side of the pair is its back link at the top of its day menu, which stays.
    """
    bar = INDEX[INDEX.index('class="strategy-bar-main"') :]
    bar = bar[: bar.index("strategy-bar-actions")]

    assert 'href="/log"' in bar, "the way to the monitor"
    assert "Session monitor" in bar, "named, not 'log'"
    assert bar.index('id="strategy-select"') < bar.index('href="/log"'), "right of the picker"

    actions = INDEX[INDEX.index('class="header-actions"') :]
    actions = actions[: actions.index("</header>")]
    assert 'href="/log"' not in actions, "the header is not where it lives any more"

    assert '<a class="rp-backlink" href="/">← Back to the Strategy lab</a>' in LOG


def test_the_way_to_the_monitor_is_purple():
    """The lab's own controls are blue. This one LEAVES the page, and the colour is how a reader
    tells the two apart before clicking — so it is styled by its own class, not by ``.btn-link``."""
    assert '<a class="btn-link purple" href="/log"' in INDEX

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "--purple: #9b7bff;" in css
    rule = css[css.index(".btn-link.purple,") : css.index(".btn-link.purple:hover { background")]
    assert "border-color: var(--purple)" in rule and "color: var(--purple)" in rule


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.html")) + sorted(STATIC.glob("*.js")))
def test_neither_old_page_name_survives_in_the_web_layer(path: Path):
    """Names, not the words: "log" is still a file, a route and a table of ticks, and ``/log`` is
    the monitor's URL — what must not survive is a page being CALLED the old thing."""
    text = path.read_text(encoding="utf-8")
    for gone in ("Trading log", "trading log", "Dashboard", "Back to dashboard",
                 "Strategy dashboard"):
        assert gone not in text, f"{path.name} still says {gone!r}"
