# The portal's front door — the PIN design

> **Status.** Stages 1, 2 and 3 are BUILT (`src/web/services/auth_service.py`, `python -m
> src.web.auth`, `src/web/middleware.py`, `src/web/routes/auth.py`, `src/web/templates/login.html`,
> `src/web/static/auth.{js,css}`), including the three jobs of the lock screen — set the first PIN,
> enter it, change it — and the Security card on the Session monitor. The lock
> is OFF until a PIN exists, and from then on the middleware gates every request
> (it reads the store per request, so no restart is involved). What remains is at the foot of each
> stage below.

**Why.** The portal is one person's window onto a live account: from it you can place an order,
flatten a position, move the switch, or change the strategy the bot is running. Today anything
that can reach the port can do all of that. This is the design for putting a lock on that door —
one person, one PIN — and, just as importantly, for being able to **change** it later without
redesigning the store.

## The invariant that shapes everything: the loop must not know this exists

The trading loop is a **separate, detached process** (`src/web/services/loop_control._spawn`
starts `python -m src.main` with `start_new_session=True`, its own session, and stdout to
`data/loop.log`). Its entire input is **files** — `trading.json` for the switch, the strategy
store for rules and settings, the dataset for bars — plus the broker and the provider. It has no
concept of a session, a cookie or a token anywhere in `src/main.py`, `src/scheduler/**`,
`src/config/**`, `src/execution/**` or `src/strategy/**`.

That is not merely convention: `tests/test_architecture.py` fails if anything outside `src.web`
imports `src.web`, and if the web layer reaches for the loop. So the lock lives **entirely inside
`src/web`** — middleware, routes, a login page — and **no auth code may touch
`data/trading.json`**. A lockout, a lockout's expiry, a sign-out or a PIN change must never stop,
start or alter a run. A locked portal is a locked *browser*; the bot keeps ticking.

## Stage 1 — the lock (three commits)

1. **`src/web/auth_service.py` + `python -m src.web.auth` CLI + tests.** No behaviour change:
   the store is only written by the CLI, so nothing is enforced yet and the suite is untouched.
2. **The middleware + `/api/v1/auth/*` + `/login` + `src/web/static/auth.js`.** Enforced only
   when the store exists.
3. **Sign-out, docs, and the route-coverage guard** — a test that walks the app's own routes and
   fails on any path that is neither in the allowlist nor behind the check. The guard is the
   point: a new endpoint added next year must not be able to arrive unauthenticated by omission.

Decisions already fixed, and the reasons they matter:

- **Fail-closed allowlist** — `/login`, `/api/v1/auth/*`, `/static/*`, `/api/v1/health`, plus the
  machine token below. Everything else is behind the check, including `/` and every `/api/v1/*`.
- **A stretched PIN, with a per-install salt**, compared with `hmac.compare_digest` (never `==`).
  It is **PBKDF2-HMAC-SHA256 at 600,000 iterations** (~145 ms a guess here) rather than scrypt, and
  not by preference: this project runs the system Python 3.9.6, which links **LibreSSL 2.8.3**, so
  `hashlib.scrypt` does not exist. The parameters ride on the record, so an install on a host with
  OpenSSL can move to scrypt without a migration.
- **HMAC-signed `HttpOnly` cookie**, `SameSite=Lax`, `Secure` when the request arrived over HTTPS.
- **Persisted lockout** on repeated failures — persisted, because a restart must not reset an
  attacker's budget.
- **A CSRF guard made of the cookie plus the Origin.** The cookie is `SameSite=Strict`, and a
  mutating request whose `Origin` is not this host is refused. The token this document first called
  for is therefore not needed: `Strict` stops another SITE sending the cookie, and the Origin check
  stops the rest — another local port is a different origin on the same site, which is the realistic
  attack on a localhost dashboard. A request with no `Origin` is not a browser, and gains nothing by
  lying, because without the cookie it has no session.
- **Auth is enabled only when `data/auth/auth.json` exists.** A fresh checkout and the 1400-test suite
  are unaffected; `disable` turns it off again.
