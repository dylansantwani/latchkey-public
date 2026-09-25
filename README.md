# latchkey

Headless Chrome that is already logged in, exposed as a **library**, a **CLI**, and an
**MCP server** for agents.

Cookies are read from your real Chrome profile, decrypted, and injected into a fresh
headless browser. No login step, no 2FA tap, no saved passwords. The browser is headless
throughout — there is no window, not even an offscreen one.

Google is the interesting one, and it is the one thing latchkey asks you to do by hand —
**once**. Gmail, YouTube and the rest of Google carry a **device-bound** account session: its
cookies are renewed by signing a challenge with a key in your machine's keystore, and the
session is re-issued to whichever client presented it last. Two consequences follow, and both
were learned the hard way:

- A *copy* of that session goes stale. Taken on Monday, opened on Friday, it arrives holding a
  value Google retired days ago and reads as signed out.
- Two browsers holding one account session is the shape of a replay. When Google reads it that
  way, what it ends is not the copy's session but the **account's** — which is a sign-out in
  your own Chrome, on a profile latchkey never wrote to.

So Google does not run on a copy of your login. It runs on **latchkey's own profile**, which
you sign in once (`latchkey login`), and which then registers its own device-bound key and
renews its own session, in its own directory — the way a second phone on the same account
does. Nothing is shared, so there is nothing to go stale and nothing to be mistaken for a
replay. Your Chrome is never copied and cannot be signed out.

One profile per account, so a second Google account is `--account school`, not a second login
inside the first one's.

```
Chrome profile  ->  decrypt cookies  ->  inject over CDP  ->  headless Chrome  ->  agent

latchkey's own profile (signed in once, renews itself)  ->  headless Chrome  ->  agent
   ~/.latchkey/profile            = account "default"
   ~/.latchkey/accounts/<name>    = any other account
```

## Four modes

A session that does not name a mode is `auto`: `dedicated` for Google, Gmail, YouTube and
Google-SSO portals you configure, `inject` for everything else, decided per navigation and
said in one line when it changes.

|  | `inject` | `clone` | `dedicated` |
|---|---|---|---|
| How | decrypt cookies, set them over CDP | Chrome opens a copy-on-write clone of the real profile | Chrome opens latchkey's own long-lived profile |
| Startup | ~1s | ~4s | ~1s, after the first run |
| Cookies | yes, partitions intact | yes | only the ones this browser earned itself |
| localStorage | via saved sessions | yes | yes |
| **IndexedDB** | **no** | **yes** | **yes** |
| Service workers, session storage | no | yes | yes |
| Every Chrome profile | yes, merged | yes, already present | no — its own profile only |
| A live login of yours | never (held back) | copied in | **never** — it has its own |
| Google | refused | on request only | **the default route** |
| Chrome can stay open | yes | yes | yes |

`real` is the fourth mode, and it copies nothing at all: latchkey attaches to a Chrome that
is already listening for DevTools and drives it. **It cannot attach to your everyday Chrome on
a current Chrome.** Chrome 136 and later ignore `--remote-debugging-port` for the default
profile directory — whether or not the directory is named on the command line — and a Chrome
that is already running hands a second launch its window and exits. This README used to give
a command that named the default directory explicitly; on Chrome 153 that opens nothing to
attach to. latchkey therefore no longer launches Chrome for this mode on 136+: it fails at
once and says what does work. What `real` can still attach to:

```bash
# a Chrome you started on a non-default profile, with a port
LATCHKEY_CDP=http://127.0.0.1:9222 latchkey get https://example.com --mode real
```

or a Chrome that has allowed remote debugging for itself at
`chrome://inspect/#remote-debugging` (Chrome 144+; it writes `DevToolsActivePort` into its
profile, latchkey reads it, and Chrome asks you to approve the connection — reported flaky on
recent stable builds). While a port is open, anything on this machine that can reach it can
drive that browser. `close()` only disconnects — it never closes the browser — and no
fingerprint is applied in this mode.

`inject` is fast and never touches your real profile — it is the default for every non-Google
site. `dedicated` is latchkey's own profile, signed in once, and it is the default for Google,
Gmail and YouTube, so you rarely name it. `clone` is still the widest coverage — the only mode
that covers IndexedDB — and is there when you want your real profile's storage; it is no
longer the Google route, because a copy of a Google login is what signs you out. Set
`LATCHKEY_GOOGLE_MODE=clone` if you would rather carry your Chrome's Google login than sign
latchkey in once, and accept those two failures.

A clone is copy-on-write on APFS and initially consumes little extra disk because blocks
are shared until one side writes. Startup time depends on the profile and machine.

## Signing latchkey in: `latchkey login`

This is the one manual step, and it happens once per account. Google, Gmail, YouTube and
known Google-SSO portals all run on latchkey's own profile, so until it is signed in they
read as signed out — and the `next_step` on that page says so, naming the account.

```bash
latchkey accounts               # which logins exist, and which are signed in
latchkey login                  # sign the 'default' account in to Google
latchkey login --account school # a second Google account, on a profile of its own
latchkey login --status         # signed in? which accounts?
latchkey login --again          # add or switch accounts on that profile
latchkey login https://canvas.example.edu/   # any other site, on the same profile
```

Then use one:

```bash
latchkey --account school get https://mail.google.com/
```

To route a third-party portal that uses Google SSO to a named account in `auto` mode,
set a JSON host-to-account mapping before starting latchkey:

```bash
export LATCHKEY_GOOGLE_SSO_ACCOUNTS='{"portal.example.edu":"school"}'
```

Only the named host and its subdomains use that account. With no mapping, latchkey does
not assume that any third-party portal uses Google SSO.

### Several accounts

Each account is a Chrome profile of its own, and nothing is shared between them — separate
cookies, separate storage, separate device-bound key. That is what makes a second Google
account work at all: two logins inside one profile would be two holders of one browser's
session, which is the arrangement Google ends.

| account | profile directory |
|---|---|
| `default` | `~/.latchkey/profile` |
| anything else | `~/.latchkey/accounts/<name>` |

`default` keeps the directory latchkey has always used rather than moving to
`accounts/default`: that directory may hold a sign-in from months ago, and a tidier layout is
not worth losing it to. Names are lower-case letters, digits, `-` and `_`; anything else is
refused, so a name can never be a route out of latchkey's own directory.

Over MCP: `latchkey_accounts` lists them, and `account="school"` on `latchkey_session_open`,
`latchkey_login` and `latchkey_login_status` picks one.

### Why the window is an ordinary Chrome

`latchkey login` opens an **ordinary Chrome window** on that account's profile
(`--profile-dir` or `LATCHKEY_PROFILE_DIR` moves it): the real Chrome binary, `open -n`, and nothing else on
the command line — no `--enable-automation`, no debugging port, none of Playwright's
defaults. That is what lets Google's sign-in page accept it instead of answering "This
browser or app may not be secure". You sign in there: password, 2-step verification,
passkeys, all in that window, and nothing you type reaches latchkey or an agent. latchkey
watches the profile's own cookie store on disk (by cookie name, nothing decrypted) until an
account session appears — Chrome writes cookies in batches, so up to ~30 seconds after you
finish — then closes the window gracefully and opens `https://myaccount.google.com/`
headless to show you what an agent will see (`--no-verify` skips that).

From then on every session opens Google, Gmail and YouTube in that profile, headless, as the
same Chrome binary started with a short explicit flag list and attached to over CDP.
Playwright's own persistent launch carries two dozen automation defaults, one of which (the
mock keychain) makes cookies written by a real window unreadable; starting Chrome ourselves
means the profile is opened exactly the way it was signed in to. Nothing crosses between that
profile and your Chrome in either direction, so there is no copy for Google to rotate away
and no sign-out in your real browser — which is the entire reason this is the default route
rather than an option.

Over MCP the same thing is two calls, because the client gives up on a call after 30 seconds
and a sign-in takes minutes:

1. `latchkey_login` — opens the window and returns at once (it closes this server's own
   sessions on that profile first: Chrome allows one browser per profile).
2. `latchkey_login_status` — waits up to 25 s for the sign-in to land; call it again until
   `signed_in` is true. Then `latchkey_open` the Google page; the next session that needs the
   profile closes the signed-in window itself.

A signed-out Google page's `next_step` says exactly that, and every tool description that
mentions signing in says the one thing a model must never do: ask the user to type a
password or code into the chat.

