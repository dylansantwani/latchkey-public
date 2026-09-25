"""This machine's own display, and the browser identity that matches it.

Everything latchkey drives is *real Chrome*, so almost everything a fingerprinting
script reads is genuine. What is not genuine is the window a Playwright launch
hangs that Chrome in. Measured on this machine (macOS, Apple Silicon, built-in
Retina display), with the same Chrome binary:

    signal        real Chrome, no emulation     latchkey's old launch config
    ------------  ----------------------------  -----------------------------
    screen        1512x982, avail 1512x896      1280x820, avail 1280x820
    dpr           2                             1
    colorDepth    30                            24
    window chrome outer-inner = 87px            outer == inner (none)
    arch hint     "arm"                         "x86"  (Playwright derives the
                                                        hint from the Intel UA
                                                        string it was handed)
    languages     en-US,en                      en-US

None of that is a "stealth flag"; it is the difference between a browser someone
is looking at and a viewport an automation framework painted. It matters most in
*clone* mode, where the profile's `cf_clearance` and `__cf_bm` were issued to the
first column and the clone then presents the second - a cookie that does not match
the client presenting it is a reason to challenge, not to trust.

The policy here is faithfulness, not invention: report this machine's real numbers,
and report them *natively*. Every one of them is now Chrome's own, set where Chrome
sets it, with no JavaScript in the page:

* the screen, its work area, its colour depth and its scale go in as a launch
  switch, `--screen-info` (`screen_info_switch`): new headless has a virtual display,
  and this is how it is described. Physical pixels, so a 2x screen is given twice
  its CSS size;
* the window - `outerWidth/outerHeight`, `screenX/screenY` - is a real window with
  bounds (`Browser.setWindowBounds`, `apply_window_bounds`). New headless subtracts
  the same 87px of browser chrome from a window's height to get its viewport that a
  real macOS window has, so the inner size follows from the outer one, as it should;
* the user agent is a launch switch, `--user-agent`, which reaches every target -
  service workers included, which a per-page CDP override never did: a fetch made by
  a site's service worker used to leave with `HeadlessChrome` in its header, and
  no per-page emulation could have reached it;
* the client hints go in over CDP per page, because `--user-agent` blanks the
  high-entropy ones (`architecture`, `platformVersion`, the full version list) and
  those have to be put back from what the machine and the binary actually are.

The older approach - `Emulation.setDeviceMetricsOverride` for the screen and a
context init script redefining `outerWidth`, `screenX` and `Screen.prototype.availHeight`
- reported the right numbers through the wrong getters: `() => values[name]` where a
page expects `[native code]`, an own accessor with no setter, a prototype patched
from outside. A detector that compares descriptors reads that as a lie about a
window, which is worse than an honest headless window. None of that runs any more.
"""
from __future__ import annotations

import functools
import json
import os
import platform as _platform
import re
import subprocess
import sys
from dataclasses import dataclass, field

from .cookies import CHROME_ROOT, chrome_version

# NSScreen through the ObjC bridge: the only place that gives both the CSS-point
# frame (what `screen.width` reports) and the backing scale factor, and the
# visible frame that `screen.availHeight` reports. `osascript` needs no
# permission for this and no extra dependency.
JXA_DISPLAY = """
ObjC.import('AppKit');
const s = $.NSScreen.mainScreen;
const f = s.frame, v = s.visibleFrame;
JSON.stringify({w: f.size.width, h: f.size.height,
                aw: v.size.width, ah: v.size.height,
                ax: v.origin.x, ay: v.origin.y,
                scale: s.backingScaleFactor});
"""

# The visible frame is the *work area*, and its origin is how far up the screen it
# starts: here origin.y is 53 (the Dock) and the frame is 896 tall, so on a 982-tall
# screen the work area's top - the menu bar - is 33 - which is exactly what the
# user's own Chrome records as `work_area_top` in its Preferences.

# What no OS call here answers, measured on this machine instead:
#
# * the browser chrome above a real Chrome window's content area - title bar, tab
#   strip and omnibox, 87px, so a window and its viewport differ by that much;
# * the fallback work area, used only when a reading could not see one: the menu bar
#   across the top and the Dock along the bottom. `avail* == screen*` is never a
#   number to fall back to, on a desktop that has both.
BROWSER_CHROME_HEIGHT = 87
MENU_BAR_HEIGHT = 33
DOCK_HEIGHT = 53

