"""What a page is: signed in, signed out, locked out, or walled off by an anti-bot wall.

The answer is one of `logged-in` / `logged-out` / `unclear` / `blocked` / `challenged`,
and the last two are different problems: a **block** is a refusal to serve this client,
a **challenge** is a test it is being asked to pass (Cloudflare's "Verify you are human",
a Turnstile checkbox, an invisible reCAPTCHA that has already decided against the
session). A challenge can clear itself in five seconds; a block will not, and an agent
that cannot tell them apart either gives up on a page that was about to work or waits
on a page that never will. `walls.py` says who is asking.

One script answers every question `state()` asks - the visible body text, the
sign-in call to action, a signed-in marker, a password field, and a block page -
because `state()` runs after every action and each of those used to be its own
`page.evaluate`. A page read now costs one round trip instead of four.

The answer is memoised per `(url, epoch)`. The epoch is bumped by anything that
could have changed the page - a navigation, a click, an eval - so reading an
unchanged page twice is free, and a page that has changed is never served a
stale verdict. There is no per-site special case anywhere in here: the hints
below are data, and a site that needs one can add it without an edit.
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass, field
from typing import Any

from . import google
from . import paths
from . import walls as walls_mod

# How much body text one probe carries back. Generous, because the whole point of
# the read is that an agent can look at the page; trimmed per call by text_limit.
BODY_LIMIT = 20_000

# Selectors that generically indicate a signed-in session.
#
# `user-menu` earns its place from a real miss: claude.ai was signed in and read
# `unclear`, because its account control is a button whose only distinguishing
# feature is data-testid="user-menu-button" - no avatar, no aria-label, nothing the
# other selectors could see. A visible *settings* button is not a marker: it exists
# when signed out too (claude.ai has one, hidden until the account menu opens).
#
# `[aria-label*="account" i]` is not a marker either, and it used to be. Google's own sign-in
# pages are full of it - "Open Google Account Help Center", "you@gmail.com selected. Switch
# account" - so on 11 September the password page and an account chooser listing both
# accounts as "Signed out" were reported `logged-in`, and the model was told to proceed.
# What is left is the label Google gives the avatar of a signed-in account, and a menu.
# The bare avatar/profile <img> selectors are scoped to the page's top chrome
# (header / nav / banner / an account-menu container). A real account avatar lives there; a
# content-area image whose alt happens to contain "avatar" does not. eBay's logged-out
# homepage carries a promo `<img alt="Event host avatar">` in page content that matched the
# unscoped selector, so eBay read `logged-in` while signed out. Sites that put their avatar in
# the top chrome (YouTube's #avatar-btn, Google's aria-labelled avatar) still match here and
# via the precise selectors below.
_CHROME = ':is(header,nav,[role="banner"],[class*="header" i],[id*="header" i],[class*="masthead" i],[aria-label*="account" i])'
GENERIC_MARKERS = (
    f'{_CHROME} img[alt*="avatar" i]', f'{_CHROME} img[alt*="profile" i]',
    f'{_CHROME} img[class*="avatar" i]',
    'header img[src*="avatar" i]',
    '[aria-label*="avatar" i]', '[aria-label*="profile" i]',
    '[aria-label^="Google Account:" i]', '[aria-label*="account menu" i]',
    '[aria-label*="user menu" i]', '[aria-label*="open profile" i]',
    '[data-testid*="avatar"]', '[data-testid*="profile"]', '[data-testid*="user-menu"]',
    'button[aria-haspopup] img',
    '#avatar-btn', '[data-testid="user-drawer"]', '#expand-user-drawer-button',
)

# Some sites build the account control out of divs with no accessible name, so no
# generic selector can find it. For those, check a known marker first. Add your
# own in ~/.latchkey/hints.json: {"example.com": ["#account-menu"]}
SITE_MARKERS = {
    "chatgpt.com": ["[data-testid='create-new-chat-button']",
                    "[data-testid='close-sidebar-button']"],
}
HINTS_FILE = paths.path("hints.json")

# Only count elements the user could actually see. Signed-in pages routinely keep
# hidden sign-in links in the DOM, which made YouTube look logged out while its
# avatar and notification count said otherwise.
LOGIN_HINTS = ("log in", "login", "sign in", "sign up", "signin", "join now", "get started")

# Anti-bot walls, kept separate from "logged out": an agent that mistakes a block for a
# login problem will ask the user to sign in forever. Reddit served one of these to the
# cloned profile while the injected session on the same account was fine.
BLOCK_HINTS = ("you've been blocked", "you have been blocked", "network security",
               "access denied", "unusual traffic", "are you a robot",
               "verify you are human", "checking your browser", "just a moment",
               "enable javascript and cookies to continue", "attention required")

# The passable half of the same problem. These are the phrases a *challenge* shows -
# the ones a client with a good fingerprint passes by waiting, which is why they get
# their own verdict instead of being folded in with a refusal. "are you a robot" and
# "verify you are human" appear in both lists on purpose: the text is what a wall says,
# and which list it came from is not the deciding evidence - `walls.py` is, because a
# challenge widget or a `cf_clearance` cookie says which kind of page this really is.
CHALLENGE_HINTS = ("just a moment", "verify you are human", "verifying you are human",
                   "are you a robot", "checking your browser", "one more step",
                   "checking if the site connection is secure",
                   "needs to review the security of your connection",
                   "please verify you are a human", "enable javascript and cookies to",
                   "ddos protection by", "protected by recaptcha", "hcaptcha",
                   "you are now in line")

# Selectors whose typed content must never reach an event, a log or a viewer.
SECRET_HINTS = ("pass", "secret", "token", "otp", "cvv", "ssn", "pin")


@functools.lru_cache(maxsize=4)
def _hints_file(_stamp: tuple) -> tuple:
    """The user's hint file, parsed. Keyed on its mtime, so an edit lands immediately.

    `state()` runs after every action, and this used to open and parse the file each
    time - a syscall and a JSON parse on the hottest path in the package, to read a file
    that changes when somebody edits it by hand.
    """
    try:
        with open(HINTS_FILE, encoding="utf-8") as fh:
            loaded = json.load(fh)
        return tuple((str(key), tuple(str(s) for s in selectors))
                     for key, selectors in loaded.items() if isinstance(selectors, list))
    except (OSError, json.JSONDecodeError, AttributeError):
        return ()


def _hints_stamp() -> tuple:
    try:
        info = os.stat(HINTS_FILE)
    except OSError:
        return ()
    return (info.st_mtime_ns, info.st_size)


def _site_hints(url: str) -> list[str]:
    """Per-site marker selectors for this URL, from the built-ins and the user file."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    hints: list[str] = []
    for key, selectors in _hints_file(_hints_stamp()):
        if key in host:
            hints.extend(selectors)
    for key, selectors in SITE_MARKERS.items():
        if key in host:
            hints.extend(selectors)
    return hints


