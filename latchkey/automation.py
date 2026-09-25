"""The verbs an agent drives the page with, plus the action-list layer.

Every verb publishes one event and returns the resulting `PageState`. Values
typed into a field that looks like a credential are never put in the event: the
events feed a viewer and, later, a live stream, and a password does not belong on
either.

The verbs that send something check the session's write policy first (`policy.py`),
and the action-list layer below turns a failure into an error that names the action it
came from, because an agent cannot see the list it sent once the call has returned.
"""
from __future__ import annotations

import re

from . import human
from .detect import PageState
from .policy import ReadOnlyError


# How long an action waits for its element to be actionable. It was 15s: long enough
# that one blocked click ate most of a model's patience, and it bought nothing once the
# failure says why (why_it_timed_out). Pages that are slow to render something get a
# latchkey_wait for it, which says what it is waiting for.
ACTION_TIMEOUT_MS = 8_000


class AutomationMixin:
    """Click, type, scroll, upload, screenshot. Mixed into `Driver`."""

    def click(self, selector: str, settle_ms: int = 1200,
              force: bool = False, frame: int | None = None) -> PageState:
        """Click the first match.

        `force=True` skips Playwright's actionability checks. Needed where an
        overlay swallows the click: Canvas opens a reactour welcome tour whose
        backdrop made every course card unclickable, and the resulting
        TimeoutError is indistinguishable from a missing element.
        """
        self.guard_write("click")
        detail = self._approach(selector, click=True, frame=frame)
        locator = self.locator_in(selector, frame)
        # A real click at the point the hand chose, so the page sees a mousedown where the
        # pointer actually travelled to instead of a jump to the element's centre.
        # `force=True` keeps the locator path: it exists for elements something else is
        # covering, and a raw click would land on the cover. A framed target keeps it too:
        # the page mouse clicks main-frame coordinates, so an element inside a cross-origin
        # iframe has to be reached through its frame-aware locator, not a coordinate on top.
        if detail and self.humanize and not force and frame is None:
            self.page.wait_for_timeout(human.dwell_ms(self.human_rand))
            try:
                self.page.mouse.click(detail["x"], detail["y"])
            except Exception:  # noqa: BLE001
                locator.click(timeout=ACTION_TIMEOUT_MS, force=force)
        else:
            locator.click(timeout=ACTION_TIMEOUT_MS, force=force)
        self.settle(settle_ms)
        self._touch()
        state = self.state()
        self._publish("click", selector=selector, force=force, frame=frame,
                      settle_ms=settle_ms,
                      **detail, **state.summary())
        return state

    def click_at(self, x: float, y: float, settle_ms: int = 1200) -> PageState:
        """Click viewport coordinates when the page exposes no usable locator."""
        self.guard_write("click_at")
        x, y = float(x), float(y)
        self._move_pointer(x, y, click=True)
        self.page.mouse.click(x, y)
        self.settle(settle_ms)
        self._touch()
        state = self.state()
        self._publish("click_at", x=x, y=y, settle_ms=settle_ms,
                      **self.viewport, **state.summary())
        return state

    def hover(self, selector: str, settle_ms: int = 500,
              force: bool = False, frame: int | None = None) -> PageState:
        """Hover an element.

        Like click, this takes `force`: sites keep fixed elements - a loading bar, a
        consent banner, a tour backdrop - that cover the page and swallow pointer
        events, and GitHub is one of them. The failure is a 15s timeout that looks
        exactly like the element not existing.
        """
        detail = self._approach(selector, frame=frame)
        # The walk above is the hover on most pages - CSS reacts to the real pointer - and
        # this is the last few pixels onto the element for the ones that need an enter
        # event on the element itself.
        self.locator_in(selector, frame).hover(timeout=ACTION_TIMEOUT_MS, force=force)
        self.settle(settle_ms)
        self._touch()
        state = self.state()
        self._publish("hover", selector=selector, force=force, frame=frame, **detail,
                      **state.summary())
        return state

    def fill(self, selector: str, value: str, settle_ms: int = 300,
             frame: int | None = None) -> PageState:
        self.guard_write("fill")
        detail = self._approach(selector, frame=frame)
        locator = self.locator_in(selector, frame)
        # Type it, or fall back to fill(): a keystroke counter sees a field that filled
        # itself as exactly that, but masked inputs and autocomplete widgets disagree
        # about what real key events mean, so `_type_like` reads the value back and hands
        # anything unexpected to `fill()`.
        typed = bool(self.humanize and detail and self._type_like(locator, value, point=detail))
        if not typed:
            locator.fill(value, timeout=ACTION_TIMEOUT_MS)
        self.settle(settle_ms)
        self._touch()
        state = self.state()
        self._publish("fill", selector=selector, frame=frame,
                      value="(hidden)" if self.secret_target(selector) else value,
                      **detail, **state.summary())
        return state

    def press(self, selector: str, key: str = "Enter",
              settle_ms: int = 1200, frame: int | None = None) -> PageState:
        self.guard_write("press")
        detail = self._approach(selector, frame=frame)
        self.locator_in(selector, frame).press(key, timeout=ACTION_TIMEOUT_MS)
        self.settle(settle_ms)
        self._touch()
        state = self.state()
        self._publish("press", selector=selector, key=key, frame=frame,
                      **detail, **state.summary())
        return state

    def select(self, selector: str, value: str, settle_ms: int = 500,
               frame: int | None = None) -> PageState:
        self.guard_write("select")
        detail = self._approach(selector, frame=frame)
        self.locator_in(selector, frame).select_option(value, timeout=ACTION_TIMEOUT_MS)
        self.settle(settle_ms)
        self._touch()
        state = self.state()
        self._publish("select", selector=selector, value=value, frame=frame, **detail,
                      **state.summary())
        return state

    def check(self, selector: str, state_on: bool = True,
              frame: int | None = None) -> PageState:
        self.guard_write("check")
        detail = self._approach(selector, frame=frame)
        self.locator_in(selector, frame).set_checked(state_on, timeout=ACTION_TIMEOUT_MS)
        self._touch()
        page_state = self.state()
        self._publish("check", selector=selector, checked=state_on, frame=frame, **detail,
                      **page_state.summary())
        return page_state

    def scroll(self, pixels: int = 800, selector: str | None = None,
               frame: int | None = None) -> PageState:
        detail = self._approach(selector, frame=frame) if selector else self._approach()
        if selector:
            self.locator_in(selector, frame).scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
        else:
            for chunk in (human.scroll_chunks(pixels, self.human_rand)
                          if self.humanize else [pixels]):
                self.page.mouse.wheel(0, chunk)
                if self.humanize:
                    self.page.wait_for_timeout(human.wheel_pause_ms(self.human_rand))
        self.settle(300)
        self._touch()
        state = self.state()
        self._publish("scroll", selector=selector, pixels=pixels, frame=frame, **detail,
                      **state.summary())
        return state

    def upload(self, selector: str, path: str, frame: int | None = None) -> PageState:
        self.guard_write("upload")
        detail = self._approach(selector, frame=frame)
        self.locator_in(selector, frame).set_input_files(path, timeout=20_000)
        self._touch()
        state = self.state()
        self._publish("upload", selector=selector, path=path, frame=frame, **detail,
                      **state.summary())
        return state

    def screenshot(self, path: str, full_page: bool = False,
                   selector: str | None = None, frame: int | None = None) -> str:
        """Screenshot the page, or one element of it.

        Screenshotting an element is how you avoid a host page's overlay: Canvas
        runs a welcome tour whose backdrop covers the whole viewport, so any
        page-sized shot of embedded content is taken through it.
        """
        if frame is not None and not selector:
            raise ValueError("a frame screenshot needs selector; screenshot the iframe element "
                             "from the main page for the whole embedded surface")
        target = self.locator_in(selector, frame) if selector else self.page
        target.screenshot(path=path, full_page=full_page and not selector,
                          timeout=20_000)
        self._publish("screenshot", path=path, full_page=full_page, selector=selector,
                      frame=frame)
        return path

    def run(self, url: str | None, actions: list[dict],
            settle_ms: int = 2500) -> PageState:
        """Navigate (if a url is given) then apply a list of actions."""
        state = self.goto(url, settle_ms) if url else self.state()
        apply_actions(self, actions)
        return self.state()


