"""Reading inside iframes.

`state()`, `links()` and `evaluate()` only ever see the main frame, so anything
embedded - a Google Slides deck inside Canvas, a payment form, a map - is
invisible to them. That blind spot is why a search for "slide" can find nothing
on a page that is showing slides: Canvas's APES agenda is a Google Doc in an
iframe, and the main frame's whole text was Canvas's navigation, contributing
zero characters of the document on screen.
"""
from __future__ import annotations


class FramesMixin:
    """Frame listing and per-frame reads. Mixed into `Driver`."""

    def frame_at(self, index: int):
        """One frame by the index returned from :meth:`frames`.

        Playwright frame handles are origin-independent: selecting an element through
        one works for a cross-origin iframe without evaluating through the parent page.
        """
        frames = self.page.frames
        if isinstance(index, bool):
            raise TypeError(f"frame index must be an integer, not {index!r}")
        try:
            wanted = int(index)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"frame index must be an integer, not {index!r}") from exc
        if not 0 <= wanted < len(frames):
            raise IndexError(f"no frame {wanted}; page has {len(frames)}")
        return frames[wanted]

    def locator_in(self, selector: str, frame: int | None = None):
        """The first matching locator in the main page or a named frame."""
        scope = self.page if frame is None else self.frame_at(frame)
        return scope.locator(selector).first

    def frames(self) -> list[dict]:
        """Every frame on the page, iframes included, with its index."""
        out = []
        for i, frame in enumerate(self.page.frames):
            try:
                title = frame.title()
            except Exception:  # noqa: BLE001
                title = ""
            out.append({"index": i, "url": frame.url, "name": frame.name,
                        "title": title, "is_main": frame == self.page.main_frame})
        return out

    def frame_eval(self, js: str, index: int) -> object:
        """Evaluate caller-requested JS inside one frame's main world.

        This is the frame counterpart to :meth:`Driver.evaluate`; internal reads use
        ``run_js`` and therefore stay in latchkey's isolated world.
        """
        return self.frame_at(index).evaluate(js)

    def frame_text(self, index: int, limit: int = 4000) -> str:
        """Visible text inside one frame."""
        body = self.frame_eval(
            "() => document.body ? document.body.innerText : ''", index) or ""
        return body[:limit].strip()

    def frame_links(self, index: int, limit: int = 50) -> list[dict]:
        js = ("() => [...document.querySelectorAll('a[href]')].slice(0, " + str(int(limit)) +
              ").map(a => ({text: (a.innerText || '').trim().slice(0, 80), href: a.href}))")
        return self.frame_eval(js, index)