# What the page reports. Read on a blank page, before any site is loaded, so no
# third party ever sees the pre-override browser.
READ_JS = r"""async () => {
  const n = navigator, w = window, s = screen;
  const out = {
    ua: n.userAgent,
    platform: n.platform,
    languages: (n.languages || []).join(','),    screen: [s.width, s.height, s.availWidth, s.availHeight, s.colorDepth].join('x'),
    geom: [w.innerWidth, w.innerHeight, w.outerWidth, w.outerHeight].join('x'),
    dpr: w.devicePixelRatio,
    dark: (() => { try { return matchMedia('(prefers-color-scheme: dark)').matches; }
                   catch (e) { return null; } })(),
  };
  try {
    const brands = n.userAgentData && n.userAgentData.brands;
    out.brands = brands ? brands.map(b => b.brand + '/' + b.version).join(',') : '';
  } catch (e) { out.brands = ''; }
  // The high-entropy hints are the interesting ones: `architecture` is where a client
  // hint derived from an Intel user-agent string gives an Apple Silicon machine away.
  try {
    const high = await n.userAgentData.getHighEntropyValues(
        ['architecture', 'bitness', 'platformVersion', 'uaFullVersion', 'model']);
    // The hint set includes `platform` as "macOS", which is not what `navigator.platform`
    // says ("MacIntel"); keep both, under their own names.
    Object.assign(out, high);
    out.uaPlatform = high.platform || (n.userAgentData && n.userAgentData.platform);
    out.platform = n.platform;      // "MacIntel", which is not what the hint says
  } catch (e) { /* not a secure context, or no client hints at all */ }
  return out;
}"""


def _run(args: list[str], timeout: int = 6) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return done.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class Display:
    """The main screen, in the units a page sees.

    `avail_*` is the work area: the screen less the menu bar and the Dock. `avail_top`
    is where that work area starts in screen coordinates (33 here), which is also the
    top a real window sits at; None means the reading did not say, and `work_area`
    falls back to the menu-bar height.
    """
    width: int
    height: int
    avail_width: int
    avail_height: int
    dpr: float = 1.0
    color_depth: int = 24
    source: str = "unknown"
    avail_top: int | None = None
    avail_left: int = 0

    @property
    def approximate(self) -> bool:
        return "approx" in self.source

    def as_dict(self) -> dict:
        return {"width": self.width, "height": self.height, "avail_width": self.avail_width,
                "avail_height": self.avail_height, "avail_top": self.avail_top,
                "avail_left": self.avail_left, "dpr": self.dpr,
                "color_depth": self.color_depth, "source": self.source}


def _display_jxa() -> Display | None:
    if sys.platform != "darwin":
        return None
    try:
        raw = json.loads(_run(["osascript", "-l", "JavaScript", "-e", JXA_DISPLAY]))
    except Exception:  # noqa: BLE001
        return None
    try:
        width, height = int(round(float(raw["w"]))), int(round(float(raw["h"])))
        avail_w = int(round(float(raw.get("aw") or width)))
        avail_h = int(round(float(raw.get("ah") or height)))
        scale = float(raw.get("scale") or 1)
    except Exception:  # noqa: BLE001
        return None
    if width <= 0 or height <= 0:
        return None
    # The visible frame's origin is measured from the bottom-left in Cocoa points, so
    # the gap above it is the menu bar: 982 - (53 + 896) = 33 here. Absent or absurd,
    # it stays None and `work_area` uses the measured menu-bar height instead.
    avail_top: int | None = None
    avail_left = 0
    try:
        origin_x, origin_y = float(raw.get("ax")), float(raw.get("ay"))
        top = int(round(height - (origin_y + avail_h)))
        if 0 <= top < height:
            avail_top = top
        avail_left = max(0, int(round(origin_x)))
    except Exception:  # noqa: BLE001
        avail_top, avail_left = None, 0
    return Display(width=width, height=height, avail_width=avail_w, avail_height=avail_h,
                   dpr=scale, color_depth=30 if scale > 1 else 24, source="NSScreen",
                   avail_top=avail_top, avail_left=avail_left)