GENERIC_FIX = ("if a click or hover on an element you can see timed out, pass force:true - an "
               "overlay is swallowing the pointer event. Screenshot the page, or read it with "
               "latchkey_text, before retrying, so the retry is not a guess.")


def why_it_timed_out(message: str) -> tuple[str, str]:
    """The reason Playwright gave up, read out of its call log, and what to do about it.

    Playwright's timeout carries a call log - "waiting for element to be visible",
    "<div class=modal> intercepts pointer events" - and that log is the whole diagnosis.
    Only its first line ("Timeout 15000ms exceeded.") used to reach the agent, so every
    timeout read the same and got the same guess: force:true, which is right for an overlay
    and wrong for a hidden or disabled control. In Lattice's week of 2026-09-12 each of
    those cost a 15-second wait and a blind retry. Returns ("", "") when the log says
    nothing more specific.
    """
    log = [line.strip(" -") for line in message.splitlines() if line.strip()]
    covered = [line for line in log if "intercepts pointer events" in line]
    if covered:
        cover = covered[-1].split(" intercepts pointer events")[0].split(" from ")[0]
        return (f"{cover[:140]} is covering it",
                "close or dismiss what covers it (a dialog, a cookie banner, a tour), or pass "
                "force:true to click through it if it is only decoration.")
    text = " ".join(log).lower()
    if "element is not visible" in text or "element is outside of the viewport" in text:
        return ("it is on the page but hidden",
                "open whatever holds it first (a menu, a tab, a collapsed section), or scroll "
                "to it; a fresh latchkey_snapshot shows what is visible now.")
    if "element is not enabled" in text or "element is disabled" in text:
        return ("it is disabled", "something before it is unfinished - a required field, an "
                                  "unticked box, a choice not yet made. Do that first.")
    if "element is not stable" in text:
        return ("it kept moving (an animation)", "wait for the page to settle, then retry.")
    if "waiting for locator" in text and "locator resolved to" not in text:
        return ("nothing on the page matches it",
                "the page changed or the selector is wrong: take a fresh latchkey_snapshot "
                "and use a ref from it.")
    return "", ""