- **The session carries two clocks** — a sliding `last_seen` and a hard `exp` — and the middleware
  enforces the idle window from them (see stage 3). Stage 1 slides `last_seen` on any authenticated
  request, which is permissive and harmless; stage 3 flips it to the explicit activity signal,
  with no change to the cookie's shape.
- **A machine token** for the watchdog: the monitor's own poll is what restores a crashed loop
  (`/api/v1/loop/ensure`), and that poll stops when nobody is signed in or the tab is hidden — so
  a `launchd`/cron job needs a way in without a session.

## Stage 2 — changing it (planned now, built later)

The expensive part of a PIN change is not the endpoint; it is the **facts the store has to have
recorded**. Stage 1 therefore writes this shape even though nothing reads the last three fields
yet — cheap today, a migration tomorrow:

```json
{
  "version": 1,
  "secret": "<hex>",            // the cookie-signing key
  "machine_token": "<hex>",     // for the watchdog cron — deliberately NOT rotated by a PIN change
  "users": [
    {
      "id": "owner",
      "label": "",
      "salt": "<hex>",
      "hash": "<hex>",
      "kdf": {"algo": "pbkdf2_sha256", "iterations": 600000, "dklen": 32},
      "generation": 1,          // bumped by every PIN change; the session cookie carries it
      "failed_attempts": 0,
      "locked_until": null,
      "updated_at": "<iso>"
    }
  ]
}
```

- **`users` from day one**, with the session carrying `sub` (the user id): that is the
  multi-account seam, without building multi-account now.
- **`generation` and `secret`** are the two things a PIN change must invalidate.
- **`kdf` params are stored** so the cost can be raised later without a schema change, and
  **`version`** so a future migration has somewhere to look.

**Where a PIN is written.** Two doors, and the lock screen is the one anybody can find:

- `GET /login` is reachable by typing the address, always, and the card it serves has **three jobs**:
  **create** the first PIN (when the store does not exist), **unlock**, or **change** the PIN. That
  is why it is public and why it never bounces you to the dashboard any more.
- `python -m src.web.auth set | change | reset | disable | status` on the machine. `reset` — a new
  PIN WITHOUT the current one — exists only here, and deliberately: a web reset would be a permanent
  bypass.

**The open door, stated plainly.** `POST /api/v1/auth/setup` refuses the moment a PIN exists (409),
and that refusal is what makes it safe to serve to anyone who can reach it. It also means that
**whoever reaches the page first, while no PIN exists, chooses the PIN** — so the window between
installing this and setting a PIN is a window. The create card says so in as many words. Mitigations,
if the portal is ever exposed beyond the machine:

- **Gate creation on the connection:** allow it only when the peer is loopback/private AND the
  request carries **no forwarding headers**. That second half is the part that matters — a local
  reverse tunnel connects to uvicorn *from* `127.0.0.1`, so `request.client.host` alone would call a
  remote attacker the person at the keyboard. `X-Forwarded-For` / `CF-Connecting-IP` / `X-Real-IP`
  being present is what tells them apart.
- Or a one-time bootstrap code printed at startup, which is more machinery and fights the
  "the loop is a detached process" shape.

The PIN itself must be **4-12 digits** and not one of the shapes everyone tries first (all the same
digit; `1234` and friends) — `auth_service.pin_problem` is the rule, mirrored in `auth.js` so a
mistake is answered without a round trip. It is a nudge, not a policy.

**And the card says so.** A quiet line under the keypad carries the two things somebody would
otherwise have to guess, per mode:

- **create** — `4-12 digits, and longer is better. Not one digit repeated, and not 1234 or 123456.
  Stored salted and hashed, and never shown again.`
- **change** — the same rules, plus `Changing it signs out every OTHER session, so this one stays
  signed in.`
- **unlock** — `Forgotten it? On the machine that runs the bot — the one with data/ beside it — run
  .venv/bin/python -m src.web.auth reset`

That last one is **text, not a button**, and that is the point: running it already requires the
machine, so naming the command helps only the operator, while a "reset it" link would be the bypass
the CLI exists to avoid. (`tests/test_web/test_lock_screen.py` pins both halves: the sentence is
there, and the only link on the unlock card is "Change PIN".)