def _display_system_profiler() -> Display | None:
    """Fallback without the ObjC bridge: pixels, halved when the panel is Retina."""
    raw = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=12)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        entries = data.get("SPDisplaysDataType") or []
        for entry in entries:
            res = str(entry.get("spdisplays_resolution") or "")
            main = str(entry.get("spdisplays_main") or "")
            if not res or (main and main != "spdisplays_yes"):
                continue
            match = re.search(r"(\d+)\s*x\s*(\d+)", res)
            if not match:
                continue
            px_w, px_h = int(match.group(1)), int(match.group(2))
            retina = "retina" in res.lower()
            dpr = 2 if retina else 1
            # The work area loses the menu bar and the Dock; which sizes those are is
            # unknowable from here, so this uses the measured ones and says so.
            return Display(width=px_w // dpr, height=px_h // dpr,
                           avail_width=px_w // dpr,
                           avail_height=max(1, px_h // dpr - MENU_BAR_HEIGHT - DOCK_HEIGHT),
                           dpr=dpr, color_depth=30 if retina else 24,
                           source="system_profiler(approx)",
                           avail_top=MENU_BAR_HEIGHT)
    except Exception:  # noqa: BLE001
        return None
    return None


@functools.lru_cache(maxsize=1)
def display() -> Display | None:
    """The main screen, cached for the process. None when it cannot be read."""
    return _display_jxa() or _display_system_profiler()


@functools.lru_cache(maxsize=1)
def appearance() -> str | None:
    """'dark' or 'light' from the OS itself. None when unknown.

    A page's `prefers-color-scheme` is one of the cheapest fingerprint bits there
    is, and Playwright's default is to force `light` whatever the machine says -
    so a dark-mode user's sessions report the opposite of their own browser, in
    the same profile, on the same day. Overridable per session for anyone who
    wants it pinned.
    """
    if sys.platform != "darwin":
        return None
    out = _run(["defaults", "read", "-g", "AppleInterfaceStyle"]).strip().lower()
    return "dark" if out == "dark" else "light"


@functools.lru_cache(maxsize=1)
def os_version() -> str:
    """The macOS version, three parts, as a client hint wants it. Cached: a process each."""
    if sys.platform != "darwin":
        return ""
    out = _run(["sw_vers", "-productVersion"]).strip()
    parts = (out.split(".") + ["0", "0"])[:3]
    return ".".join(parts) if out else ""


def architecture() -> str:   # no cache: it reads a module attribute, and tests set it
    """The client-hint name for this CPU: arm, x86."""
    machine = _platform.machine().lower()
    return "arm" if machine in ("arm64", "aarch64") else "x86"


def work_area(screen: Display) -> tuple[int, int, int, int]:
    """The usable rectangle of the screen: (left, top, width, height).

    NSScreen's visible frame already is exactly this - the menu bar and the Dock are
    outside it - and `avail_top` says where it starts. A reading that could not see a
    work area (`avail* >= screen*`) is derived from the menu bar and the Dock instead
    of being passed on as-is: `screen.availHeight == screen.height` is a desktop with
    no menu bar, which is not the desktop this is running on. Both results are clamped
    into the screen, so the window can never be asked to sit off it.
    """
    width = int(screen.avail_width or 0)
    height = int(screen.avail_height or 0)
    if height <= 0 or height >= screen.height:
        height = max(1, screen.height - MENU_BAR_HEIGHT - DOCK_HEIGHT)
    if width <= 0 or width > screen.width:
        width = screen.width
    top = MENU_BAR_HEIGHT if screen.avail_top is None else int(screen.avail_top)
    left = max(0, int(screen.avail_left or 0))
    top = max(0, min(top, max(0, screen.height - height)))
    left = min(left, max(0, screen.width - width))
    return left, top, width, height


def default_window(screen: Display) -> tuple[int, int]:
    """A plausible Chrome window on this screen.

    Chrome opened 1200x765 of viewport on a 1512x982 screen (measured, non-maximised,
    fresh profile), so width is capped at 1200 and the height follows the same
    shape, then clamped so the window cannot exceed the screen's usable area.
    A guess at the *window*, never at the *screen* - the screen is measured.
    """
    width = min(1200, max(800, screen.width - 120))
    height = round(width * 0.6375)
    height = max(600, min(height, max(600, screen.avail_height - 100)))
    return int(width), int(height)


@functools.lru_cache(maxsize=4)
def _languages_at(roots: tuple) -> str:
    """Chrome's own `intl.accept_languages`, straight from the profile's Preferences.

    This is the value the real browser sends as Accept-Language and exposes as
    `navigator.languages`. latchkey used to pin `en-US`, which is one language
    where this machine's Chrome says two (`en-US,en`) - a small thing that shows up
    in the same fingerprint the "every visit is consistent" argument rests on.
    """
    for root in (roots or (CHROME_ROOT,)):
        if not root or not os.path.isdir(root):
            continue
        for name in ("Default", "Profile 1", "Profile 2", "Profile 3"):
            path = os.path.join(root, name, "Preferences")
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    data = json.load(handle)
            except Exception:  # noqa: BLE001
                continue
            value = str(data.get("intl", {}).get("accept_languages") or "").strip()
            if value:
                return value
    return "en-US,en"


def profile_languages(profile_dirs: list | None = None) -> str:
    """Chrome's Accept-Language for these profile roots. Cached: it is read per session."""
    return _languages_at(tuple(profile_dirs) if profile_dirs else (CHROME_ROOT,))


@dataclass
class Plan:
    """What the browser should report about its screen, its window and itself.

    `width`/`height` are the content area - the emulated viewport, which is what the
    page must see as `innerWidth/innerHeight`. The window holding it is
    `outer_width`/`outer_height` at `window_x`/`window_y`, and it has to fit in the
    work area `avail_width`/`avail_height`, which is what `screen.avail*` reports.
    """
    width: int = 1280
    height: int = 820
    screen_width: int | None = None
    screen_height: int | None = None
    avail_width: int | None = None
    avail_height: int | None = None
    outer_width: int | None = None
    outer_height: int | None = None
    window_x: int = 0
    window_y: int = 0
    dpr: float = 1.0
    color_depth: int = 24
    accept_language: str = "en-US,en"
    color_scheme: str | None = None
    ua: str = ""
    native: bool = False
    source: str = "defaults"
    residuals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"width": self.width, "height": self.height,
                "screen_width": self.screen_width, "screen_height": self.screen_height,
                "avail_width": self.avail_width, "avail_height": self.avail_height,
                "outer_width": self.outer_width, "outer_height": self.outer_height,
                "window_x": self.window_x, "window_y": self.window_y,
                "dpr": self.dpr, "color_depth": self.color_depth,
                "accept_language": self.accept_language, "color_scheme": self.color_scheme,
                "native": self.native, "source": self.source,
                "residuals": list(self.residuals)}

    def summary(self) -> str:
        """One line for a log or an event detail."""
        bits = [f"{self.width}x{self.height}@{self.dpr:g}x"]
        if self.screen_width and self.screen_height:
            bits.append(f"screen {self.screen_width}x{self.screen_height}")
        bits.append(self.source)
        if self.residuals:
            bits.append("residual: " + "; ".join(self.residuals))
        return " ".join(bits)