class ActionFailed(RuntimeError):
    """An action list stopped part-way, and says which action stopped it.

    An action list is the natural unit for an agent - sign in, then open the assignment -
    and the failure used to be a bare `TimeoutError` from inside one of them: no index, no
    verb, and no way to tell whether the steps before it had run. The traceback pointed at
    `apply_actions`, which is not where the agent was working. This carries that instead,
    plus the hint the docs give a human, because the agent is the one who has to retry.
    """

    def __init__(self, index: int, total: int, action: dict, url: str,
                 cause: BaseException) -> None:
        verb = action.get("do")
        target = action.get("selector") or action.get("url") or ""
        detail = (str(cause).strip().splitlines() or [""])[0][:200]
        why, fix = why_it_timed_out(str(cause))
        super().__init__(
            f"action {index} of {total} ({verb} {target}) failed: "
            f"{type(cause).__name__}: {detail}\n"
            + (f"  why: {why}\n" if why else "")
            + f"  the page was at {url or 'unknown'}; actions 1..{index - 1} did run.\n"
            + f"  {fix or GENERIC_FIX}")
        self.index = index
        self.verb = verb
        self.target = target
        self.cause = cause


# One runner per verb, so a failure can be reported against the verb that raised it.
def _target(browser, action: dict) -> str:
    """The selector an action points at: a snapshot `ref`, or a plain `selector`.

    Refs are how an agent holds on to something it has *read* - "the button that said Sign
    in" - without inventing CSS for it, and a ref is checked before it is used: if the page
    has moved on, the failure says which element it used to be rather than clicking whatever
    replaced it.
    """
    ref = clean_ref(action.get("ref"))
    if ref:
        if action.get("frame") is not None:
            raise ValueError("frame actions take a selector, not a main-page snapshot ref; "
                             "use the frame index from latchkey_frames with selector")
        return browser.ref_selector(ref)
    return action["selector"]


