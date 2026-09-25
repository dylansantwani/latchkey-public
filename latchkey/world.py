"""Running page JavaScript from a world of latchkey's own.

Playwright's `page.evaluate` runs in the page's *main* world, through a wrapper it calls
`UtilityScript`. Both halves are visible to the page. A site that wraps
`document.getElementsByClassName` sees every call an automation makes through it; a
getter that reads `new Error().stack` sees `UtilityScript.evaluate` in the frames above
it. rebrowser's bot-detector names them `mainWorldExecution` and `sourceUrlLeak`, and
latchkey lit both: every state probe, login read and accessibility snapshot went that way.

An isolated world is what an extension's content script runs in: the same DOM, its own
JavaScript globals and prototypes. Nothing the page redefined applies there, and nothing
run there appears in the page's stack traces. Chrome makes one on request
(`Page.createIsolatedWorld`) and runs a function in it (`Runtime.callFunctionOn`), with
no wrapper of anyone's between the two. That is the whole of this module: the same
signature as `page.evaluate(js, arg)`, in a world the page cannot see.

What stays in the main world, on purpose: init scripts (they exist to change what the
page sees), and anything that must read a main-world global.

Execution contexts die with a navigation, so a context id is a cache, not a fact:
a call that finds its context gone makes a new one and tries once more.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError

WORLD_NAME = "latchkey"

# The same test Playwright applies to decide whether a string is a function to call or an
# expression to evaluate. An expression is wrapped so both spellings keep working.
_FUNCTION_RE = re.compile(r"^\s*(async\s+)?(function\b|\(|[A-Za-z_$][\w$]*\s*=>)")

# Chrome's own words for "that context is gone", every spelling seen from a navigation, a
# closed frame or a crashed renderer between two calls.
_CONTEXT_GONE = ("Cannot find context with specified id", "Execution context was destroyed",
                 "Inspected target navigated or closed", "Cannot find default execution context",
                 "No frame for given id found", "Target closed", "Session closed")

MISSING = object()
_MISSING = MISSING


class EvaluateError(PlaywrightError):
    """The page's own exception, re-raised here. A subclass of Playwright's error so
    callers that already catch that keep catching this."""


def as_function(js: str) -> str:
    """`js` if it already is a function, else an arrow function returning it."""
    return js if _FUNCTION_RE.match(js or "") else "() => (" + str(js) + "\n)"


def _context_gone(exc: BaseException) -> bool:
    text = str(exc)
    return any(word in text for word in _CONTEXT_GONE)


class World:
    """One isolated world per frame, made on demand, in which JavaScript is run.

    `cdp_for(page)` hands back the CDP session for a page (the driver's own cache, so a
    page never gets two). `frames_of(page)` lists that page's Playwright frames, for
    matching a Playwright `Frame` to a CDP frame id.
    """

    def __init__(self, cdp_for: Callable[[Any], Any]) -> None:
        self._cdp_for = cdp_for
        self._contexts: dict[tuple[int, str], int] = {}

    # -- frames ------------------------------------------------------------

    @staticmethod
    def _walk(tree: dict) -> list[dict]:
        out = [tree.get("frame") or {}]
        for child in tree.get("childFrames") or []:
            out.extend(World._walk(child))
        return out

    def frame_id(self, page: Any, frame: Any | None = None) -> str | None:
        """The CDP id of a Playwright frame on this page; the main frame by default.

        A frame is matched by its url and name, which is all a Playwright frame says
        about itself. An out-of-process iframe (a cross-origin frame with site isolation
        on) belongs to another target and is not in this page's tree, and for that the
        answer is None - the caller falls back to Playwright's own evaluate for it.
        """
        cdp = self._cdp_for(page)
        tree = cdp.send("Page.getFrameTree")["frameTree"]
        if frame is None or frame == getattr(page, "main_frame", None):
            return tree.get("frame", {}).get("id")
        url = str(getattr(frame, "url", "") or "")
        name = str(getattr(frame, "name", "") or "")
        candidates = [f for f in self._walk(tree)[1:]
                      if str(f.get("url") or "") == url and str(f.get("name") or "") == name]
        if len(candidates) != 1:
            candidates = [f for f in self._walk(tree)[1:] if str(f.get("url") or "") == url]
        return candidates[0].get("id") if len(candidates) == 1 else None

    # -- contexts ----------------------------------------------------------

    def context(self, page: Any, frame_id: str, *, fresh: bool = False) -> int:
        key = (id(page), frame_id)
        if not fresh and key in self._contexts:
            return self._contexts[key]
        cdp = self._cdp_for(page)
        made = cdp.send("Page.createIsolatedWorld",
                        {"frameId": frame_id, "worldName": WORLD_NAME,
                         "grantUniveralAccess": True})
        context_id = int(made["executionContextId"])
        self._contexts[key] = context_id
        return context_id

    def forget(self, page: Any) -> None:
        """Drop every context of a page that is gone."""
        for key in [k for k in self._contexts if k[0] == id(page)]:
            self._contexts.pop(key, None)

    # -- evaluate ----------------------------------------------------------

    def evaluate(self, page: Any, js: str, arg: Any = _MISSING, *,
                 frame: Any | None = None) -> Any:
        """Run `js` - a function, or an expression - in this page's isolated world.

        `arg`, when given, is passed as the function's one argument, as `page.evaluate`
        does. The result comes back by value (JSON); a DOM node or anything else that
        cannot be serialised comes back as None, as it would from Playwright. The page's
        own exception is raised as `EvaluateError`.
        """
        frame_id = self.frame_id(page, frame)
        if frame_id is None:
            # An out-of-process frame: Playwright reaches it through its own session for
            # that target. Its main world, with the leak that implies, for this one frame.
            scope = frame if frame is not None else page
            return scope.evaluate(js) if arg is _MISSING else scope.evaluate(js, arg)
        cdp = self._cdp_for(page)
        params: dict = {"functionDeclaration": as_function(js), "returnByValue": True,
                        "awaitPromise": True, "userGesture": False}
        if arg is not _MISSING:
            params["arguments"] = [{"value": arg}]
        for attempt in (0, 1):
            params["executionContextId"] = self.context(page, frame_id, fresh=bool(attempt))
            try:
                result = cdp.send("Runtime.callFunctionOn", params)
            except Exception as exc:  # noqa: BLE001
                if attempt == 0 and _context_gone(exc):
                    continue
                raise
            details = result.get("exceptionDetails")
            if details:
                raise EvaluateError(_describe(details))
            return result.get("result", {}).get("value")
        return None


def _describe(details: dict) -> str:
    exc = details.get("exception") or {}
    text = exc.get("description") or details.get("text") or "evaluate failed"
    return str(text).splitlines()[0][:400] if "\n" in str(text) else str(text)[:400]