**Changing the PIN — `POST /api/v1/auth/change` (current, new).** `auth_service.change_pin`
re-hashes, bumps `generation` and rotates `secret`, so **every other device is signed out** — while
the caller, who has just proved they know both PINs, gets a fresh cookie in the same answer and
stays exactly where they were. Requiring the **current** PIN is what makes a stolen cookie
insufficient to change anything, and that endpoint feeds the **same failure counter and lockout** as
the login, because it is the same secret being guessed. Choosing the PIN you already have is not an
error: nothing is rotated and no session is signed out over it.

The lock screen **verifies the current PIN at the step where it is typed**, by calling the login
endpoint, rather than at the end after the new PIN has been typed twice. Two reasons: a wrong one is
answered where the mistake was, and the final `change` call can then only fail on the NEW PIN.

**Signing out the sessions you are not looking at — `POST /api/v1/auth/sign-out-everywhere`.** A
secret rotation with the PIN left alone, offered by the Security card. It needs a session of its own
(the middleware lets the whole `/api/v1/auth/` prefix through, so the route checks), and answers the
caller with a fresh cookie.

**Forgetting it — `python -m src.web.auth reset`.** For a self-hosted single user the honest
answer is a CLI on the machine: new PIN twice, no current PIN, no network. It rewrites the store
and bumps the generation, so every session dies with it. This is not a new exposure — the CLI
sits beside `data/credentials.json`, which is the same trust level — and it is the only recovery
path that does not add email, SMS or a second account. `disable` deletes the store and turns the
lock off. The lock screen shows this as plain text rather than a button, because a "forgot it?"
button on a public page is the bypass the CLI exists to avoid.

**What a change must NOT do:**

- touch **`data/trading.json`** — the switch is the loop's only authority over itself;
- touch the **machine token** — rotating it would silently break the watchdog cron;
- touch the strategy store, the broker credentials or the dataset;
- invalidate a run. A loop that is ticking keeps ticking through a PIN change, a lockout, or a
  sign-out.

## Stage 2's build order

4. **`auth_service.change(current, new)` + `reset`,** with the generation/secret rotation and the
   tests that prove an old cookie is refused while the caller's fresh one works. **BUILT**
5. **`POST /api/v1/auth/change`**, `POST /api/v1/auth/setup`, `POST /api/v1/auth/sign-out-everywhere`,
   `GET /api/v1/auth/status`, and the **Security** card on the Session monitor: change PIN, sign out
   everywhere, when it was last changed (`updated_at`), and — with no PIN set — that the portal is
   open, with the button that closes it. **BUILT**
6. **The CLI verbs** `set | change | reset | disable | status`, wired to the service, so the
   forgotten-PIN path is testable and documented rather than a paragraph in a README. **BUILT**

**One component, three modes.** The card is `mode: "unlock" | "create" | "change"` over a shared
step machine (`FLOWS`), so `/login`, the overlay a page raises on 401, and the Security card's
buttons are all the same DOM with the same behaviour. Adding a fourth job means adding a mode, not a
second card — which is also why the overlay can offer "Change PIN" without a new screen.

**Inactivity note.** The heartbeat is the only thing that slides a session's idle clock. It used to
slide on **any** authenticated request as well (`SLIDE_AFTER_SECONDS` in the middleware) — that was
the permissive half of stage 3, and it defeated the point of it: the monitor polls every twenty
seconds, so a tab nobody was sitting at kept its own session alive for ever. Worse, it broke short
windows outright: the slide waited five minutes before it would move a clock that a one- or
three-minute window had already run out, so the server refused a session between two beats *while
the operator was typing*. Now `POST /api/v1/auth/heartbeat` is the whole of it, and the browser
beats a third of the window (see below).

## Stage 3 — locking itself when nobody is there (planned)

**The rule:** fifteen minutes with no *human* action puts the lock screen up. Nothing the machine
does counts — not the twenty-second poll, not the countdown redrawing every second, not a chart
refresh, not the loop writing ticks (which the browser cannot see anyway).