def _frame_kwargs(action: dict) -> dict:
    """Pass frame only when present, preserving simple Browser-compatible stubs."""
    return {"frame": action["frame"]} if action.get("frame") is not None else {}


def clean_ref(ref) -> str:
    """A ref as a model copies it out of a snapshot: `[ref=e7]`, `ref=e7`, `e7 `."""
    text = str(ref or "").strip().strip("[]").strip()
    if text.lower().startswith("ref="):
        text = text[4:].strip()
    return text.strip("\"'")


def _run_click(browser, a):
    browser.click(_target(browser, a), a.get("settle_ms", 1200), a.get("force", False),
                  **_frame_kwargs(a))


def _run_click_at(browser, a):
    if a.get("x") is None or a.get("y") is None:
        raise ValueError("click_at needs viewport coordinates x and y")
    browser.click_at(a["x"], a["y"], a.get("settle_ms", 1200))


def _run_hover(browser, a):
    browser.hover(_target(browser, a), a.get("settle_ms", 500), a.get("force", False),
                  **_frame_kwargs(a))


def _run_fill(browser, a):
    browser.fill(_target(browser, a), a.get("value", ""), **_frame_kwargs(a))


def _run_press(browser, a):
    browser.press(_target(browser, a), a.get("key", "Enter"), **_frame_kwargs(a))


def _run_select(browser, a):
    browser.select(_target(browser, a), a.get("value", ""), **_frame_kwargs(a))


def _run_check(browser, a):
    browser.check(_target(browser, a), a.get("state", True), **_frame_kwargs(a))


def _run_scroll(browser, a):
    browser.scroll(a.get("pixels", 800), (_target(browser, a) if (a.get("ref") or a.get("selector")) else None),
                   **_frame_kwargs(a))


def _run_upload(browser, a):
    browser.upload(_target(browser, a), a["path"], **_frame_kwargs(a))


def _run_goto(browser, a):
    browser.goto(a["url"], a.get("settle_ms", 2500))


def _run_back(browser, a):
    browser.back()


def _run_forward(browser, a):
    browser.forward()


def _run_reload(browser, a):
    browser.reload()


def _run_wait(browser, a):
    browser.page.wait_for_timeout(a.get("ms", 1000))


def _run_wait_for(browser, a):
    browser.wait_for(_target(browser, a), a.get("timeout_ms", 15_000), **_frame_kwargs(a))


def _run_screenshot(browser, a):
    browser.screenshot(a["path"], a.get("full_page", False), (_target(browser, a) if (a.get("ref") or a.get("selector")) else None),
                       **_frame_kwargs(a))


def _run_eval(browser, a):
    js = a.get("js")
    if not isinstance(js, str) or not js.strip():
        # a bare KeyError: 'js' told a model nothing about what was missing
        raise ValueError("eval needs the JavaScript to run: {\"do\": \"eval\", \"js\": \"document.title\"} "
                         "(script, code and expression are read as js too)")
    browser.evaluate(js, **_frame_kwargs(a))


def _run_sync(browser, a):
    browser.refresh()


def _run_save_session(browser, a):
    browser.save_session(a.get("site"))


def _run_new_page(browser, a):
    browser.new_page(a.get("url"))