## Two browsers, one login

This section is the mechanism behind all of the above — why a *naive copy* of a Google login
fails, and therefore why Google routes to a clone that holds the key rather than to `inject`.
With the default `clone`, none of this bites you; it bites an `inject` copy, which is why
`inject` is refused for Google.

If latchkey keeps signing you out of your real Chrome, this is why, and it is not
latchkey damaging anything: nothing in this package ever writes to a Chrome profile. It
is that two browsers are holding **one login** — specifically, an `inject` copy that holds
the cookies but not the key.

A site's cookies are storage. They are long-lived and per-site, and copying them is what
every "log me in on this device" flow does. A Google *account* session is not storage: it
carries a freshness token (`__Secure-1PSIDTS`, `__Secure-1PSIDRTS`, `__Secure-1PSIDCC`,
`SIDCC`) that Google re-issues to whichever client used it last. Copy that family into a
second browser and the second browser retires the value the first one is still holding;
the first one's next request then carries a token Google has already rotated away, Google
reads it as a replayed session, and the session ends — in the real Chrome, an hour later,
on a profile latchkey never wrote to. Which is also why the browser that gets signed out
is the one holding the *good* copy, not the automated one.

Three ways to stop it, in order of preference:

1. **Give latchkey a Google login of its own**, which is what `auto` picks for every Google
   host: `mode="dedicated"`, on a profile seeded by nothing. You sign into it once with
   `latchkey login`, it registers a device-bound key of its own, and after that the session
   lives and rotates inside that directory alone — one holder, one session, nothing to
   replay. `--account <name>` picks which profile (default `~/.latchkey/profile`, others
   under `~/.latchkey/accounts/`); `LATCHKEY_PROFILE_DIR` moves the default one, and every
   one of them refuses to live inside your real profile.
2. **`inject` for everything else.** Ordinary sites: yes, your login travels, and that is
   fine and it is the whole point. Just remember the browser is now holding your session
   for those sites too — `read_only=True` where you do not need to act as you.
3. **Do not clone a Google-signed-in profile.** Clone hands Chrome the account bookkeeping
   as well as the cookies, and a second client presenting the account's refresh token is a
   *reuse*, the one failure mode that ends the session on every device at once. latchkey did
   route Google here for a while, on the strength of one measurement where the copy rotated
   its own token cleanly; what that measurement did not cover was a copy left for days
   (stale) or used alongside the original (a replay). The clone report names the accounts it
   inherited (`signed_in_accounts`) and says so in `warning`, so choosing this does not
   happen quietly.

The live Google session is held back from the injected cookie jar — `.google.com`, the
country sites (`.google.co.uk`), `.youtube.com` — and from saved sessions too, so `inject`
mode logs into your sites but **never** into your Google account; the report counts what it
held back (`withheld_live_session`), `latchkey_session_open` says it in a line, and asking an
`inject` session to wait for a Google sign-in answers `wrong-mode` at once instead of watching
your Chrome forever. `clone` is the exception — it inherits the login along with the profile, which is
precisely what its `warning` is about, and the reason `dedicated` is what anything
Google-hosted uses unless you override it. If you do want the account session *copied* into an injected browser anyway,
say so out loud with `LATCHKEY_SHARE_LIVE_SESSION=1`, and expect your Chrome to be signed
out later; that is the trade, not a bug.

Do not use `profiles="all"` with a Google login in mind, either: it merges every profile's
cookies into one browser, and several accounts' sessions in one jar are not something a real
browser ever does.

## Why an *injected* Google session cannot work — and why its own profile can

If Gmail reads as signed out in an **inject** session while other sites copy
over, the cookies are not the problem, and no amount of cookie-transfer engineering
fixes it. What works is a profile that holds a registration of its **own** — which is what
`latchkey login` creates, and why Google routes there. Chrome keeps a **device bound
session** for origins that ask for one, and Google does:

```bash
sqlite3 "$HOME/Library/Application Support/Google/Chrome/Default/Device Bound Sessions" \
  "select key, length(proto) from dbsc_session_tbl;"
# https://google.com|425      <- the registration; inside it: .https://accounts.google.com/RotateBoundCookies
```

That registration means: this session's cookies are renewed by **signing a challenge with a
private key in the OS keystore**. The key is per profile, it is not in the profile directory
(the directory holds the registration, not the key - `security find-generic-password` for one
finds nothing), and it is deliberately non-exportable. The important word is *exportable*, not
*usable*: the key lives on the machine and a second Chrome from the same signed app, running as
you, can **use** it without ever seeing it. So the decisive question is whether the browser
holding the cookies also holds the registration and can reach the key:

- **`inject` cannot, so it is refused.** It sets decrypted cookies into a fresh profile that
  carries no registration and runs on a mock keychain. It can present the cookies once and
  never renew them, and its first rotation retires the value your real Chrome still holds -
  which is why an inject copy reads as signed out **and** signs your Chrome out. Sites without
  that binding may still work in inject mode, which is why it is refused only for bound hosts.
- **`dedicated` holds a registration of its own, so it is the mode Google uses.** Nothing is
  copied: the profile is seeded by nothing, signs in once as an ordinary Chrome window, and
  registers its own key against its own session. One browser, one session, one holder of the
  freshness token — so there is no copy to go stale and no second holder for Google to read
  as a replay. It renews itself for as long as you keep using it.