**What counts as a human.** Exactly the five events `auth.js` binds: `keydown`, `pointerdown`,
`mousedown`, `wheel`, `touchstart` — plus `visibilitychange → visible`, because coming back to the
tab is an action somebody took. The listeners are capture-phase and passive, so a handler that
stops propagation cannot stop the clock. Every one of them does one thing: writes `Date.now()` to
`state.lastActivityAt`. Nothing else counts — not the twenty-second poll, not the one-second
redraw, not a chart refresh, and not the heartbeat the page sends on its own. A keystroke in *any*
of the portal's tabs counts for all of them, since they share the session and the `traider.lock`
key.

**Two clocks, two layers — and the browser's is not the enforcement.**

- *Client.* `auth.js` holds `lastActivityAt`; one 1-second check (the pattern the countdown already
  uses) compares `now - lastActivityAt` against the window and raises an opaque overlay reusing the
  login screen's PIN field. On lock the poll and the charts **stop**, so nothing new is fetched or
  drawn beneath it; unlocking does one `loadAll`.
- *Server.* The session's own idle window lapses as well, so the overlay is the *visible
  consequence* rather than the control. Without that half, a stale cookie could still
  `POST /api/v1/trading/on` from another tab, curl or devtools — a screen lock that only hides
  pixels is not a lock.

**The idle window is a SETTING.** `Account Settings → Security → Sign Out User Inactivity
Minutes`, 1-30 whole minutes, default 15. It lives in `data/account/account.json` as
`AUTH_IDLE_MINUTES` and is read per request. One reader — `auth_service.idle_seconds` — serves the
places that care: the middleware refusing a stale session, the login that tells the browser the
window, `GET /api/v1/auth/status`, which is what the Security card prints, and the heartbeat's own
answer. The bounds come from the field's own pydantic constraints, so the form's `min`/`max` and the
server's validation cannot drift. `data/auth/auth.json` still carries an `idle_seconds` from when it was
created, and it is only the fallback for an object without the setting.

**A change reaches the browser, not just the server.** The browser is the half that raises the card,
so a window it read once and kept would be a setting that only *looks* obeyed — and that is exactly
what the operator hit: the setting was moved to three minutes and the screen still locked after one,
over a session the server considered perfectly alive. So the tab adopts the window from **every**
answer that carries one — `beginIdleWatch` at page load, the answer that signs you in (login, setup,
change), the heartbeat, and an explicit `Auth.refreshWindow()` that `saveAccount()` calls right after
an account save. The saved tab therefore obeys the new value at once; a tab somebody is *working* in
hears within one beat; a tab nobody is touching keeps the window it loaded with, which can only ever
lock it **earlier** than the setting says, never later — the server still enforces the real one, and
the next unlock hands the page the true value.

**The poll must not hold the session open.** This is the whole problem: the page reads the log every
twenty seconds, so if the server slid its idle window on *any* authenticated request, the session
would never expire while a tab was open — the dashboard's own traffic would prop the door open. So
the window slides on an explicit signal only: the page posts `POST /api/v1/auth/heartbeat`
(authenticated, CSRF-checked) *while its detector says a human is present*, and nothing else extends
it. A hidden tab's poll therefore cannot keep it alive, and the client's detector is the only thing
deciding whether anybody is there — which is exactly what was asked for.

**How often the beat goes out follows the window, and must.** The interval is
`max(30s, min(4 min, window / 3))` — 60 seconds for a three-minute window, the old flat 4 minutes
for the 15-minute default. A *fixed* beat is a session that expires between two beats: the server
deadline runs from the last heartbeat it saw, so with a four-minute beat inside a three-minute
window the next request after the third minute is refused while somebody is at the keyboard. A
third of the window is always comfortably inside it; the 30-second floor keeps a one-minute window
usable; the 4-minute ceiling keeps the long windows' traffic exactly as it was.

**Hidden tabs are throttled, so lock on return.** A background tab's timers are throttled to about
one tick a minute and may be frozen outright, so the check is a *comparison of timestamps*, never a
countdown of ticks. On `visibilitychange → visible` the page looks at the elapsed time first and
raises the lock immediately if the window has passed — otherwise a laptop reopened after lunch would
show live positions for a moment before deciding.