def _run_switch(browser, a):
    browser.switch(a.get("target", a.get("tab", a.get("index"))))


def _run_close_page(browser, a):
    browser.close_page(a.get("target", a.get("tab", a.get("index"))))


RUNNERS = {
    "click": _run_click, "click_at": _run_click_at, "hover": _run_hover,
    "fill": _run_fill, "press": _run_press,
    "select": _run_select, "check": _run_check, "scroll": _run_scroll,
    "upload": _run_upload, "goto": _run_goto, "back": _run_back,
    "forward": _run_forward, "reload": _run_reload, "wait": _run_wait,
    "wait_for": _run_wait_for, "screenshot": _run_screenshot, "eval": _run_eval,
    "sync": _run_sync, "save_session": _run_save_session, "new_page": _run_new_page,
    "switch": _run_switch, "close_page": _run_close_page,
}


# -- reading an action list the way it actually arrives -----------------------
#
# `{do, selector, value}` is the shape; models reach for `{action, css, text}` and
# `{type: "click", target: "..."}` and mean the same thing every time. A synonym is a
# model that knew what it wanted, so these are read rather than refused. What is *not*
# guessed at is an unknown verb: the error names the nearest one and lists the rest.
VERB_KEYS = ("do", "action", "verb", "op", "type", "command")
SELECTOR_KEYS = ("selector", "sel", "css", "query", "element", "target", "locator")
VALUE_KEYS = ("value", "text", "input", "content")
URL_KEYS = ("url", "href", "link", "to")
VERB_ALIASES = {
    "type": "fill", "input": "fill", "enter": "fill", "set": "fill", "set_value": "fill",
    "write": "fill", "navigate": "goto", "open": "goto", "visit": "goto", "go": "goto",
    "keypress": "press", "key": "press", "sendkeys": "press", "tap": "click",
    "choose": "select", "pick": "select", "toggle": "check", "set_checked": "check",
    "sleep": "wait", "pause": "wait", "waitfor": "wait_for", "wait_until": "wait_for",
    "shot": "screenshot", "capture": "screenshot", "evaluate": "eval", "js": "eval",
    "script": "eval", "refresh": "reload", "new_tab": "new_page", "open_tab": "new_page",
    "switch_tab": "switch", "select_tab": "switch", "close_tab": "close_page",
    "upload_file": "upload", "scroll_by": "scroll", "save": "save_session",
    "coordinate_click": "click_at", "click_xy": "click_at",
}
TAB_VERBS = frozenset({"switch", "close_page"})
# The one argument each verb is about, for the form `{"click": "e7"}` - the verb as the key.
PRIMARY_ARG = {"goto": "url", "new_page": "url", "press": "key", "wait": "ms", "eval": "js",
               "screenshot": "path", "switch": "target", "close_page": "target",
               "save_session": "site", "scroll": "pixels"}
TEXT_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "textarea"})
# An element's own `type`, which a model copies out of a page next to a selector. It is an
# attribute, not a verb, and not a mistake worth refusing the action over.
INPUT_TYPES = frozenset({"text", "email", "password", "search", "tel", "url", "number",
                         "checkbox", "radio", "submit", "button", "date", "file", "hidden"})
TOGGLE_ROLES = frozenset({"checkbox", "radio", "switch", "menuitemcheckbox"})
REF_SHAPE = re.compile(r"^\[?(?:ref=)?e\d+\]?$", re.IGNORECASE)


def _looks_like_target(value: str) -> bool:
    """A ref (`e7`) or something CSS-shaped (`#q`, `.btn`, `input[name=q]`), not a word."""
    text = value.strip()
    return bool(REF_SHAPE.match(text) or text[:1] in "#.[" or re.search(r"[\[#.>=:]", text))


def verb_of(text) -> str:
    """A verb name as the runners know it, from any spelling a model uses; "" if none."""
    word = str(text or "").strip().lower().replace("-", "_").replace(" ", "_")
    word = VERB_ALIASES.get(word, word)
    return word if word in RUNNERS else ""