- **`clone` can reach the key, but it is still a copy.** A clone copies the whole profile —
  the registration with it — and runs on the *real* keychain, so on this machine it resolves
  the same key and can sign the rotation itself. Measured once: a clone re-minted its own
  bound `__Secure-1PSIDTS` five hours after it was made while the real Chrome stayed signed
  in. That measurement is why clone was the Google default for a while, and what it does not
  cover is the two ways the arrangement actually fails in use — a clone left for days holds a
  token Google retired (stale), and a clone used alongside the original is two holders of one
  session (a replay, which ends the *account's* session and signs your Chrome out). Available
  on request; not the default.

If you do opt back into clone for Google, know that it is never reused between runs for
exactly that reason: it is re-seeded from your real profile on every open (its own directory,
under `~/.latchkey/clones/<this server's id>/google-clone`, so two servers — two models —
never want the same Chrome window). Keep such a session open across actions rather than
reopening it in a tight loop.

`mode="real"` drives a Chrome that already has the key (see "Four modes" for why that is not
your everyday Chrome on Chrome 136+). `LATCHKEY_ALLOW_BOUND_COPY=1` lets even inject copy a
bound host anyway, for anyone who wants to watch it fail; `LATCHKEY_GOOGLE_MODE` picks the
Google mode outright.

## Install

```bash
cd ~/tools/latchkey
python3 -m pip install -e .
```

That puts a `latchkey` command on your path and makes `import latchkey` work from
anywhere, so nothing has to be run from this directory. `python3 -m pip install -r
requirements.txt` still works if you would rather not install the package, in which case
run it as `python3 -m latchkey` from here.

Requires Google Chrome (the script uses `channel="chrome"`, i.e. your installed browser,
not Playwright's bundled Chromium) and Python 3.9+. macOS only: the cookie store, the
Keychain key and the screen measurements are all read the way macOS keeps them.

Everything latchkey writes lives under `~/.latchkey` - saved sessions, the `default`
account's profile (`profile/`) and every other account's (`accounts/<name>/`), the optional
credential vault, the site hints file. `LATCHKEY_HOME` moves it.
The package was called abrowser, and anything left under `~/.abrowser` is carried across
the first time it is imported.

## Tests

```bash
python3 -m unittest discover -s tests        # 848 offline tests in ~35s, no browser
```

Offline tests cover both cookie-decryption layouts, the CDP conversion (partition keys,
`__Host-` handling, samesite mapping, session-cookie expiry), session storage round-trips
and file permissions, credential resolution and its repr safety, the verdict logic, the
MCP protocol and the session-scoped MCP surface. One test asserts that `tools/list` and
the dispatch table have not drifted apart - an agent calling an advertised tool that does
not exist is a silent failure - and another that the one advertised tool's description
indexes every name in the catalog, since a name an agent cannot find is no more useful
than one that does not exist.

`tests/test_sessions.py` drives the verbs through a fake page, which is how the promises
that cost money or round trips are checked without a browser: one page read per `state()`,
cursor coordinates only while someone is watching, no credential in any event, and two
sessions that never share state. `tests/test_mcp_sessions.py` covers routing a tool call
to a named session, and that a slow call in one session neither blocks another nor the
connection.

More files exist because of a specific thing that went wrong, and each says so in
its docstring: `test_secrets.py` (a password reported through the ref path),
`test_decrypt.py` (both cookie layouts, built with a known key rather than guessed at),
`test_navigation_policy.py` (where a session may go), `test_registry_lifecycle.py` (one
wedged session stalling the rest), `test_model_tolerance.py` (the shapes a small
model actually emits), `test_detect_google.py` (Google's password page and a "Signed out"
account chooser that read `logged-in`), `test_google_login.py` (routing, the sign-in
window, the wait that looped) and `test_compact_surface.py` (what a local model carries and
gets back).

## Layout

```
latchkey/
  cookies.py       Chrome cookie store -> decrypted cookies -> CDP params
  profile.py       copy-on-write profile clone (covers IndexedDB, all profiles)
  driver.py        the page handle, the verdict probe, the events, the cursor
  session.py       Browser: lifecycle, cookies, login handoff, SessionSpec
  navigation.py    goto, history, reload, wait_for, links
  automation.py    click, type, scroll, upload, screenshot + the action list
  frames.py        listing and reading inside iframes
  tabs.py          pages, new/switch/close
  detect.py        PageState, the verdict hints, the single-round-trip probe
  viewer.py        the HTTP + SSE server behind the viewer (events, frames, pointer)
  viewer_page.html the page that server hands out: read per request, never compiled in
  sessions.py      Session (thread-pinned) + SessionRegistry, for parallel agents
  events.py        EventBus + the on-page cursor
  store.py         session persistence (cookies + localStorage)
  credentials.py   optional credential sources (vault, env, Keychain, Chrome)
  a11y.py          the snapshot: what is on the page, and a ref for each of them
  policy.py        what a session may do, and where it may go
  paths.py         ~/.latchkey, and the move from the name this used to have
  google.py        which hosts are Google's, and what a sign-in step looks like
  chrome.py        starting the real Chrome binary: a window for a human, headless for an agent
  login.py         signing latchkey's own profile in, once, and knowing when it is
  intervene.py     hand the human a window to pass a wall, with their yes first; carry it back
  reaper.py        close idle sessions so a browser nobody is using stops holding memory
  walls.py         who blocked us, and on what evidence
  human.py         pointer paths and typing rhythm, so a client is not perfect
  fingerprint.py   the screen, the window, the UA and the client hints
  cli.py           command line
  mcp_server.py    MCP server on stdio, for agents
  __main__.py      python3 -m latchkey
viewer/            the Electron app: transparent, always-on-top, click-through
tests/test_latchkey.py       cookies, profiles, store, hints, MCP protocol
tests/test_sessions.py       events, cursor, probe cost, sessions, parallelism
tests/test_mcp_sessions.py   session routing and the stdio transport
tests/test_viewer.py         the SSE contract, frames, attach-only-while-watched
tests/test_secrets.py        a credential must not leave the page, however it was named
tests/test_decrypt.py        both cookie layouts, built with a known key
tests/test_navigation_policy.py  where a session may go
tests/test_registry_lifecycle.py one wedged session costs one session
tests/test_model_tolerance.py    the shapes a small model actually emits
tests/test_paths.py          one directory, and the move from the old name
tests/test_detect_google.py  Google's sign-in pages, as they were read, are signed out
tests/test_google_login.py   routing, the sign-in window, the dedicated launch, real mode
tests/test_compact_surface.py  the surface's size, the small budget, the mistakes models make
tests/test_intervene.py      the human-intervention handoff, driven without a browser
tests/test_assist_surface.py the assist tool over MCP: ask first, then carry cookies back
tests/test_reaper.py         idle sessions are reaped, busy and mid-assist ones are not
examples/verify_wiring.py    real site: events, cursor and probe cost
examples/verify_parallel.py  real sites: two sessions overlap
examples/verify_mcp_path.py  real sites: the session-scoped MCP path, per host
examples/verify_sites.py     a coverage sweep for both modes
examples/verify_viewer.py    real browser: screencast frames, SSE, the pointer
examples/verify_pointer.py   real frame: where the pointer lands, and what the old sum drew
examples/verify_snapshot.py  real page: read by snapshot, act by ref, and a stale ref refusing
examples/verify_agent_surface.py  real page: find, wait, and the size of every reply an agent gets
examples/verify_showcase.py  two agents, two sites, one viewer at once
```

## CLI

```bash
python3 -m latchkey login                    # give latchkey a separate identity, once
python3 -m latchkey get https://mail.google.com/   # open a page: verdict, why, next step, text
python3 -m latchkey sites --profile all      # list hosts from every Chrome profile
python3 -m latchkey show github.com          # inspect one site (values masked)
python3 -m latchkey profiles                 # list Chrome profiles
python3 -m latchkey profiles --host example.edu   # which profile has that host
python3 -m latchkey probe https://github.com/     # is this site logged in?
python3 -m latchkey shot https://chatgpt.com/ -o /tmp/chat.png
python3 -m latchkey run https://example.com/ --actions examples/actions.json
python3 -m latchkey sync                     # re-read the browser's cookies
python3 -m latchkey wait-login <url>         # wait for you to sign in, then transfer
python3 -m latchkey sessions                 # saved sessions
python3 -m latchkey clone --status           # what the profile clone holds
python3 -m latchkey serve                    # MCP server on stdio
python3 -m latchkey watch                    # viewer server: watch the screen live
```

Add `--headful` to any browsing command to watch it work. Default is headless.

- `--profile all` merges cookies from every Chrome profile.
- `--mode` defaults to `auto`: `clone` for Google and known portals whose primary login
  immediately redirects to Google SSO when configured; `inject`
  elsewhere.
- `--mode clone` opens a copy-on-write clone of the real profile instead of injecting
  cookies. Slower to start, far wider coverage — it is the only mode that carries
  IndexedDB. It can also trip a bot wall that inject mode walks through.
- `watch` runs the viewer server in this process, so it shows the sessions *this*
  process opened. With the MCP server, the viewer listens inside that process instead
  (see "Watch it work").

By default **every cookie in the profile** is injected, not just the ones for the site
you are visiting. That is deliberate — see "Why not filter by host" below.

## MCP server (for agents)

```bash
python3 -m latchkey serve     # speaks JSON-RPC 2.0 over stdin/stdout
```

Register it with any MCP client:

```json
{
  "mcpServers": {
    "latchkey": {
      "command": "latchkey",
      "args": ["serve"],
      "env": {"LATCHKEY_VIEWER_PORT": "8788"}
    }
  }
}
```

(`"command": "python3", "args": ["-m", "latchkey", "serve"], "cwd": "~/tools/latchkey"`
works too, for a checkout that was not installed.)

Tools exposed:

| Tool | Purpose |
|---|---|
| `latchkey_batch` | Every latchkey tool call, in one request, in the order given - and the only thing `tools/list` advertises |

### If your model cannot build a batch

One tool whose argument is a list of nested calls is the cheapest surface there is to
carry, and it is the hardest shape for a small model to emit. Two things follow from
that, and both are already done.

The first is that the shapes models actually send are **read rather than refused**. A
stringified `calls`, one call where a list was asked for, `name` instead of `tool`, the
client's own `mcp__latchkey__` prefix still attached, the arguments beside the name
instead of under it, `{"action": "type", "css": "#q", "text": "..."}` for
`{"do": "fill", "selector": "#q", "value": "..."}` - each of those is a model that knew
exactly what it wanted, and each is understood. So are the ones a local model sent in real
use: `mode: "plain"` for a text snapshot, an action with no verb key at all
(`{"ref": "e4", "value": "hi"}` is a fill, `{"url": ...}` a navigation, and a ref a snapshot
named as a button is clicked, not typed into), the verb as the key (`{"click": "e7"}`), a
verb where the tool goes (`{"tool": "click", "ref": "e7"}`), `session` passed to a tool that
has none, `latchkey_wait {"text": "Welcome"}`, and `return document.title` handed to `eval`.
What cannot be read gets one line naming the values that exist. A name or a verb that resolves to
*nothing* is still an error, because running the wrong tool is worse than saying so, but
the error names the nearest match. The default session opens itself on first use, too, so
`latchkey_open` works as the very first call; a *named* session that does not exist is
still an error, because a typo in a name should not be a second silent browser.

The second is that the surface itself is the human's choice, in the server's environment:

| `LATCHKEY_ADVERTISE` | `tools/list` says | per turn (was) |
|---|---|---|
| `batch` (default) | one tool | 2.9 kB (8.7 kB) |
| `core` | the nine a task actually uses, flat, plus the batch and help | 7.8 kB (18.3 kB) |
| `all` | every name | 14.8 kB (29.7 kB) |

Measured as `len(json.dumps(tools))` for the list the server returns. The batch
description is ~2,000 characters (it was 7,316): a worked example, the loop, the core tools
with their arguments, the rest by name, and the rules that matter on every turn. Everything
else moved into `latchkey_help`, by topic (`act`, `snapshot`, `wait`, `google`, `verdicts`,
`batch`, `budget`, `modes`) or by tool name. The server also sends MCP `instructions` at
`initialize` (kept short for clients that truncate tool descriptions).

### A smaller budget for smaller models

`LATCHKEY_COMPACT=1` in the server's environment, or `budget: "compact"` on a batch (or any
single call), roughly halves every default: a snapshot 3,000 characters instead of 6,000, page
text 2,500 instead of 4,000, a batch reply 12,000 instead of 40,000, `eval` 2,000 instead of
8,000, 15 cookies or hosts, 25 links, 5 `find` matches, and replies drop fields that say
nothing. The lists that had no ceiling at all now summarise in either budget: `show_cookies`
is counts plus the session-looking cookies first, `sites` is totals plus the top hosts, and
`eval` reports the size of a large result instead of carrying it. Nothing is cut silently —
`latchkey_text` says the `offset` to read next, a snapshot ends in its outline, lists say how
many more there are, `eval` how big the result was.

Nothing about dispatch changes - every name is callable either way. Only what
`tools/list` advertises does.

### Settling: how latchkey knows a page is ready

After a `goto` or a `click`, latchkey waits for the page to stop changing before it reads it,
up to a ceiling (`settle_ms`: 2500 for a navigation, 1200 for a click). The ceiling is a cap,
not a duration - a click that opens a panel in 40 ms must not pay two seconds.

It used to get there with Playwright's `networkidle`, which means *zero* network connections
for 500 ms. A page with a video, a websocket, an analytics beacon or a long-poll channel never
reaches that, so those pages - most live apps - burned the whole ceiling on every single
action while the page had been interactive the entire time. Settle now watches the requests
genuinely in flight instead:

* a long-lived transport (websocket, server-sent events, streaming media, a fire-and-forget
  beacon) is background by nature and never a thing an action waits on, so it is ignored by
  resource type;
* a request still open past `LATCHKEY_SETTLE_LONGPOLL_MS` (default 1500) is a held-open
  channel, not a reply to what we just did, so it is aged out of the count;
* everything else - a document, its subresources, an XHR or fetch a click kicked off - is what
  "still working" means, and settle waits for all of it to finish.

The page is settled once that filtered count has stayed at or below `LATCHKEY_SETTLE_INFLIGHT`
(default 0) for `LATCHKEY_SETTLE_QUIET_MS` (default 350) without interruption; that quiet
window is also the beat that catches changes which never touch the network. A page that is
still genuinely loading is waited on exactly as before, and a real flood of requests that never
drains still gets the whole ceiling - but a live app that merely holds a socket open now settles
in a beat. A site that polls many times a second can be told to tolerate it with
`LATCHKEY_SETTLE_INFLIGHT=1` (or more).

A batch is a list of calls, each naming any tool in the second table below:

```json
{"tool": "latchkey_batch", "arguments": {"calls": [
  {"tool": "latchkey_session_open", "args": {"name": "github", "host": "github.com"}},
  {"tool": "latchkey_open", "args": {"url": "https://github.com/", "session": "github"}},
  {"tool": "latchkey_injected", "args": {"session": "github"}},
  {"tool": "latchkey_text", "args": {"session": "github"}}]}}
```

That is one round trip instead of four, and it is also what the agent pays for on every
turn: one schema, 2.9 kB. The names and their argument shapes are an index inside that
one description, and `latchkey_help` hands back any tool in full
(`{"name": "latchkey_act"}` for its verbs), so a name is discoverable without being
advertised. `continue_on_error` runs the calls after a failure instead of reporting them
`skipped`, and `parallel` lets calls for *different* sessions overlap.

A batch also gives itself a budget - 24 seconds by default, `timeout_ms` to change it, 28
at the very most - because the client stops waiting for the whole request at 30 seconds.
Calls the budget does not reach come back `skipped`, each saying that is why, instead of
the request vanishing into a client-side timeout with its work still holding the session:

```text
{"tool": "latchkey_act", "ok": false, "skipped": "not run: the batch's 24s budget was
 spent before this call started, and the client waits only 30s for the whole request.
 Send what did not run in another batch."}
```

The same rule applies to one call at a time: nothing here may take longer than the client
will wait, so the few calls that could (a login the user has to do themselves) hand the
wait back - `latchkey_wait_for_login` waits at most 25s and says to call it again.

### The agent's loop

Everything here is shaped for a model paying by the token, behind a client that gives up on a
call after 30 seconds:

1. **Look once.** `latchkey_snapshot` names what is on the page and gives each thing a `ref`.
   Read it in `interactive` mode (the default), and re-read with `mode:"diff"` after an action:
   that says what moved in a few lines instead of sending the page again.
2. **Or skip the look.** `latchkey_find "sign in"` returns the handful of things that match,
   best first, each with the ref that acts on it.
3. **Act by ref.** `{"do": "click", "ref": "e7"}` - no CSS to invent, and a ref from before a
   navigation fails saying what it used to be rather than clicking whatever took its place.
4. **Wait, do not poll.** `latchkey_wait until:"text", value:"Welcome back"` is one call, and a
   timeout still answers usefully: where the page actually got to.
5. **Read the receipt.** An action's reply is the page's shape, not its text: url, title,
   verdict, tab, and `text_chars` when there is something worth reading. A dialog that answered
   itself is in there too - that is usually why a step did not do what it looked like it would.

Two promises the server keeps, so a plan can rely on them: **no call outlives the client's
patience** (a batch runs to a 24 s budget and names the calls it did not reach; the few calls
that could outlast a client - a login the user has to do themselves - hand the wait back), and
**no reply is padded** (page text, screenshots and frames only when asked for).

### Reading a page, and acting on what you read

`latchkey_snapshot` is how an agent sees a page. It names what is on it and hands back a
ref for anything worth acting on:

```text
- main [ref=e1]
  - heading1 "Sign in" [ref=e2]
  - form "Sign in form" [ref=e3]
    - textbox "Email" placeholder="you@example.com" [ref=e4]
    - textbox "Password" required [ref=e5]
    - checkbox "Remember me" [ref=e6]
    - button "Continue" [ref=e7]
```

That ref goes straight into an action - `{"do": "fill", "ref": "e4", "value": "…"}` -
which is what every verb that takes a selector also takes. A ref is minted once per
element and never reused, so a stale one fails saying what it *was* rather than clicking
whatever took its place:

```text
RefError: ref e4 was textbox Email, and it is not on the page now - the page has moved on
since that snapshot. Read it again (latchkey_snapshot) and act on the ref that is there now.
```

Modes: `interactive` (the default), `full` (the page's text as well), `text`, `outline`
(the page's shape with a selector per part, and the tail of a snapshot that was cut for
length), and `diff` - only what changed since the last snapshot, which is the cheap read to
do after an action.

The names are still `tools/call` targets in their own right - an allowlist
(`mcp__latchkey__latchkey_open`) or a script written against the older surface keeps
working - so the only thing the batch takes away is the per-turn cost.

What a batch can call (plus `latchkey_help` itself):

| Tool | Purpose |
|---|---|
| `latchkey_open` | Navigate as you; returns url, title, text and a `verdict` |
| `latchkey_text` | Current page's url, title, visible text |
| `latchkey_sync` | Re-read your browser's cookies, inject anything new |
| `latchkey_wait_for_login` | Wait for you to sign in, then transfer the session (never for Google: there the sign-in is yours, in your own Chrome) |
| `latchkey_login` / `latchkey_login_status` | Sign a separate identity in, once, on latchkey's own profile; then whether it is signed in. Not the Google path |
| `latchkey_save_session` / `latchkey_sessions` / `latchkey_forget_session` | Persist and manage sessions |
| `latchkey_act` | click, click_at, hover, fill, press, select, check, scroll, upload, goto, back, forward, reload, wait, screenshot, eval, sync, save_session, new_page, switch, close_page; selector actions accept `frame` from `latchkey_frames` |
| `latchkey_screenshot` | Screenshot the current page to a path |
| `latchkey_eval` | Evaluate JS, return JSON |
| `latchkey_links` | List links on the current page |
| `latchkey_frames` / `latchkey_frame_text` | Enumerate iframes and read inside one — needed for embedded content |
| `latchkey_pages` / `latchkey_new_page` / `latchkey_switch` / `latchkey_close_page` | Tabs |
| `latchkey_history` | back / forward / reload |
| `latchkey_sites` | Hosts in the cookie store: totals and the largest, with counts — never values |
| `latchkey_profiles` | Chrome profiles on this machine. Pass `host` to see which profile holds that site's cookies (counts and session-looking names, best first) |
| `latchkey_show_cookies` | Cookie names for a site, values masked: counts, session-looking first |
| `latchkey_injected` | How many cookies were injected and accepted |
| `latchkey_credential_sources` | Which credential sources could serve a site |
| `latchkey_session_open` | Open a named, isolated browser: its own Chrome, cookies, tabs. No `mode` is `auto`. `read_only:true` refuses everything that would send something as you |
| `latchkey_session_list` / `latchkey_session_close` | What is open, and closing one |
| `latchkey_viewer_start` / `latchkey_viewer_stop` | Open the local viewer, so a human can watch |
| `latchkey_viewers` | Every viewer currently listening, newest first — which port actually has the sessions |
| `latchkey_close` | Close every session |

The browser is **per session**, not a singleton: each named session is its own Chrome
process on its own thread, with its own cookie jar and tabs, so several agents can drive
several sites at once without touching each other. Every browsing tool takes an optional
`session` argument and defaults to `"default"`. The agent never sees a cookie value —
there is no tool that returns one.

A batch names no session of its own, so its calls do not queue as one request: each takes
the place in its session's queue that it would have had sent on its own, and waits for the
call before it. Calls for one session therefore run in the order they are listed, a batch
cannot be overtaken by the call that follows it, and an `latchkey_open` inside a batch
cannot outrun the `latchkey_session_open` in front of it. Calls for different sessions run
one after another unless the batch asks for `parallel: true`.

## When a client gives up

MCP clients stop waiting for a call at their own timeout, and they say so: the cancellation
arrives as `notifications/cancelled`, with the id of the request they have stopped waiting for.
latchkey used to ignore it — the server drained instead of cancelling, on the reasoning that a
client which stops talking is often a script waiting for replies it already asked for. That
reasoning is sound for a *reply* and wrong for *work*: an abandoned batch of six calls kept
running all six, one after another, in its session's lane, so the agent's next call queued
behind twenty-odd seconds of work it had already given up on. That is what "the browser has
gone unresponsive" looks like from the outside. It was never unresponsive; it was busy with
something nobody wanted.

What happens now:

- a cancelled request that has **not started** does not run at all, and the batch's reply says
  `skipped` with the reason;
- a cancelled request that is **running** is told so, and answers
  `-32800 request cancelled by the client` instead of reporting a result nobody wants;
- `latchkey_wait_for_login` returns `status: "abandoned"` within a fraction of a second of the
  cancellation, because a wait for a human is the one call here that can hold a session for a
  whole minute;
- `latchkey_session_close` and `latchkey_forget_session` cancel whatever is in flight on that
  session's lane first — otherwise the tools for reclaiming a stuck session are themselves
  stuck behind the call they exist to interrupt.

What is still true, so it does not surprise you: a call that cannot check a flag — a
navigation, a slow page — holds its lane until its own timeout, and a wait inside `batch` holds
the lane for as long as that batch runs, because a batch promises its calls run in order. The
next step for both is a wait that yields the lane between polls instead of occupying it.

## Watch it work

The viewer is how a human sees what an agent is doing: the live event stream, the screen,
and the agent's pointer, in one window.

```bash
# Inside the MCP server - which is where the sessions live. Either set
# LATCHKEY_VIEWER_PORT=8788 in its environment, or ask for it:
#   latchkey_viewer_start  ->  {"url": "http://127.0.0.1:8788/"}
open http://127.0.0.1:8788/       # or point the Electron app at it

# For sessions you are driving from your own script:
python3 -m latchkey watch --port 8788
```

And the menu bar, for the human rather than the agent:

```bash
menubar/install.sh        # build LatchkeyBar, put it in ~/Applications, start it
```

`menubar/` is a small AppKit app (built with `swiftc`, nothing to install) that puts that
same web page one click away: left click shows it in a panel, esc puts it away, right click
gives the menu - open in browser, copy the address, which sessions are live, a viewer of its
own when no agent is running, launch at login. The icon is the state: filled while a viewer
is live, a dimmed hollow rectangle while nothing is listening. It picks its server the same
way the Electron app does (sessions first, then `~/.latchkey/viewers.json`, then 8788-8798),
and `--status` reads out what it believes - see `menubar/README.md`.

The HTTP surface, which the web page and the Electron app both speak:

| endpoint | what it gives |
| --- | --- |
| `GET /events` | SSE: `hello`, `sessions`, `event`, `cursor`, `frame` messages |
| `GET /frame/<session>?n=<counter>` | the latest JPEG; `n` is a cache-buster (204 until the first frame, a 404 that names the session and lists what *is* running when it does not exist) |
| `POST /session/<name>/close` | stop a session and close its browser; answers with what is still running |
| `GET /sessions` | the session list as JSON |
| `GET /health` | up, its uptime, and what it can see |

It listens on `127.0.0.1` only and never drives the browser: attaching costs one cursor
injection and a screencast, and the last viewer leaving puts both back. It sends **no CORS
headers**, deliberately — the Electron app adds `Access-Control-Allow-Origin` to its own
requests (`viewer/main.js`), because the one-line alternative in the server would let any web
page the user has open read their browsing off `127.0.0.1:8788/events`. Chromium pushes
frames over CDP rather than anyone polling, with a lower-rate screenshot fallback when a
screencast cannot start, and one screenshot at attach time so the window is never blank.
The pointer rides in the events the agent already publishes, so it keeps moving between
frames. The Electron app is in `viewer/` — `npm start` to run it, `npm run build` to
package it.

**Finding the right port is the app's job, not yours.** Every MCP connection gets its own
latchkey server process, and the viewer socket lives inside that process, so the sessions
are on whichever port that process was given. The app therefore looks for a live viewer
instead of trusting one: it asks the ports in `~/.latchkey/viewers.json` (each viewer writes
itself down as it starts), scans `8788`–`8798`, and prefers a server that actually *has*
sessions — then keeps checking every few seconds and moves to a better one if the agent turns
out to be using another. Pass `--port` (or `LATCHKEY_VIEWER_PORT`) to pin a port; it wins
whenever it has sessions. `latchkey_viewers` lists what is out there.

**A port is a preference on the server side too, and that is what makes the window work.**
Every MCP connection is started with the same `LATCHKEY_VIEWER_PORT`, so the second server
process to come up asks for a port the first one already holds — and the first one is rarely
the one the agent is driving. A server that cannot bind therefore rolls forward to the next
free port (still inside `8788`–`8798`, still registered, and it says so on stderr: `port 8788
was taken`). Giving up instead is the failure that looks like a broken product: the process
with the sessions serves no viewer at all, the only viewer on the machine belongs to a
different connection, and the human's window is connected, live, and empty forever while the
agent is plainly working. `latchkey_viewer_start` takes the same route, so an agent can put
its own process's sessions on screen mid-run.

**An empty window says where the sessions are.** For the same reason — a viewer only ever
serves its own process's sessions — a viewer with nothing to show asks the others in the
registry (`/elsewhere`) what they have, and the page names them with a link (`1 session is on
another latchkey viewer: http://127.0.0.1:8789/`) instead of leaving the reader to conclude
that latchkey is broken.

The window shows **the page, and nothing else**: the strip on top (sessions, click-through,
quit) and a one-line footer with the URL and the verdict — `logged-in` / `logged-out` /
`blocked` / `unclear`, which is the one thing the old action list was carrying that is worth
keeping in view. A `✕` on each tab stops that session and closes its browser; it takes two
clicks (the tab arms red in between), because it is the only control here that changes the
agent's world. It is also how a session that has stopped answering gets cleared: the HTTP
API stays answerable when a session's own thread does not. Anything that goes wrong is said
in full at the bottom — which session is missing, what *is* running, and the likeliest
reason, rather than a bare status code.

## When a site isn't logged in

That is the normal case, and latchkey does not need your password for it.

1. `latchkey_open` returns `"verdict": "logged-out"` plus a `next_step` field.
   Before that step asks you to sign in, latchkey compares the current Chrome profile with
   the others. Harmless analytics, language or device cookies no longer hide a stronger
   session in another profile: `profile_recommendation` contains the exact
   `latchkey_use_profiles` call, and that switch becomes the primary `next_step`.
2. For an ordinary site, the agent asks you to sign in to that site **in your own Chrome
   window**. Google, Gmail and YouTube do not go through this: they open in a clone of your
   own profile, signed in as you, and there is nothing to sign in to.
3. The agent calls `latchkey_wait_for_login(url)`. For an ordinary site that polls your
browser's cookie database; the moment your sign-in produces new cookies, it injects them into
the running headless session, reloads, confirms `logged-in`, saves the session, and returns.
When the requested window is longer than one client call, the result includes a structured
`continuation_call` with a reduced remaining timeout; following those calls reaches the real
deadline instead of restarting the same 25-second wait forever.
If a stronger login may already be present in another Chrome profile, a timed-out wait returns
the same explicit recommendation. It still honors the current-profile wait first because the
other profile's cookies could be stale or belong to somebody else. The profile change is never
automatic because different Chrome profiles may represent different people.
Not for Google: a Google host answers `wrong-mode` at once and tells you to sign in in your
own Chrome and start the session again, which clones the profile you just signed in to.
latchkey never copies your Chrome's Google session and never opens a sign-in of its own for
Google.

Your password is never typed into the agent, never stored by latchkey, and never crosses
the process boundary. The headless browser goes from logged-out to logged-in without a
restart.

```bash
python3 -m latchkey wait-login https://example.com/
# Open https://example.com/ in your real browser and sign in. Waiting up to 300s...
```

There are optional programmatic paths (`latchkey/credentials.py`) that can read a macOS
Keychain internet-password entry, a `~/.latchkey/credentials.json` vault, `LATCHKEY_<SITE>_PASSWORD`
env vars, or Chrome's saved logins. They exist for headless CI, not for the normal flow —
and Chrome's saved-login store is **empty** on this machine, so they will not do anything
until you fill one of them in.

## When a wall needs a person: `latchkey_assist`

Some pages a headless browser cannot get past on its own: a captcha or "press & hold", an
anti-bot block that will not clear, a sign-in whose password the agent must never see. The
agent knows it is stuck — the page reads `challenged` or `blocked` — and `latchkey_assist`
is what it does about it: hand *you* a real browser window carrying that site's cookies, let
you solve the thing, and carry the cookies you earn back into the headless session so it
carries on. It never opens that window without asking you first.

1. The agent calls `latchkey_assist` (no `confirm`). latchkey reads the page and returns
   `ask_user` — which site, which wall — and **opens nothing**. The agent relays it to you.
2. You say yes. The agent calls `latchkey_assist(confirm=true)`. A headed Chrome window opens
   on a throwaway profile, seeded with that site's cookies **at this session's own user agent
   and window size** — the same client the site issued its cookies to. You solve the check in
   it (a password or a code goes here, never into the chat).
3. latchkey harvests the cookies the site gained — the `cf_clearance` you just earned, say —
   injects them into the running headless session, re-reads the page, and reports whether the
   wall is gone. The throwaway profile, which held a copy of the site's cookies, is shredded.

Why a separate window and not the agent's own browser: Chrome allows one process per profile
and the session is already driving one, so a throwaway profile keeps the session alive and
paused rather than torn down. Why it matches the session's user agent and size: an anti-bot
clearance is bound to the client that earned it, so the window has to *be* that client — same
UA, same window, same machine (so the same IP) — or the pass does not fit when it comes back.

The window can outlast one call. If you are still working when the call returns, its status
is `waiting`; calling `confirm=true` again keeps waiting (no second window opens), and
`cancel=true` closes it. `latchkey_assist_status` says what is open. Only the site's own
cookies and known bot-vendor clearance cookies go into the window, never your whole jar, and
a live Google account session is held back as always. Turn the whole thing off with
`LATCHKEY_ASSIST=off` (for a server running where nobody is at the screen).

From a terminal it is one command, which blocks while you solve it:

```bash
python3 -m latchkey assist https://example.com/checkout
# example.com : challenged (Cloudflare challenge).
# Opening a Chrome window carrying this site's cookies... solve the check there.
# Carried 1 of 1 earned cookies back. The page now reads 'logged-in'.
```

## Idle browsers are closed for you

A session is a real Chrome process — cheap to keep for the next call, not free to keep
forever. A session that has run nothing and had nothing queued for `LATCHKEY_IDLE_TTL`
seconds (default 900; set `off` to disable) is closed, and its Chrome, and the memory, go
with it. Nothing is lost that a re-open would not restore. A **busy** session is never idle
whatever the clock says — a `wait_for_login` can sit for minutes with the browser genuinely
in use — and a session in the middle of a `latchkey_assist` window is protected too.

## Library

```python
from latchkey import Browser, logged_in

with logged_in() as browser:                 # every cookie, headless; Google in a clone
    state = browser.goto("https://chatgpt.com/")
    print(state.verdict, state.title)        # 'logged-in' | 'logged-out' | 'unclear'

    if state.verdict == "logged-out":
        # you sign in on your real browser; this blocks until the session arrives
        print(browser.wait_for_login("https://chatgpt.com/"))

    browser.click("nav a[href='/settings']")
    browser.fill("input[name=q]", "hello")
    browser.press("input[name=q]")           # Enter
    browser.scroll(1200)
    browser.screenshot("/tmp/out.png")
    print(browser.evaluate("() => document.title"))
    browser.save_session()                   # survives restarts

with Browser("github.com") as browser:       # only github's cookies
    print(browser.goto("https://github.com/").text[:200])
```

Several agents at once, each on its own Chrome and its own thread:

```python
from latchkey import SessionRegistry, SessionSpec

registry = SessionRegistry()
canvas = registry.get("canvas", spec=SessionSpec(mode="clone", label="canvas"))
mail = registry.get("mail", spec=SessionSpec(host="mail.example", label="mail"))

# these two navigations run at the same time; each session has its own cookie jar
print(canvas.submit(lambda b: b.goto("https://canvas.example/").verdict))
print(mail.submit(lambda b: b.goto("https://mail.example/").verdict))
registry.close_all()
```

## How it works, and the three things that bite

**1. Chrome's macOS cookie format is not the documented one.**
The recipe you find everywhere is `v10 || AES-128-CBC(plaintext)` with an IV of 16
spaces. On this build that produces output which is ~96% printable ASCII and *looks*
fine — but the first two 16-byte blocks are garbage. The real layout is
`v10 || 32-byte header || ciphertext`. `cookies.decrypt()` detects the header length
instead of assuming it.

**2. Playwright's `add_cookies` cannot set partitioned cookies.**
Cloudflare's `cf_clearance` is partitioned (293 cookies in this profile are). Playwright
has no way to express a partition key, so those cookies get dropped and ChatGPT responds
`Just a moment...` — a bot challenge that looks exactly like "our cookies are broken".
CDP's `Network.setCookies` **does** accept `partitionKey`, so injection goes through CDP.
That single change is what made ChatGPT work headless.

**3. A partition key needs both of its fields.** CDP rejects `partitionKey` given as just
`{topLevelSite}` with an unhelpful `Invalid parameters`. It also needs
`hasCrossSiteAncestor`. Every one of the 293 partitioned cookies failed to set until that
field was added; the failure is silent unless you count acceptances.

## Why not filter by host

The obvious optimisation is to inject only the cookies for the site you're visiting. It
breaks logins. `chatgpt.com` needs cookies from `.openai.com` and `.auth.openai.com` too;
filtering to `chatgpt.com` silently drops them and you get challenged. Loading everything
is slower to set up and more likely to just work.

## What a bot-detector sees, and what latchkey does about it

A session is read in four layers. Every vendor that decides whether to ask you to prove
you are a human - Cloudflare's bot score and Turnstile, DataDome, Akamai, HUMAN/PerimeterX,
reCAPTCHA's v3 score - weighs the first two most heavily, because they are the ones a
script cannot rewrite at run time:

| layer | what it reads | latchkey |
| --- | --- | --- |
| network | egress IP and ASN, the TLS client hello (JA3/JA4), header order | your own connection and Chrome's own TLS stack. Nothing to fix, and the one thing that would undo everything below is routing through a proxy or a VPS |
| browser | `navigator.webdriver`, the user-agent string, client hints, screen, `devicePixelRatio`, colour depth, languages, plugins, codecs, WebGL/GPU | real Chrome (`channel="chrome"`), so plugins, proprietary codecs, GPU and fonts are genuine; `webdriver` off; window, screen, scale and language read from this machine (`latchkey/fingerprint.py`) |
| behaviour | pointer paths, dwell before a click, keystrokes, scrolling, cadence | the real pointer walks to the element and lands inside it, keystrokes go through the keyboard, scrolling arrives in notches (`latchkey/human.py`). On by default; `LATCHKEY_HUMANIZE=0` or `humanize=False` turns it off |
| session | cookies, `cf_clearance`/`__cf_bm` history, and whether the device presenting them is the one they were issued to | your cookies, injected or cloned, and a fingerprint matched to the machine those cookies belong to |

### The fingerprint, concretely

Measured on this machine (macOS, Apple Silicon, built-in Retina display) with the same
Chrome binary, the numbers that used to disagree with a real Chrome window were:

| signal | real Chrome | latchkey before | latchkey now |
| --- | --- | --- | --- |
| screen | 1512x982, avail 1512x896 | 1280x820, avail == screen | 1512x982, avail from the work area (`--screen-info`) |
| `devicePixelRatio` | 2 | 1 | 2 |
| colour depth | 30 | 24 | 30 (`--screen-info`) |
| window chrome | 87px (title bar etc.) | none: `outer == inner` | 87px: a real window with bounds, the viewport derived from it |
| `outerWidth` getter | `[native code]` | `() => values[name]` (an init script) | `[native code]` - nothing is patched |
| client-hint architecture | `arm` | `x86` | `arm` |
| service worker user agent | `Chrome/153.0.8010.37` | `HeadlessChrome/153.0.0.0` (on the wire) | `Chrome/153.0.8010.37` (`--user-agent`) |
| where latchkey's own JS runs | - | the main world, via `UtilityScript` | an isolated world (`world.py`) |
| `navigator.languages` | `en-US,en` | `en-US` | the profile's own value |

Three of those were found by putting a session in front of real detectors (rebrowser's
bot-detector, CreepJS, and `httpbin.org/headers` fetched from inside a site's service worker):

* **The user agent is a launch switch, `--user-agent`.** Playwright's `user_agent=` is a
  per-target CDP override, and a site's *service worker* is a target it never reaches: every
  fetch the worker made left with `HeadlessChrome` in its header, while the page next to it
  said `Chrome`. Cloudflare reads the header before it runs a line of JavaScript. A cloned
  profile brings its registered service workers with it, which is why clone mode met walls
  that inject walked through. The switch is browser-wide.
* **latchkey's own JavaScript runs in an isolated world.** `page.evaluate` runs in the page's
  main world through a wrapper called `UtilityScript`; a getter that reads `new Error().stack`
  sees it, and a site that wraps `document.getElementsByClassName` sees every probe.
  `latchkey/world.py` makes a world of latchkey's own (`Page.createIsolatedWorld`) and runs
  functions in it directly (`Runtime.callFunctionOn`): same DOM, its own globals, no wrapper.
  Everything internal goes through `Driver.run_js`.
* **Nothing in the window is a patched getter.** The screen, its work area, colour depth and
  scale are new headless's virtual display, described by `--screen-info` in physical pixels;
  the window has real bounds (`Browser.setWindowBounds`), and new headless takes the same
  87px of browser chrome off a window's height to get its viewport that a macOS window has.
  No `Emulation.setDeviceMetricsOverride`, no `viewport=`, no init script.

The `x86` was the subtle one: Playwright *derives* client-hint metadata from whatever
user-agent string it is handed, so an "Intel Mac OS X" user agent made an M-series machine
report an Intel CPU to any page that asked. The metadata is *measured*: latchkey serves
itself a blank page on `127.0.0.1` (a secure context, and the only kind where Chrome will
answer `getHighEntropyValues`), reads the browser's own brand list from it, takes
`architecture` from the machine and `platformVersion` from the OS - `sw_vers` prints
exactly what an untouched Chrome reports (`26.6.2` here) - rebuilds the full version list
from the brands and the binary's version (the `--user-agent` switch blanks it), and applies
the user agent per page with all of it. A CDP user-agent override that does **not** carry the
metadata clears the hints entirely - brands `[]`, platform and architecture empty - which is
a louder signal than the `x86` it was meant to fix, so the metadata is always supplied in full.

What is still not a real browser, stated rather than hidden: a service worker's own
high-entropy hints are blank under `--user-agent` (its brands and platform are right, its
`architecture` is empty); an out-of-process iframe is read through Playwright, in its main
world; and `speechSynthesis` has no voices in headless. `examples/verify_fingerprint.py`
prints every signal next to the machine's own numbers.

That measurement costs about half a second - a local server, an iframe and a read - and the
answer is the same on every open, because the brand list and full version list belong to the
Chrome binary and the machine, not the session. So it is **cached on disk**, keyed by Chrome
version and platform (`~/.latchkey/client-hints.json`), and the probe runs only on a miss: a
fresh install, a Chrome update, or a cleared cache. `LATCHKEY_HINTS_CACHE=0` forces the live
probe on every open, which is the way to re-measure by hand.

### Why clone mode meets walls that inject mode does not

A clone carries the profile's `cf_clearance` and `__cf_bm`, which were issued to the *real*
Chrome: 1512x982 at dpr 2. A clone presenting a 1280x820 viewport at dpr 1 is a client
holding somebody else's pass, and a stale pass is worse than none. Matching the machine is
what makes the clone look like the browser the cookie belongs to.

There is a second possibility - that carrying any clearance issued elsewhere is what
flags it - and the two are separable:

```bash
python3 examples/verify_clone_ab.py                 # baseline, old-config, no-clearance
```

If `no-clearance` clears the wall and `baseline` does not, it is the stale cookie. If
`baseline` clears it and `old-config` does not, it is the device. If all three are walled,
it is the egress IP or the profile's reputation, and nothing inside the browser will fix it.

### Who walled us, and on what evidence

`state.wall` names the vendor and the evidence, because "blocked" on Reddit, ChatGPT and a
bank need three different responses:

```python
state = browser.state()
state.verdict           # logged-in / logged-out / unclear / blocked / challenged
state.wall              # {'vendor': 'cloudflare', 'kind': 'challenge', 'confidence': 'high',
                        #  'reasons': ['cookie cf_clearance', 'page says "just a moment"'],
                        #  'sentence': 'Cloudflare challenge HTTP 403 high - cookie cf_clearance'}
```

Evidence is graded, because `server: cloudflare` is true of most of the web: a vendor cookie,
a challenge marker or a distinctive phrase can name a vendor and decide what kind of wall it
is; a response header alone cannot. A healthy page behind Cloudflare produces no wall at all,
and a permission-denied 403 with a real page under it is not called a bot wall.

### Checking it

```bash
python3 examples/verify_fingerprint.py              # every signal, next to this machine's
python3 examples/verify_fingerprint.py --sannysoft  # and bot.sannysoft.com's checks
python3 examples/verify_fingerprint.py --json       # for the sweep table
```

## Security

This tool is your entire authenticated identity. Be deliberate about it.

- **Cookie values never touch disk and are never logged.** They are decrypted into
  memory, injected, and discarded with the process. Only tests/summaries are printable.
- **`--host` narrows the blast radius** if you want it: `Browser("github.com")` touches
  only GitHub's cookies.
- **localStorage is not encrypted.** Chrome encrypts cookies and needs the Keychain for
  them; localStorage is a plaintext LevelDB. Any process running as you can read every
  site's localStorage with no prompt. Do not treat this project as the thing protecting
  that.
- **The Keychain prompt** appears on first use, asking to release "Chrome Safe Storage".
  That key decrypts every cookie in the profile.
- **An agent driving this can act as you** on every site you're logged into. Prefer
  `--host` scoping when you hand it to an agent you don't fully trust, and consider opening
  the session read-only: `latchkey_session_open(read_only=true)`, or `LATCHKEY_READ_ONLY=1`
  in the server's environment, which is a floor no tool call can lift. Read-only refuses the
  verbs that send something — click, fill, press, select, check, upload, `eval` — and still
  allows reading, navigating, tabs and screenshots. `eval` counts as a write because
  `fetch(url, {method: 'POST'})` is one line.
- **A session goes to the web and nowhere else.** `http` and `https`, plus `about:blank`.
  Read-only refuses the verbs that *send*, which says nothing about where the browser
  goes - and a browser is a perfectly good local file reader, so a read-only session
  could open `file:///Users/you/.ssh/id_rsa` and read it back with `latchkey_text` like
  any other page. `LATCHKEY_ALLOW_SCHEMES=file` opens that, and only the human running
  the server can set it. A bare `example.com` is read as `https://example.com`, the way
  an address bar does.
- **A credential never leaves the page.** The field is decided by the walker, on the
  element - `type=password`, or a name, id, `autocomplete` or label that reads like a
  secret - and the ref carries it, because a ref selector says nothing about what it
  points at and acting by ref is the flow every tool description recommends. A fill on
  one reports `(hidden)`, and the snapshot renders `filled (28 chars, hidden)` rather
  than the value. Diff mode still notices it being typed.
- **The viewer answers loopback only.** It sends no CORS header, which stops an ordinary
  cross-origin read but not **DNS rebinding**: a page on a name that resolves to
  127.0.0.1 is same-origin with the viewer as far as the browser is concerned, and could
  read your URLs, titles, typed values and screen off `/events`. The header that attack
  cannot forge is `Host`, so a request addressed to anything but loopback gets a 403.

## Known limitations

- **Sites that keep auth in IndexedDB.** Use `--mode clone`. Cookie injection cannot
  carry IndexedDB, which is why ChatGPT-style SPAs need the clone route even though their
  cookies decrypt fine.
- **Session-only cookies** (184 here, no expiry) die when you quit Chrome. The clone route
  carries them, because they are in the cloned database.
- **The logged-in verdict is a heuristic.** It looks for a visible avatar or account
  control, a visible sign-in call-to-action, a visible password field, and a sign-in step
  (Google's sign-in URLs, a signed-out account chooser, a passkey prompt) - the last two
  outrank a generic avatar marker, which Google's own sign-in pages are full of. A site with an
  unusual DOM may report `unclear` rather than guess — see below.
- **A closed shadow root is genuinely not there.** The snapshot crosses *open* ones, which
  is what a site built on web components needs, but a closed root is invisible to every
  script including this one.
- **The snapshot stops at 1500 things** and says how many it skipped. Pass a `selector`
  to read one part of a page that has more.

### Why the clone has to use `cp -cR`, not rsync

The first attempt at profile reuse failed, and the reason was not what I assumed. rsync
copying a live profile **silently dropped `Default/Cookies` entirely** — the file was not
in the destination at all — so Chrome started, found no cookie database, created a fresh
empty one (20KB of empty SQLite pages against the original's 1.8MB), and every site looked
signed out. Adding Chromium's inherited `SingletonLock` on top of that made Chrome refuse
to start outright.

An APFS clone fixes both: `cp -cR` copies the tree correctly in ~4s, and `profile.clone()`
deletes the inherited `SingletonLock`, `SingletonCookie` and `SingletonSocket`. If you ever
see a clone whose `Default/Cookies` has no rows, `clone()` now raises instead of handing you
a quietly logged-out browser.

## The `verdict` field (a hint, not a gate)

`verdict` is a convenience, not a source of truth. It is a DOM heuristic, and it reports
`unclear` on plenty of sites that are plainly signed in — **Canvas is the standing example**,
because its account control has no accessible name. Never let an agent refuse to act because
the verdict says `unclear`. The real signal is the content: if `title` and `text` show the
user's courses, inbox, feed or account menu, they are signed in.

| Verdict | Meaning | What to do |
|---|---|---|
| `logged-in` | a visible avatar/account control, or a known site marker | proceed |
| `logged-out` | a visible sign-in call-to-action or password field, a Google sign-in page, an account chooser whose accounts are "Signed out", a passkey or "verify it's you" prompt (`signin` names which) | follow `next_step`: Google needs `latchkey_login`; other sites, the user signs in in their own Chrome, then `wait_for_login`. Never ask for a password in chat |
| `blocked` | an anti-bot wall (Cloudflare, "unusual traffic", Reddit network security) | **not** a login problem — do not ask the user to sign in |
| `challenged` | a wall that is *asking* something: Cloudflare's "Verify you are human", a Turnstile box, an invisible reCAPTCHA that has already voted against this session | wait a few seconds and read again (`state.wall` names the vendor, and says whether it is a `challenge` or a `block`) |
| `unclear` | no evidence either way | **proceed**; read `text`, screenshot only if genuinely unsure |

`blocked` is checked first, because the failure it prevents is an agent looping — asking the
user to log in over and over for a site that is already signed in but rate-limited.
`unclear` is the common case for sites with unusual DOMs, and it means "I could not tell",
not "signed out". The batch tool's description - the one thing an agent reads on every
turn - carries the same table, and `latchkey_help({"name": "latchkey_open"})` carries it
for that tool alone.

## Embedded content lives in iframes

`text`, `links` and `eval` only see the **main frame**. Anything inside an `<iframe>`
is invisible to them — and on a real course page that is where the content is. Canvas
demonstrates it exactly:

```
/courses/1194250/modules/items/13415204   ->   "APES Calendar"
  <iframe title="APES Calendar"
          src="https://docs.google.com/document/d/1JwXnYIeuxaz0j5U_96wLoZa9SL3pJ5NeRCSchjFDlRM/">

top-frame text:  'Skip To Content | Account | Dashboard | Courses | Calendar | Inbox ...'
frame[2]      :  the actual document
```

The top frame shows only Canvas's navigation chrome while the document renders on screen.
So a search for a word in `text` can come back empty on a page that is visibly showing it.
When content is missing, or the page looks emptier than the screenshot:

```python
for f in browser.frames():
    print(f["index"], f["url"])          # the doc is a frame, not a link
print(browser.frame_text(2))              # read inside it
```

Two related traps from the same site:

- **Raw-HTML searches lie.** Searching `page.content()` for "slide" matched
  `mejs__volume-slider` in a stylesheet. Match on rendered text, not markup.
- **A modal can swallow every click.** Canvas opens a `reactour__helper` welcome tour whose
  backdrop marks the root `aria-hidden`, making otherwise-visible elements unclickable. The
  resulting `TimeoutError` is indistinguishable from a missing element. Use
  `{"do": "click", ..., "force": true}`, or dismiss the tour first.

## When a site reports `unclear`

The verdict is a heuristic: it looks for a visible avatar or account control, a visible
sign-in call-to-action, and a visible password field. That is right on all five verified
sites, but some sites build the account control out of `div`s with no accessible name, so
no generic selector can find it. ChatGPT is exactly that case, which is why it ships with
a known marker for `create-new-chat-button`.

For any other stubborn site, add a hint rather than guessing:

```json
// ~/.latchkey/hints.json
{ "example.com": ["[data-testid='account-menu']"] }
```

The host key is a substring match, so one entry covers `www.example.com` too. `unclear`
means "no evidence either way" — not "broken" — and it is always safe for an agent to
treat it as "check with a screenshot".

## Validation and limits

The test suite covers routing, cookie selection, session lifecycle, the MCP surface,
and UI behavior with fixtures. Browser-specific behavior still depends on Chrome,
the site's login flow, and anti-bot challenges; a verdict of `unclear` means the
page needs human inspection. `blocked` is reported separately from `logged-out`.

Cookie injection can transfer cookie state but not IndexedDB or service workers.
Clone mode carries more browser state, though sites may challenge an automated
clone. Dedicated mode uses a separate profile that the operator signs in to once.

Latchkey handles live account sessions. Run it only on a machine and with agents
you trust. The MCP server can navigate to arbitrary HTTP(S) URLs and, unless
`LATCHKEY_READ_ONLY=1` is set, can perform actions in those sessions. Keep the
server local and review the permissions of the agent that uses it.