**Several tabs are one session.** Activity in any tab is activity (the heartbeat is the proof), and
a lock in one tab locks them all: both directions travel on the `storage` event, so a background tab
cannot sit unlocked, and working in one tab does not lock the other.

**Deliberate exceptions.** The **machine token** is not a user and must not be subject to idle
expiry — an hourly cron would otherwise fail every time — so it gets its own rule (a long,
non-sliding validity) and never touches the browser path. And an **absolute cap** (say 24 h) ends a
session even when activity is continuous.

**Tests.** The detector is a node harness in the style the other page tests use — a faked document
and clock — asserting that a fetch, a re-render and `visibilitychange → hidden` do **not** reset the
clock, that `keydown` / `pointerdown` / `wheel` / `visibilitychange → visible` do, that the lock
fires at exactly the window and fires **once**, and that returning to a hidden tab past the window
locks immediately. Server side: a cookie whose idle window has lapsed is refused, the heartbeat
slides it, a poll-shaped request does **not**, and the machine token is unaffected.

**The loop still does not know.** Same invariant as stage 1: this is browser and web-app code, and
no part of it reads or writes `data/trading.json`.

### Stage 3's build order

7. `Auth.beginIdleWatch({ windowSeconds, onIdle })` in `src/web/static/auth.js` + the shared lock
   overlay both pages include + the cross-tab `storage` channel, with the node harness above.
8. `POST /api/v1/auth/heartbeat`, the idle refusal in the middleware, and `idle_seconds` reported by
   `GET /api/v1/auth/status` so the window has one source of truth rather than a 15 hard-coded in
   the browser. **The heartbeat is the only slide** — the middleware's slide-on-any-request is gone,
   and the browser's interval is derived from the window.
9. The CLI's `idle <minutes>` verb (stored beside the KDF params in `auth/auth.json`) and the docs.

### What remains, exactly

- Stage 1 is complete: the service, the CLI (`set | status | change | reset | disable`) and the gate,
  with the route walk and "the lock never touches the switch" pinned by tests.
- Stage 2 is complete: `POST /api/v1/auth/setup`, `POST /api/v1/auth/change`,
  `POST /api/v1/auth/sign-out-everywhere`, the lock screen's create and change modes, and the
  Security card on the Session monitor.
- Stage 3 is missing one small thing: the CLI has no `idle <minutes>` verb, now the least useful
  of the set because the setting is on the Session monitor's Security card. The rest is built — the
  detector, the overlay, the cross-tab channel, the create and change modes, the heartbeat as the
  only thing that slides a session, and a beat whose interval follows the window it has to fit in.
- Not built, deliberately: a **web reset** (a permanent bypass) and a **"forgot it?" button** on the
  lock screen. Both stay on the machine.

### The page, as it stands

| Mode | When the card comes up in it | What it sends |
| --- | --- | --- |
| **create** | `GET /login` and no PIN exists. The only way a PIN is ever set from a browser. | `POST /api/v1/auth/setup` (once, after the second entry matches) |
| **unlock** | Any page with no session, a 401 from anywhere, or the idle window lapsing. | `POST /api/v1/auth/login` |
| **change** | The card's "Change PIN" link, or the monitor's Security card. | `POST /api/v1/auth/login` for the current PIN, then `POST /api/v1/auth/change` |

The "Enter" key prints **Continue**, **Set PIN** or **Save PIN** depending on where in the flow it
is, and the dots are the entry in progress — not the PIN as a whole — which is what lets one card
collect two or three values without a second screen.

## Open decisions

They change stage 1's middleware, not the store — which is why the store shape above is worth
settling first:

- **PIN, TOTP, or a real WebAuthn passkey.** The store holds a PIN hash today; a passkey would add
  a per-user credential list and keep `generation` as the invalidation switch, so the shape
  survives either answer.
- **Where the portal is reachable from.** A tunnel or VPN keeps it on localhost; a public HTTPS
  name means TLS, HSTS and `Secure` cookies land in the same change.