def infer_verb(action: dict, refs=None) -> str:
    """The verb an action with no verb in it can only have meant, or "".

    "unknown action None" is what a small model got back for `{"ref": "e4", "value": "x"}`
    and `{"url": "..."}`: the verb was left out because the fields already said it. They
    still do - a url alone is a navigation, a target with a value is typing - and a ref from
    a snapshot even says what kind of element it is, which settles "is this text to type or
    a label to click".
    """
    keys = {key for key, value in action.items() if value is not None}
    target = bool(keys & ({"ref"} | set(SELECTOR_KEYS)))
    role = ""
    if refs is not None and action.get("ref"):
        role = str((refs.known(clean_ref(action.get("ref"))) or {}).get("role") or "")
    if not target:
        if keys >= {"x", "y"}:
            return "click_at"
        if keys & set(URL_KEYS):
            return "goto"
        if keys & {"key", "keys"}:
            return "press"
        if keys & {"js", "script", "expression", "code"}:
            return "eval"
        if keys & {"pixels", "dy", "delta", "direction", "amount"}:
            return "scroll"
        if keys & {"ms", "seconds", "duration"}:
            return "wait"
        if "path" in keys:
            return "screenshot"
        return ""
    if keys & {"key", "keys"}:
        return "press"
    if "path" in keys:
        return "upload"
    if keys & {"checked", "state"} or role in TOGGLE_ROLES:
        return "check" if keys & {"checked", "state"} else "click"
    if keys & set(VALUE_KEYS) and role not in ("", *TEXT_ROLES) and role != "listbox":
        return "click"            # the label of a button, not text to type into it
    if keys & set(VALUE_KEYS):
        return "select" if role == "listbox" else "fill"
    return "click"