def secretish(selector: str) -> bool:
    """Would showing what gets typed into this field leak a credential?"""
    low = (selector or "").lower()
    return any(hint in low for hint in SECRET_HINTS) or "type=password" in low


# The whole probe. Returns the body text and every verdict signal in one result,
# so one round trip answers `state()` completely. Everything site-specific is
# passed in as configuration; nothing about a particular site is compiled in.
PROBE_JS = r"""(cfg) => {
  const vis = e => e.checkVisibility
      ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
      : e.offsetParent !== null;
  const title = document.title || '';
  const body = document.body ? document.body.innerText : '';
  const hay = (title + ' ' + body.slice(0, 1500)).toLowerCase();

  let prompt = null;
  for (const el of document.querySelectorAll('button, a, [role=button]')) {
    if (!vis(el)) continue;
    const t = (el.innerText || '').trim().toLowerCase();
    if (t.length < 24 && cfg.login.some(h => t === h || t.startsWith(h))) {
      prompt = el.innerText.trim();
      break;
    }
  }

  let marker = null, markerSite = false;
  for (let i = 0; i < cfg.markers.length; i++) {
    let el = null;
    try { el = document.querySelector(cfg.markers[i]); } catch (err) { continue; }
    if (el && vis(el)) {
      marker = el.getAttribute('aria-label') || el.getAttribute('alt')
          || el.tagName.toLowerCase();
      markerSite = i < (cfg.site_markers || 0);
      break;
    }
  }

  const password = [...document.querySelectorAll('input[type=password]')].some(vis);
  const blocked = cfg.block.find(h => hay.includes(h)) || null;
  const challenged = cfg.challenge.find(h => hay.includes(h)) || null;

  // The wall sweep asks twenty-odd selectors, so it runs only on a page that looks
  // walled already, or on one with barely any text on it - a challenge page is a few
  // hundred characters. Ordinary reads pay nothing for it.
  const lean = body.length < 2000;
  const selectors = [], urls = [];
  if (blocked || challenged || lean) {
    for (const sel of cfg.wall_markers) {
      let el = null;
      try { el = document.querySelector(sel); } catch (err) { continue; }
      if (!el) continue;
      selectors.push(sel);
      const src = el.getAttribute && (el.getAttribute('src') || el.getAttribute('data-src'));
      if (src) urls.push(src);
    }
    for (const node of document.querySelectorAll('script[src], iframe[src]')) {
      const src = node.getAttribute('src') || '';
      if (/challenge|turnstile|recaptcha|captcha|sensor|ips\.js|akam/i.test(src)) urls.push(src);
    }
  }

  return {title: title, body: body.slice(0, cfg.body_limit), login_prompt: prompt,
          logged_in_marker: marker, marker_site: markerSite,
          password_field: password, blocked: blocked,
          challenged: challenged, selectors: selectors, urls: urls.slice(0, 12)};
}"""