def plan(*, width: int | None = None, height: int | None = None, native: bool = True,
         accept_language: str | None = None, ua: str = "",
         color_scheme: str | None = None) -> Plan:
    """Work out the window, the screen and the identity for a session.

    `native=False` keeps the old behaviour (the requested viewport, dpr 1, no
    screen emulation) for anyone who prefers a fixed frame - the viewer and the
    coordinate maths do not care either way, because both read the page's own
    viewport size.
    """
    screen = display()
    language = accept_language or profile_languages()
    scheme = color_scheme or appearance()
    if not native or screen is None:
        return Plan(width=int(width or 1280), height=int(height or 820),
                    dpr=1.0, color_depth=24, accept_language=language,
                    color_scheme=scheme, ua=ua, native=False,
                    source="defaults",
                    residuals=[] if not native else ["no display detected"])
    want_w, want_h = default_window(screen)
    left, top, avail_w, avail_h = work_area(screen)
    inner_w, inner_h = int(width or want_w), int(height or want_h)
    # The window holds the viewport, plus the chrome above it. Width is unchanged -
    # Chrome on macOS has no side borders, so outerWidth == innerWidth - and the height
    # carries the title bar, the tab strip and the omnibox, which is why outerHeight is
    # the larger number a page expects to see.
    outer_w, outer_h = inner_w, inner_h + BROWSER_CHROME_HEIGHT
    plan_out = Plan(width=inner_w, height=inner_h,
                    screen_width=screen.width, screen_height=screen.height,
                    avail_width=avail_w, avail_height=avail_h,
                    outer_width=outer_w, outer_height=outer_h,
                    window_x=left, window_y=top,
                    dpr=float(screen.dpr), color_depth=int(screen.color_depth),
                    accept_language=language, color_scheme=scheme, ua=ua, native=True,
                    source=screen.source)
    # Say what is still not faithful instead of hiding it. The screen, its work area, the
    # colour depth and the window are all Chrome's own now (`--screen-info`, window
    # bounds), so the one thing left to say is a window that does not fit.
    if outer_h > avail_h or outer_w > avail_w:
        # Only reachable when a size was asked for by hand: the default window is
        # clamped inside the work area. The inner size still wins - it has to be the
        # emulated viewport exactly - so the window overhangs and that is said here.
        plan_out.residuals.append(
            f"window {outer_w}x{outer_h} is larger than the {avail_w}x{avail_h} work area")
    return plan_out