def normalise_action(raw, index: int, total: int, refs=None) -> dict:
    """One action, in the shape the runners expect, whatever shape it came in."""
    if isinstance(raw, str):
        raw = {"do": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"action {index} of {total} is {type(raw).__name__}, not an "
                         f"object like {{'do': 'click', 'ref': 'e7'}}")
    action = dict(raw)
    verb = ""
    for key in VERB_KEYS:
        value = action.get(key)
        if isinstance(value, str) and verb_of(value):
            verb = verb_of(action.pop(key))
            break
    if not verb:
        # The verb as the key: {"click": "e7"}, {"goto": "https://..."}, {"type": "#q", "text": ..}
        argument_names = set(VALUE_KEYS) | set(SELECTOR_KEYS) | set(URL_KEYS)
        has_target = any(action.get(key) for key in ("ref",) + SELECTOR_KEYS)
        for key in list(action):
            named = verb_of(key)
            value = action.get(key)
            if (not named or key in argument_names or isinstance(value, (dict, list))
                    or (key in VERB_KEYS and not value)):
                continue
            if key in VERB_KEYS and not (isinstance(value, str) and not has_target
                                         and _looks_like_target(value)):
                # `type` is also an attribute an input has: {"selector": "#e", "type": "email"}
                # describes a field, and reading it as "fill" would type nothing into it and
                # wipe it. Only `{"type": "#q", "text": ...}` - a target and no other - is a verb.
                continue
            action.pop(key)
            verb = named
            if value is True or value is None:
                break
            primary = PRIMARY_ARG.get(named)
            if primary and primary not in action:
                action[primary] = value
            elif not primary and isinstance(value, str):
                if has_target:
                    if named in ("fill", "select") and "value" not in action:
                        action["value"] = value       # {"selector": "#s", "select": "Two"}
                else:
                    action["ref" if REF_SHAPE.match(value.strip()) else "selector"] = value
            break
    if not verb:
        leftover = next((str(action.get(key)) for key in VERB_KEYS
                         if isinstance(action.get(key), str)
                         and not (key == "type" and str(action.get(key)).lower() in INPUT_TYPES)),
                        "")
        if leftover:
            import difflib
            close = difflib.get_close_matches(leftover.lower(), list(RUNNERS), n=2, cutoff=0.5)
            hint = (" Did you mean " + " or ".join(close) + "?") if close else ""
            raise ValueError(
                f"action {index} of {total}: unknown verb {leftover!r}.{hint} Use one of: "
                f"{', '.join(sorted(RUNNERS))} - e.g. {{\"do\": \"click\", \"ref\": \"e7\"}}.")
        verb = infer_verb(action, refs)
    if not verb:
        raise ValueError(
            f"action {index} of {total} has no verb: add \"do\", one of "
            f"{', '.join(sorted(RUNNERS))} - e.g. {{\"do\": \"click\", \"ref\": \"e7\"}} "
            f"or {{\"do\": \"fill\", \"ref\": \"e4\", \"value\": \"...\"}}.")
    action["do"] = verb
    # `target` means a tab to switch/close and an element everywhere else, so the
    # renaming is per verb rather than global - the one synonym that is genuinely two
    # different arguments.
    selector_keys = tuple(key for key in SELECTOR_KEYS
                          if not (key == "target" and verb in TAB_VERBS))
    for keys, wanted in ((selector_keys, "selector"), (VALUE_KEYS, "value"),
                         (URL_KEYS, "url"), (("keys",), "key"),
                         (("script", "expression", "code"), "js")):
        if wanted in action:
            continue
        for key in keys:
            if key in action and action[key] is not None:
                action[wanted] = action.pop(key)
                break
    if "ref" in action:
        action["ref"] = clean_ref(action["ref"])
    if verb == "check" and "state" not in action and "checked" in action:
        action["state"] = bool(action.pop("checked"))
    if verb == "scroll" and isinstance(action.get("pixels"), str):
        word = action["pixels"].strip().lower()
        action["pixels"] = (-800 if word in ("up", "top") else
                            int(word) if word.lstrip("-").isdigit() else 800)
    if verb == "wait" and "ms" not in action and "seconds" in action:
        try:
            action["ms"] = int(float(action.pop("seconds")) * 1000)
        except (TypeError, ValueError):
            pass
    return action


def normalise_actions(actions, refs=None) -> list[dict]:
    """An action list, read from whatever a caller sent: a list, one action, or JSON."""
    if isinstance(actions, str):
        import json
        try:
            actions = json.loads(actions)
        except json.JSONDecodeError:
            actions = [actions]        # a bare verb name, e.g. "reload"
    if isinstance(actions, dict):
        actions = [actions]            # one action where a list was asked for
    if actions is None:
        raise ValueError("actions is required: a list like "
                         "[{'do': 'click', 'ref': 'e7'}]")
    if not isinstance(actions, (list, tuple)):
        raise ValueError(f"actions is {type(actions).__name__}, not a list of actions")
    total = len(actions)
    return [normalise_action(raw, index, total, refs)
            for index, raw in enumerate(actions, 1)]


def apply_actions(browser, actions) -> None:
    """Run an action list. Shared by Browser.run and the MCP `latchkey_act` tool.

    One action at a time, stopping at the first failure - and saying which one it was,
    because by then the agent cannot see the list it sent.
    """
    actions = normalise_actions(actions, getattr(browser, "refs", None))
    total = len(actions)
    for index, action in enumerate(actions, 1):
        kind = action["do"]
        try:
            RUNNERS[kind](browser, action)
        except ReadOnlyError:
            # A policy refusal is not a broken action: it already says exactly what it is
            # and what to do about it, and dressing it as a failure would hide that.
            raise
        except Exception as exc:
            raise ActionFailed(index, total, action, _where(browser), exc) from exc


def _where(browser) -> str:
    """Where the page was when an action failed, without costing a round trip."""
    try:
        return browser.page.url
    except Exception:  # noqa: BLE001
        return ""