@dataclass
class PageState:
    """What an agent gets back after an action."""
    url: str
    title: str
    text: str = ""
    login_prompt: str | None = None
    logged_in_marker: str | None = None
    password_field: bool = False
    blocked: str | None = None
    challenged: str | None = None   # the phrase a challenge page shows, if it shows one
    # Who we think stopped us, from walls.py: vendor, kind (block/challenge/queue),
    # confidence and the evidence behind it. Empty when nothing suggests a wall at all.
    # Filled in by PageProbe.state with whatever the session already knows - the page's
    # text and markers, the document response's status and headers, the cookie names
    # Chrome is holding for the host.
    wall: dict = field(default_factory=dict)
    tab: str = ""                 # the stable id of the tab this was read from
    tabs: int = 1                 # how many tabs the session has open
    # Cookies this session carries that apply to the page's host, as a count. None means
    # "not known", and the verdict then leans on the page alone. Only the zero case is
    # meaningful: it means this session holds nothing for that site, whatever the page
    # looks like. There are two ways to be known - the domains collected while injecting,
    # which is free, and one question to Chrome per navigation for a cloned profile
    # (`Driver.host_cookies`) - and no verdict moves on a None.
    host_cookies: int | None = None
    # Why this page is a step of signing in - a Google sign-in URL, a password or passkey
    # prompt, an account chooser whose accounts are signed out - or None. A sign-in step
    # outranks a generic signed-in marker, because those pages are full of account-shaped
    # labels.
    signin: str | None = None
    marker_site: bool = False     # the marker came from a site's own hint, not the generic list

    @property
    def verdict(self) -> str:
        """One word an agent can branch on.

        challenged / blocked / logged-in / logged-out / unclear. The wall answers come
        first because asking the user to sign in does not fix an anti-bot wall, and
        because the two kinds of wall want different things: a `challenged` page is worth
        waiting on (a managed challenge usually clears in seconds for a client that looks
        like a browser - that is the whole design), a `blocked` page is not.

        Which of the two it is comes from `walls.py`, not from the wording on the page:
        "just a moment" is a challenge when a challenge widget is behind it and a refusal
        when "Attention Required!" is above it, and only the evidence says which.

        `logged-in` needs two things: something on the page that only a signed-in
        visitor would see, and a session that actually carries a cookie for that host.
        The second half came out of the site sweep: instagram.com, steamcommunity.com
        and stackoverflow.com all read `logged-in` on a signed-out session, because a
        public page has plenty of profile-shaped markup in it. In `clone` that question
        goes to Chrome instead of to our injected domains - once per navigation, not per
        read (`Driver.host_cookies`) - so the rule is the same in both modes.

        When a marker has no cookie behind it *and* the page is showing a sign-in call
        to action, the answer is `logged-out`, not `unclear`: webassign.net's landing
        page has both a "SIGN IN" button and an image whose alt text reads like a user
        avatar, and `unclear` there would hide a page that is plainly asking to be
        signed in to.
        """
        kind = str((self.wall or {}).get("kind") or "")
        if kind in ("challenge", "queue") or (self.challenged and kind != "block"):
            return "challenged"
        if self.blocked or kind in ("block", "rate_limit"):
            return "blocked"
        signin = self.signin or google.signin_step(self.url)
        signed_out = bool(self.login_prompt or self.password_field or signin)
        if self.logged_in_marker and self.marker_site:
            # A site's own marker is evidence a generic label is not, and survives a password
            # field (a signed-in settings page can have one).
            if self.host_cookies == 0:
                return "logged-out" if signed_out else "unclear"
            return "logged-in"
        if signin or self.password_field:
            # A sign-in step is a sign-in step whatever else is on it. This is what the
            # Google password page and the "Signed out" account chooser were missing.
            return "logged-out"
        if self.logged_in_marker:
            if self.host_cookies == 0:
                return "logged-out" if signed_out else "unclear"
            return "logged-in"
        if signed_out:
            return "logged-out"
        return "unclear"

    def as_dict(self, detail: bool = False) -> dict:
        """The page as a reply - small on purpose.

        Without `detail` the page's *text* is left out and only its size is mentioned. That is
        the difference between an agent that can afford twenty actions and one that cannot:
        four kilobytes of page text pasted into the reply to "click the Login button" is
        context spent on nothing, and the text is still there to be read deliberately with
        `latchkey_text` or `latchkey_snapshot`. `detail=True` is for a human at a terminal,
        which is the one caller that wants it inline.
        """
        out = {"url": short_url(self.url), "title": self.title, "verdict": self.verdict,
               "tab": self.tab, "tabs": self.tabs,
               "host_cookies": self.host_cookies,
               "login_prompt": self.login_prompt,
               "logged_in_marker": self.logged_in_marker,
               "password_field": self.password_field, "blocked": self.blocked,
               "challenged": self.challenged, "wall": self.wall}
        signin = self.signin or google.signin_step(self.url)
        if signin:
            out["signin"] = signin
        if detail:
            out["text"] = self.text
        elif self.text:
            out["text_chars"] = len(self.text)
        return out

    def summary(self) -> dict:
        """The always-relevant part, for event detail."""
        out = {"url": short_url(self.url), "title": self.title, "verdict": self.verdict,
               "tab": self.tab}
        if self.wall:
            out["wall"] = self.wall.get("sentence") or self.wall.get("vendor") or ""
        return out