def screen_info_switch(plan_out: Plan) -> str:
    """The `--screen-info` launch switch describing this plan's screen to new headless.

    New headless Chrome has a virtual display, and this switch is how it is told what
    that display is: its size, its colour depth, its scale, and the work area inside it
    (`workArea*` are insets from each edge). Everything is in *physical* pixels - a
    1512x982 screen at 2x is `3024x1964 devicePixelRatio=2` - and Chrome divides by the
    scale to get what `screen.width`, `screen.availHeight` and `screen.colorDepth`
    report. All of it native, none of it emulated, and no JavaScript in the page.

    Without a native plan the switch only says the screen is at least as big as the
    window, which is the one thing a default virtual display does not guarantee.
    """
    dpr = float(plan_out.dpr or 1.0)
    if not plan_out.native or not plan_out.screen_width or not plan_out.screen_height:
        width = int(plan_out.outer_width or plan_out.width)
        height = int(plan_out.outer_height or plan_out.height + BROWSER_CHROME_HEIGHT)
        return "--screen-info={%dx%d}" % (max(width, 800), max(height, 600))
    screen_w, screen_h = int(plan_out.screen_width), int(plan_out.screen_height)
    avail_w = int(plan_out.avail_width or screen_w)
    avail_h = int(plan_out.avail_height or screen_h)
    left, top = int(plan_out.window_x), int(plan_out.window_y)
    insets = {"workAreaLeft": left, "workAreaTop": top,
              "workAreaRight": max(0, screen_w - left - avail_w),
              "workAreaBottom": max(0, screen_h - top - avail_h)}
    phys = lambda n: int(round(n * dpr))  # noqa: E731
    bits = ["0,0 %dx%d" % (phys(screen_w), phys(screen_h)),
            "colorDepth=%d" % int(plan_out.color_depth or 24),
            "devicePixelRatio=%g" % dpr]
    bits += ["%s=%d" % (name, phys(value)) for name, value in insets.items() if value]
    return "--screen-info={" + " ".join(bits) + "}"


def window_bounds(plan_out: Plan) -> dict:
    """Where the window sits and how big it is, in the screen's CSS pixels."""
    return {"left": int(plan_out.window_x), "top": int(plan_out.window_y),
            "width": int(plan_out.outer_width or plan_out.width),
            "height": int(plan_out.outer_height or plan_out.height + BROWSER_CHROME_HEIGHT)}


def apply_window_bounds(cdp, plan_out: Plan) -> bool:
    """Give this target's window the plan's bounds, through the browser, not the page.

    `outerWidth/outerHeight` and `screenX/screenY` are read from the window itself,
    and in new headless a window is a real thing with bounds. Setting them is the
    difference between a page that reports a window and a page that reports a getter
    somebody wrote. The viewport follows from the window - new headless takes the
    same 87px of browser chrome off the height that a macOS window has - so nothing
    emulates the inner size either, and `innerHeight !== outerHeight` is simply true.
    """
    try:
        window = cdp.send("Browser.getWindowForTarget")
        cdp.send("Browser.setWindowBounds",
                 {"windowId": window["windowId"], "bounds": window_bounds(plan_out)})
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_ua(page, cdp, plan_out: Plan) -> dict:
    """Check the UA the page actually has; correct it if a HeadlessChrome token survived.

    The launch switch is preferred over Playwright's `user_agent=` option because
    Playwright *derives* the client-hint metadata from the string it is handed -
    hand it an "Intel Mac OS X" user agent and it will tell the page this Apple
    Silicon machine is x86. The launch switch changes the string and leaves
    Chrome's own metadata alone. This is the belt to that braces: read the page
    and only intervene if the token is still there.
    """
    seen = ""
    try:
        seen = str(page.evaluate("() => navigator.userAgent") or "")
    except Exception:  # noqa: BLE001
        return {"seen": "", "patched": False, "headless": False}
    headless = "HeadlessChrome" in seen
    if not headless:
        return {"seen": seen, "patched": False, "headless": False, "matches": seen == plan_out.ua}
    want = plan_out.ua or _clean_ua(seen)
    try:
        cdp.send("Emulation.setUserAgentOverride",
                 {"userAgent": want, "acceptLanguage": plan_out.accept_language,
                  "platform": "MacIntel"})
        return {"seen": seen, "patched": True, "headless": True, "matches": False}
    except Exception:  # noqa: BLE001
        return {"seen": seen, "patched": False, "headless": True, "matches": False}


