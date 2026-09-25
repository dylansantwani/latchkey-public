"""The difference between driving a page and *being* in it.

A person arriving at a form moves a pointer across it in an arc, hovers over the
field, clicks a few pixels off its centre, types, and stops to think. Playwright
does none of that: `locator.click()` teleports the pointer to the element's exact
centre and clicks in the same instant, `fill()` sets a value with no key events at
all, and `mouse.wheel()` moves the page in one jump. Those are the behavioural
tells every bot-detection vendor publishes, and until now latchkey produced all
three - the drawn cursor moved, but the *real* pointer never did, so the page saw
teleports while the viewer saw a glide.

This module is the arithmetic: where a pointer should go, how long to wait, how
much text to type at a time. The Playwright calls are one line each in the driver,
so the interesting part stays testable without a browser.

Everything is seeded off a `random.Random`, so a session can be reproduced exactly
by handing it a seed - which is what makes a flaky *interaction* distinguishable
from a flaky site.
"""
from __future__ import annotations

import math
import os
import random

# Off for one session with LATCHKEY_HUMANIZE=0, or `humanize=False` in the spec.
ENV = "LATCHKEY_HUMANIZE"

# Once typed text passes this, per-character delays shrink to fit the budget: a
# 60-character URL typed at a human 90 ms/char is six seconds of nothing, and an
# agent that is slower than it needs to be is its own kind of dead giveaway when
# the same client comes back every twenty minutes.
TYPE_BUDGET_MS = 1400
MIN_DELAY_MS, MAX_DELAY_MS = 28, 150


def enabled(explicit: bool | None = None) -> bool:
    """Human-shaped input on by default; off only when asked."""
    if explicit is not None:
        return bool(explicit)
    raw = os.environ.get(ENV, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def rand(seed: int | None = None) -> random.Random:
    return random.Random(seed)


def waypoints(x0: float, y0: float, x1: float, y1: float, rng: random.Random,
              count: int | None = None) -> list[tuple[float, float]]:
    """A bowed, jittered path from one point to another.

    A cubic Bézier whose control points are pushed sideways off the straight line,
    sampled a handful of times and nudged by a pixel or two. The *last* point is
    the target exactly - a pointer that lands 3 px off is a miss, and this
    function exists to make a click look approached, not to make it miss.

    A hand on a mouse also overshoots and corrects; that is left out on purpose.
    Overshoot is only convincing when the correction is timed like a person, and
    a wrong-time overshoot on a submit button is a mis-click.
    """
    distance = math.hypot(x1 - x0, y1 - y0)
    if distance < 2:
        return [(x1, y1)]
    steps = count or max(3, min(14, int(distance / 55) + 3))
    # Perpendicular bow: bigger for longer moves, always at least a couple of px.
    bow = max(2.0, min(28.0, distance * 0.09)) * rng.choice((-1, 1))
    nx, ny = -(y1 - y0) / distance, (x1 - x0) / distance
    c1 = (x0 + (x1 - x0) * 0.28 + nx * bow, y0 + (y1 - y0) * 0.28 + ny * bow)
    c2 = (x0 + (x1 - x0) * 0.72 + nx * bow * 0.6, y0 + (y1 - y0) * 0.72 + ny * bow * 0.6)
    points: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out: slow off the mark, quicker in the middle, slow to land.
        u = t * t * (3 - 2 * t)
        inv = 1 - u
        px = (inv ** 3) * x0 + 3 * (inv ** 2) * u * c1[0] + 3 * inv * (u ** 2) * c2[0] + (u ** 3) * x1
        py = (inv ** 3) * y0 + 3 * (inv ** 2) * u * c1[1] + 3 * inv * (u ** 2) * c2[1] + (u ** 3) * y1
        if i < steps:
            # A wandering hand, damped as it settles onto the target.
            wobble = 1.0 + 0.35 * (1 - t)
            px += rng.uniform(-1, 1) * 1.6 * wobble
            py += rng.uniform(-1, 1) * 1.6 * wobble
        points.append((px, py))
    points[-1] = (x1, y1)
    return points


def click_point(box: dict, rng: random.Random, inset: float = 0.18) -> tuple[float, float]:
    """A point inside an element that is not its exact centre.

    Playwright clicks the centre because it is the point most likely to hit. A
    person aims at the label. Landing dead centre of every single element is
    itself the signal - so stay inside, at least two pixels from every edge, and
    never twice in the same spot.
    """
    x, y = float(box.get("x") or 0), float(box.get("y") or 0)
    w, h = float(box.get("width") or 0), float(box.get("height") or 0)
    if w <= 4 or h <= 4:
        return x + w / 2, y + h / 2
    ix = min(w * inset, max(0.0, (w - 4) / 2))
    iy = min(h * inset, max(0.0, (h - 4) / 2))
    px = x + w / 2 + rng.uniform(-ix, ix)
    py = y + h / 2 + rng.uniform(-iy, iy)
    return (min(max(px, x + 2), x + w - 2), min(max(py, y + 2), y + h - 2))


def dwell_ms(rng: random.Random) -> int:
    """The pause between arriving at a control and committing to it."""
    return rng.randint(55, 240)


def step_pause_ms(rng: random.Random) -> int:
    """The gap between two legs of a pointer's journey: about one frame, plus nerve."""
    return rng.randint(8, 26)


def type_delays(text: str, rng: random.Random, budget_ms: int = TYPE_BUDGET_MS) -> list[int]:
    """Per-character delays, sized from a total budget.

    Real typing is bursty - a flurry of keys, a pause, another flurry - so each
    delay is drawn from a wide spread rather than being one constant, and a rare
    longer pause is added on top. Constant inter-key timing is one of the few
    behavioural things a keystroke logger can measure with almost no data (a real
    typist's variance is enormous), which is exactly why a fixed `delay=50` reads as
    machine-made. The budget decides the *base* delay, not the total: a long string
    should not take six seconds to type, but it should still sound like a person.
    """
    if not text:
        return []
    n = len(text)
    base = min(max(int(budget_ms / n), MIN_DELAY_MS), MAX_DELAY_MS)
    delays = []
    for _ in range(n):
        d = base * rng.uniform(0.55, 1.5)
        if rng.random() < 0.06:          # the occasional think
            d += rng.randint(120, 420)
        delays.append(int(max(12, d)))
    return delays


def type_timing(text: str, rng: random.Random,
                budget_ms: int = TYPE_BUDGET_MS) -> tuple[int, list[int]]:
    """`(nominal delay, delays)` - one number for Playwright plus the shape to wait on."""
    delays = type_delays(text, rng, budget_ms)
    nominal = int(sum(delays) / len(delays)) if delays else MIN_DELAY_MS
    return nominal, delays


def scroll_chunks(pixels: int, rng: random.Random,
                  size: tuple[int, int] = (90, 240)) -> list[int]:
    """Break one big scroll into wheel-sized pieces that add up to it exactly."""
    if pixels == 0:
        return []
    direction = 1 if pixels > 0 else -1
    left = abs(int(pixels))
    chunks: list[int] = []
    while left > 0:
        take = min(left, rng.randint(*size))
        chunks.append(take * direction)
        left -= take
    return chunks


def wheel_pause_ms(rng: random.Random) -> int:
    """Between wheel notches: humans scroll, read, scroll again."""
    return rng.randint(35, 150)
