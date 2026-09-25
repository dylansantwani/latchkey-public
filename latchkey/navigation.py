"""Going places: goto, history, reload, waiting for a selector.

Every verb here that lands on a new page also forgets the cached jar count: a cloned
profile's cookies are only knowable by asking Chrome, so they are asked for once per
navigation and cached until the next one (`Driver.host_cookies`).
"""
from __future__ import annotations

from .detect import PageState


class NavigationMixin:
    """Navigation verbs. Mixed into `Driver`; every verb publishes an event."""

    def goto(self, url: str, settle_ms: int = 2500,
             text_limit: int = 4000) -> PageState:
        # Every route to a new page comes through here - the tool, an action list, a new
        # tab, the login handoff - which is why the guard is here and not at the tool.
        url = self.guard_navigation(url)
        # A new document is a clean slate for settling: drop any count left over from the
        # page we are leaving, so `settle` waits on this navigation's requests, not theirs.
        self.reset_network()
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.settle(settle_ms)
        self._touch()
        self.forget_jar()
        state = self.state(text_limit)
        self._publish("goto", settle_ms=settle_ms, **state.summary())
        return state

    def back(self, settle_ms: int = 1000) -> PageState:
        return self._history("back", self.page.go_back, settle_ms)

    def forward(self, settle_ms: int = 1000) -> PageState:
        return self._history("forward", self.page.go_forward, settle_ms)

    def reload(self, settle_ms: int = 1500) -> PageState:
        return self._history("reload", self.page.reload, settle_ms)

    def _history(self, kind: str, move, settle_ms: int) -> PageState:
        self.reset_network()
        move(wait_until="domcontentloaded", timeout=30_000)
        self.settle(settle_ms)
        self._touch()
        self.forget_jar()
        state = self.state()
        self._publish(kind, settle_ms=settle_ms, **state.summary())
        return state

    def wait_for(self, selector: str, timeout_ms: int = 15_000,
                 frame: int | None = None) -> PageState:
        self.locator_in(selector, frame).wait_for(timeout=timeout_ms)
        self._touch()
        state = self.state()
        self._publish("wait_for", selector=selector, timeout_ms=timeout_ms, frame=frame,
                      **state.summary())
        return state

    def links(self, limit: int = 50) -> list[dict]:
        return self.run_js("""(limit) => [...document.querySelectorAll('a[href]')]
            .slice(0, limit)
            .map(a => ({text: (a.innerText || '').trim().slice(0, 80), href: a.href}))""",
            limit)