def _clean_ua(seen: str) -> str:
    version = chrome_version() or ""
    return re.sub(r"HeadlessChrome/[\d.]+",
                  f"Chrome/{version}" if version else "Chrome", seen)


# Read in a secure context. Chrome refuses `getHighEntropyValues` anywhere else, which
# is why we serve ourselves a page on 127.0.0.1 rather than asking a third party.
IDENTITY_JS = r"""async () => {
  const d = navigator.userAgentData;
  if (!d) return {};
  const out = {ua: navigator.userAgent, brands: d.brands, mobile: !!d.mobile,
               platform: d.platform};
  const high = await d.getHighEntropyValues(
      ['architecture', 'bitness', 'model', 'platformVersion', 'uaFullVersion',
       'fullVersionList', 'wow64']);
  Object.assign(out, high);
  return out;
}"""


class LocalPage:
    """A one-page HTTP server on 127.0.0.1, and the reason it exists.

    `getHighEntropyValues` is available only in a secure context, and `about:blank` is not
    one - so the metadata a session needs to describe itself has to be read somewhere
    trustworthy. `http://127.0.0.1` counts as one, it is our own machine, and it leaves no
    trace on the tab: the page is loaded in a frame, which does not enter the history.
    """

    BODY = b"<!doctype html><html><head><title>blank</title></head><body></body></html>"

    def __init__(self) -> None:
        import http.server
        import socketserver
        import threading

        body = self.BODY

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        self._server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self._server.timeout = 2
        self.port = int(self._server.server_address[1])
        # The handler must not go through a proxy, and must not be reused: one request.
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def close(self) -> None:
        try:
            self._server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass


def measure(page, timeout_ms: int = 2500) -> dict:
    """The browser's own client-hint metadata, measured rather than invented.

    Two attempts, because a secure context is the whole difficulty: a frame first (it does
    not touch the tab's history), then the page itself (which does, but works where a
    frame inside an opaque about:blank does not qualify). The page handed in is a
    throwaway in either case - the session measures on a page it then closes.

    Returns {} if nothing lines up, in which case the session keeps Playwright's derived
    hints: everything right except `architecture`, which says x86 on an Apple Silicon
    machine, and the harness reports that as a residual rather than hiding it.
    """
    local = LocalPage()
    try:
        value = _measure_in_frame(page, local, timeout_ms)
        if not value:
            value = _measure_at_top(page, local, timeout_ms)
        return value
    finally:
        local.close()


def _looks_measured(value: object) -> bool:
    return isinstance(value, dict) and bool(value.get("brands"))


def _measure_in_frame(page, local: LocalPage, timeout_ms: int) -> dict:
    try:
        page.set_content(f"<iframe src='{local.url}' style='width:1px;height:1px'></iframe>")
        waited = 0
        while waited <= timeout_ms:
            for frame in page.frames:
                if frame.url.startswith(local.url):
                    value = frame.evaluate(IDENTITY_JS)
                    return value if _looks_measured(value) else {}
            page.wait_for_timeout(100)
            waited += 100
    except Exception:  # noqa: BLE001
        return {}
    return {}


def _measure_at_top(page, local: LocalPage, timeout_ms: int) -> dict:
    try:
        page.goto(local.url, wait_until="domcontentloaded", timeout=timeout_ms)
        value = page.evaluate(IDENTITY_JS)
        return value if _looks_measured(value) else {}
    except Exception:  # noqa: BLE001
        return {}


