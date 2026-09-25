"""Tabs inside one session.

A session drives one tab at a time: the one `switch()` or `new_page()` selected.
`state()` always reads that tab and says so in its `tab` field, so "which tab is
this?" is never a guess.

Tabs are addressed by a stable id (`t1`, `t2`, ...) or, for convenience, by index.
Indices are positional and shift when a tab closes, so an agent that is going to
hold on to a handle across calls should use the id.
"""
from __future__ import annotations

from typing import Any

from .detect import PageState


class TabsMixin:
    """Open, list, switch and close tabs. Mixed into `Driver`."""

    def pages(self) -> list[dict]:
        """Every tab in this session, with its stable id and its current index."""
        pages = list(self._ctx.pages)
        return [{"id": self.tab_id(page), "index": i, "url": page.url,
                 "title": page.title(), "active": page is self._page}
                for i, page in enumerate(pages)]

    def new_page(self, url: str | None = None, settle_ms: int = 3000) -> PageState:
        """Open a tab and make it the one this session drives."""
        self._page = self._ctx.new_page()
        self._touch()
        self.forget_jar()
        if self.show_cursor:
            self._inject_cursor()
        if url:
            return self.goto(url, settle_ms)
        state = self.state()
        self._publish("tab", action="new", **state.summary())
        return state

    def switch(self, target: Any, settle_ms: int = 800) -> PageState:
        """Drive a different tab: pass its id ('t2') or its index (1)."""
        self._page = self.page_for(target)
        self.page.wait_for_timeout(settle_ms)
        self._touch()
        self.forget_jar()
        if self.show_cursor:
            self._inject_cursor()
        state = self.state()
        self._publish("tab", action="switch", target=target, **state.summary())
        return state

    def close_page(self, target: Any | None = None) -> dict:
        """Close a tab by id or index (default: the last one)."""
        page = self.page_for(target) if target is not None else self._ctx.pages[-1]
        closed = self.tab_id(page)
        was_active = page is self._page
        page.close()
        self._tabs.pop(id(page), None)
        self._forget_cdp(page)
        if was_active:
            self._page = self._ctx.pages[-1] if self._ctx.pages else self._ctx.new_page()
        self._touch()
        self.forget_jar()
        if self.show_cursor:
            self._inject_cursor()
        result = {"closed": closed, "pages": len(self._ctx.pages),
                  "active": self.page.url, "tab": self.tab_id()}
        self._publish("tab", action="close", **result)
        return result