def short_url(url: str, limit: int = 200) -> str:
    """A URL small enough to sit in a reply.

    A `data:` URL is a *page*, not an address, and pasting one into the reply to "click
    Continue" costs more tokens than the page's own text would have - which is the exact thing
    these replies exist to avoid. Real URLs are short and pass through untouched.
    """
    if not url or len(url) <= limit:
        return url
    if url.startswith("data:"):
        return f"data: page ({len(url)} characters of markup)"
    return url[:limit - 1] + "…"


@dataclass(frozen=True)
class Probe:
    """One page's raw answer, before it is shaped into a PageState."""
    title: str = ""
    body: str = ""
    login_prompt: str | None = None
    logged_in_marker: str | None = None
    password_field: bool = False
    blocked: str | None = None
    challenged: str | None = None
    marker_site: bool = False
    markers: tuple[str, ...] = ()     # wall selectors that matched, for attribution
    urls: tuple[str, ...] = ()        # challenge-ish script/iframe srcs

    @classmethod
    def from_js(cls, value: dict) -> "Probe":
        return cls(title=str(value.get("title") or ""),
                   body=str(value.get("body") or ""),
                   login_prompt=value.get("login_prompt") or None,
                   logged_in_marker=value.get("logged_in_marker") or None,
                   password_field=bool(value.get("password_field")),
                   blocked=value.get("blocked") or None,
                   challenged=value.get("challenged") or None,
                   marker_site=bool(value.get("marker_site")),
                   markers=tuple(str(s) for s in (value.get("selectors") or ())),
                   urls=tuple(str(s) for s in (value.get("urls") or ())))