def user_agent_metadata(measured: dict, plan_out: Plan) -> dict | None:
    """CDP's userAgentMetadata, built from what the browser reported about itself.

    Two fields cannot be taken from the measurement as-is, and both were checked against a
    Chrome that Playwright had never touched (a copy launched by hand and read over CDP):

    * `architecture` comes from this machine, because Playwright has already answered for
      it - an "Intel Mac OS X" user agent is what makes it say x86 on Apple Silicon;
    * `platformVersion` comes from the OS, because the measurement inherits the same
      contamination: the genuine value is the one `sw_vers` prints (26.6.2 here, which is
      exactly what an untouched Chrome reported), while anything measured under an
      overriding user agent says the frozen `10_15_7`.

    Everything else - the brand list and the full version list - is the browser's own, and
    matched the untouched Chrome exactly, so nothing here is guessed and nothing drifts when
    Chrome's GREASE brand changes.

    One more since the user agent became a launch switch: `--user-agent` makes Chrome blank
    every high-entropy hint (the full version list, `uaFullVersion`, `bitness` come back
    empty), so a measurement taken under it carries the brands and nothing else. The full
    versions are then rebuilt from the brands and the binary's own version, which is what
    an untouched Chrome reports for them: Chrome's brands at its full version, the GREASE
    brand at `N.0.0.0`.
    """
    if not measured.get("brands"):
        return None
    platform = str(measured.get("platform") or "")
    if not platform or platform == "Unknown":
        platform = "macOS" if sys.platform == "darwin" else platform
    platform_version = os_version() or str(measured.get("platformVersion") or "")
    brands = measured.get("brands") or []
    full_version = str(measured.get("uaFullVersion") or "") or str(chrome_version() or "")
    full_list = measured.get("fullVersionList") or full_version_list(brands, full_version)
    return {"brands": brands,
            "fullVersionList": full_list,
            "fullVersion": full_version,
            "platform": platform,
            "platformVersion": platform_version,
            "architecture": architecture(),
            "model": str(measured.get("model") or ""),
            "mobile": bool(measured.get("mobile")),
            "bitness": str(measured.get("bitness") or "64"),
            "wow64": bool(measured.get("wow64"))}


def full_version_list(brands: list, full_version: str) -> list:
    """The full-version brand list an untouched Chrome reports, from its brands.

    A real brand (Google Chrome, Chromium) carries the binary's full version; the GREASE
    brand ("Not_A Brand" and its spellings) carries its major at `.0.0.0`, which is what
    Chrome itself puts there.
    """
    out = []
    for entry in brands or []:
        brand = str(entry.get("brand") or "")
        major = str(entry.get("version") or "")
        grease = "brand" in brand.lower()          # "Not_A Brand", "Not/A)Brand", ...
        version = full_version if full_version and not grease else f"{major}.0.0.0"
        out.append({"brand": brand, "version": version})
    return out


def set_user_agent(cdp, plan_out: Plan, metadata: dict | None = None) -> bool:
    """Point this target at the corrected user agent, hints included.

    Every page needs this, not just the first one: the emulation is per target, and a
    second tab that reports HeadlessChrome (or the hints Playwright derived) would be the
    one page in the session that gives it away.
    """
    params: dict = {"userAgent": plan_out.ua, "acceptLanguage": plan_out.accept_language,
                    "platform": "MacIntel"}
    if metadata:
        params["userAgentMetadata"] = metadata
    try:
        cdp.send("Emulation.setUserAgentOverride", params)
        return True
    except Exception:  # noqa: BLE001
        return False


# The client-hint measurement is the same answer on every open: the brand list and the
# full version list belong to the Chrome *binary*, not the session, and the two machine
# fields folded in afterwards (architecture, platformVersion) belong to the machine. So it
# is worth ~500ms to learn nothing changed - a local HTTP server, an iframe and a poll -
# and that cost is paid on every session start. Cache it, keyed by what can actually move it.
_HINTS_SCHEMA = 3   # bump to invalidate every cached entry after a shape change here


def _hints_cache_off() -> bool:
    return (os.environ.get("LATCHKEY_HINTS_CACHE") or "").strip().lower() in ("0", "off", "no")


def _hints_cache_file() -> str:
    from . import paths
    return paths.path("client-hints.json")


def _hints_key() -> str:
    """What the measurement depends on: this Chrome's version and this platform.

    A Chrome update changes the brand and full-version lists (and the UA), which is exactly
    when the cached answer must be thrown away - so the version string is the key, and the
    schema tag lets a change to what we store here invalidate old entries for free.
    """
    return f"{_HINTS_SCHEMA}|{sys.platform}|{chrome_version() or '?'}"


def _load_cached_hints(key: str) -> dict | None:
    if _hints_cache_off():
        return None
    try:
        with open(_hints_cache_file(), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:  # noqa: BLE001 - a missing or corrupt cache is a miss, not an error
        return None
    entry = data.get(key) if isinstance(data, dict) else None
    return entry if _looks_measured(entry) else None


def _store_cached_hints(key: str, value: dict) -> None:
    """Record a measurement, so the next open reads it instead of re-measuring.

    Only a real measurement is stored (`brands` present); an empty result is the signal to
    keep Playwright's derived hints, and caching *that* would pin the residual forever. The
    write is atomic and never raises: a cache that cannot be written just means the next
    open measures again, which is exactly today's behaviour.
    """
    if _hints_cache_off() or not _looks_measured(value):
        return
    path_ = _hints_cache_file()
    try:
        os.makedirs(os.path.dirname(path_), exist_ok=True)
        data: dict = {}
        try:
            with open(path_, encoding="utf-8") as handle:
                loaded = json.load(handle)
                data = loaded if isinstance(loaded, dict) else {}
        except Exception:  # noqa: BLE001
            data = {}
        data[key] = value
        tmp = f"{path_}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path_)
    except Exception:  # noqa: BLE001
        pass


def measured_hints(page) -> dict:
    """Chrome's own client-hint metadata, measured once per Chrome version and cached.

    The live probe (`measure`) is correct but costs ~500ms every open to confirm a value
    that only a Chrome update can change. This is the caching wrapper the session actually
    calls: a hit skips the probe (and the local server and iframe it needs) entirely; a
    miss measures and records the answer for next time. `LATCHKEY_HINTS_CACHE=0` forces the
    live probe on every open, which is the old behaviour and the way to re-measure by hand.
    """
    key = _hints_key()
    cached = _load_cached_hints(key)
    if cached is not None:
        return cached
    measured = measure(page)
    _store_cached_hints(key, measured)
    return measured


def apply_identity(page, cdp, plan_out: Plan, measure_page=None) -> dict:
    """Make the page agree with the machine: screen, scale, user agent, client hints.

    The screen itself (`--screen-info`) and the user agent string (`--user-agent`) are
    launch switches, in force before the first page exists. Two things are per target
    and are arranged here, for the first page and again for every page opened later:

    * the **window**, through its bounds (`apply_window_bounds`) - `outer*`, `screenX/Y`
      and, from those, the viewport - a real window in new headless, nothing patched;
    * the **client-hint metadata**, because `--user-agent` blanks the high-entropy
      hints and Playwright, handed a user agent, would derive them from the string
      (an "Intel Mac OS X" string makes this Apple Silicon machine say `x86`). They are
      read from the browser itself through a secure local page (see `measure`), with
      `architecture` and `platformVersion` taken from the machine and the full versions
      from the binary.

    The metadata has to be supplied in full when overriding the user agent: measured on
    this machine, a CDP override without it clears the hints entirely - brands, platform
    and architecture all come back empty, which is a louder signal than the one it was
    meant to fix.
    """
    done: dict = {"window": False, "ua": False, "measured": False, "metadata": False}
    done["window"] = apply_window_bounds(cdp, plan_out)
    measured = measured_hints(measure_page or page)
    done["measured"] = bool(measured)
    metadata = user_agent_metadata(measured, plan_out)
    if metadata:
        done["metadata"] = True
        done["architecture"] = metadata.get("architecture")
        done["userAgentMetadata"] = metadata
    done["ua"] = set_user_agent(cdp, plan_out, metadata)
    return done


def clean_ua(seen: str) -> str:
    """Chrome's own UA string with only the headless token swapped for the real one.

    Chrome builds the string itself, so version, platform and engine stay exactly
    right; the only edit is the token that says "nobody is looking at this".
    """
    return _clean_ua(seen)


def read(page) -> dict:
    """The signals a fingerprinting script reads, in one round trip."""
    try:
        value = page.evaluate(READ_JS)
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def expected(plan_out: Plan) -> dict:
    """What the same signals should say if the session is faithful to this machine."""
    screen = display()
    out: dict = {"languages": plan_out.accept_language,
                 "dpr": plan_out.dpr}
    if screen is not None:
        out["screen"] = f"{screen.width}x{screen.height}"
    if plan_out.ua:
        out["ua_has_headless"] = False
    return out