class PageProbe:
    """Reads a page's state in one round trip, and does not read it twice.

    Called after every action, so the cheap path matters: an unchanged page
    answers from memory, and only a real change costs a round trip.
    """

    def __init__(self) -> None:
        self._key: tuple[str, int] | None = None
        self._value: Probe | None = None

    def invalidate(self) -> None:
        self._key = self._value = None

    def read(self, page: Any, url: str, epoch: int, run: Any = None) -> Probe | None:
        """The current page, or None if it could not be read.

        A page that is mid-navigation throws here; that is not a verdict, so it
        is reported as unreadable and never cached. `run` is the session's `run_js`,
        which asks from the isolated world; without it the page is asked directly.
        """
        key = (url, epoch)
        if key == self._key and self._value is not None:
            return self._value
        site = _site_hints(url)
        cfg = {"markers": site + list(GENERIC_MARKERS), "site_markers": len(site),
               "login": list(LOGIN_HINTS), "block": list(BLOCK_HINTS),
               "challenge": list(CHALLENGE_HINTS),
               "wall_markers": list(walls_mod.MARKERS),
               "body_limit": BODY_LIMIT}
        try:
            value = run(PROBE_JS, cfg) if run is not None else page.evaluate(PROBE_JS, cfg)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(value, dict):
            return None
        probe = Probe.from_js(value)
        self._key, self._value = key, probe
        return probe

    def state(self, page: Any, url: str, epoch: int,
              text_limit: int = 4000, *, response: dict | None = None,
              cookie_names: Any = None, run: Any = None) -> PageState:
        """The page, plus who walled it off if anyone did.

        `response` is the last document response the session saw ({status, headers, url})
        and `cookie_names` is a callable, not a list, because answering it costs a round
        trip to Chrome: it is only asked for on a page that already looks walled.
        """
        probe = self.read(page, url, epoch, run)
        if probe is None:
            return PageState(url=url, title="")
        wall = self._wall(probe, response=response, cookie_names=cookie_names)
        return PageState(url=url, title=probe.title,
                         text=probe.body[:text_limit].strip(),
                         login_prompt=probe.login_prompt,
                         logged_in_marker=probe.logged_in_marker,
                         password_field=probe.password_field,
                         blocked=probe.blocked, challenged=probe.challenged,
                         wall=wall.as_dict() if wall.known else {},
                         signin=google.signin_page(url, probe.title, probe.body),
                         marker_site=probe.marker_site)

    def _wall(self, probe: Probe, *, response: dict | None = None,
              cookie_names: Any = None) -> "walls_mod.Wall":
        """Name the wall, but only when something already says there is one.

        A 403 with no text, a challenge marker, block wording - any of those is worth a
        question to the cookie jar and a look at the response headers. A healthy page is
        not, which is why the common read stays at one round trip.
        """
        response = response or {}
        status = response.get("status")
        if not (probe.blocked or probe.challenged or probe.markers
                or status in walls_mod.DENIAL_STATUSES):
            return walls_mod.Wall(status=status if isinstance(status, int) else None)
        names = cookie_names() if callable(cookie_names) else (cookie_names or [])
        wall = walls_mod.from_page({"title": probe.title, "body": probe.body,
                                    "urls": list(probe.urls),
                                    "selectors": list(probe.markers)},
                                   headers=response.get("headers"),
                                   cookie_names=names, status=status)
        # A vendor named by a header alone (half the web answers `cf-ray`) is worth
        # nothing to an agent, and putting it on every read of every Cloudflare-hosted
        # site would be noise. Only keep a wall we can actually say something about.
        if not (wall.strong or wall.kind):
            return walls_mod.Wall(status=status if isinstance(status, int) else None)
        return wall
