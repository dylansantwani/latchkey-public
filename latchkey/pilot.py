"""``latchkey_pilot`` - hand a UI errand to the Jev decision model.

Instead of an agent looping snapshot -> act -> snapshot, the pilot runs that loop
itself with a fast, cheap decision model (Jev, see :mod:`latchkey.jev`): it reads
the page, offers Jev the controls that are actually on it, takes the one action
Jev picks, waits for the page to settle, and repeats until the goal is reached,
it is stuck, or a step needs the calling agent.

Guarantees the loop keeps so the caller stays in control:

* It never types text of its own - only the values the caller supplies in ``type``.
* Irreversible controls (submit, buy, send, delete, sign out ...) are never even
  offered to Jev; when the only way forward is one of those, the pilot hands back.
* It stays on the starting host unless ``offsite=True``; a click that wanders off
  is walked back.
* Below ``min_confidence`` it hands back rather than guess.

A hand-back is a question, not a failure: it names the obstacle and the best
candidates, and the caller acts on that one step then calls again with no ``url``
to resume on the same tab.

This mirrors openbrowser's ``browser_pilot`` (same Jev wire format, same stop
ladder) but drives latchkey's own page primitives directly on structured
snapshot nodes rather than re-parsing snapshot text.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from . import a11y, jev

DEFAULT_STEPS = 8
MAX_STEPS = 20
VISIBLE_TEXT_CAP = 2000
# Stop and hand back before the MCP client's ~30s call ceiling rather than let the
# whole call time out with nothing to show.
DEFAULT_DEADLINE_S = 26.0

# Never offered to Jev - the caller owns anything that commits.
IRREVERSIBLE = re.compile(
    r"\b(submit|buy|purchase|pay|order|send|delete|remove|post|publish|sign ?out|log ?out|"
    r"confirm|checkout|subscribe|donate|accept|agree|save|upload|create account|request|"
    r"share|invite|apply|register|enroll|unenroll|bid|offer|add to cart)\b", re.I)

CLICKABLE = frozenset({
    "button", "link", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
    "checkbox", "radio", "switch", "option", "treeitem", "combobox", "listbox",
    "disclosure", "spinbutton", "slider", "searchbox", "textbox"})
FIELDS = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
BACK = jev.BACK


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def _label(node: dict) -> str:
    role = node.get("role") or "text"
    if role == "heading" and node.get("level"):
        role = f"heading{node['level']}"
    name = (node.get("name") or "").strip()
    bits = [f'{role} "{name}"' if name else role]
    if node.get("value"):
        bits.append(f'value="{node["value"]}"')
    elif node.get("placeholder"):
        bits.append(f'placeholder="{node["placeholder"]}"')
    for flag in ("checked", "disabled", "required"):
        if node.get(flag):
            bits.append(flag)
    if "expanded" in node:
        bits.append("expanded" if node["expanded"] else "collapsed")
    return " ".join(bits)


def _headings(nodes: list[dict]) -> list[str]:
    return [n["name"] for n in nodes
            if n.get("role") == "heading" and n.get("name")][:12]


def _selected(nodes: list[dict]) -> list[str]:
    out = []
    for n in nodes:
        if n.get("checked") or n.get("expanded") is True:
            label = (n.get("name") or "").strip()
            if label:
                out.append(f'{n.get("role")}: {label}')
    return out[:12]


def _roster(nodes, *, avoid, tried, type_pool, use_limit=3):
    """The options offered to Jev this step: clickable controls (minus irreversible
    and over-used ones), a typed value for each field, and Go back.

    Returns ``(items, actions)`` where ``items`` is ``[{ref, label}]`` for Jev and
    ``actions`` maps each offered ref to a concrete action tuple.
    """
    items, actions = [], {}
    avoid_low = [a.lower() for a in avoid]
    fields = [n for n in nodes if n.get("ref") and n.get("role") in FIELDS and not n.get("disabled")]

    # Typed values first: they are usually the point of the errand.
    for vi, val in enumerate(type_pool):
        if val["used"]:
            continue
        targets = _match_fields(val, fields)
        for node in targets:
            ref = f"type:{vi}:{node['ref']}"
            label = f'type "{val["text"]}" into {_label(node)}'
            items.append({"ref": ref, "label": label})
            actions[ref] = ("type", vi, node["ref"])

    for node in nodes:
        ref = node.get("ref")
        if not ref or node.get("role") not in CLICKABLE or node.get("disabled"):
            continue
        label = _label(node)
        low = label.lower()
        if IRREVERSIBLE.search(low) or any(a in low for a in avoid_low):
            continue
        if tried.get(ref, 0) >= use_limit:
            continue
        items.append({"ref": ref, "label": label})
        actions[ref] = ("click", ref)

    if tried.get(BACK, 0) < use_limit:
        items.append({"ref": BACK, "label": "Go back to the previous page"})
        actions[BACK] = ("back",)
    return items, actions


def _match_fields(val, fields):
    """Which fields a supplied value may be typed into: the one whose name matches
    its hint, else every text field (a short list on most pages)."""
    hint = (val.get("hint") or "").lower().strip()
    if hint:
        matched = [f for f in fields
                   if hint in ((f.get("name") or "") + " " + (f.get("placeholder") or "")).lower()]
        if matched:
            return matched[:4]
    return fields[:6]


def _key_options(keys, tried, use_limit=3):
    items, actions = [], {}
    for desc, combo in (keys or {}).items():
        ref = f"key:{combo}"
        if tried.get(ref, 0) >= use_limit:
            continue
        items.append({"ref": ref, "label": f"press {combo} ({desc})"})
        actions[ref] = ("key", combo)
    return items, actions


def _until_met(state, until, typed_all: bool) -> bool:
    if not until:
        return False
    checks = []
    if until.get("url"):
        checks.append(str(until["url"]).lower() in (state.url or "").lower())
    if until.get("title"):
        checks.append(str(until["title"]).lower() in (getattr(state, "title", "") or "").lower())
    if until.get("text"):
        checks.append(str(until["text"]).lower() in (getattr(state, "text", "") or "").lower())
    if until.get("typed"):
        checks.append(typed_all)
    return bool(checks) and all(checks)


def _candidates(pick, actions, limit=5):
    """Top ranked refs from Jev's probability map, for a hand-back message."""
    probs = (pick or {}).get("probabilities") or {}
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    out = []
    for ref, p in ranked:
        if ref in (jev.HANDBACK, "none") or ref not in actions:
            continue
        out.append(f"{ref} (p={p:.2f})")
        if len(out) >= limit:
            break
    return out


def _signature(state, nodes) -> tuple:
    return (state.url, getattr(state, "title", ""), len(nodes))


def run_errand(browser, goal, *, jev_ask, usage, url=None, type_values=(), keys=None,
               until=None, max_steps=DEFAULT_STEPS, avoid=(), offsite=False,
               min_confidence=jev.DEFAULT_MIN_CONFIDENCE, page=True,
               deadline_s=DEFAULT_DEADLINE_S):
    """Run one errand to ``goal``. ``jev_ask`` is a :func:`latchkey.jev.asker`; the
    browser is a live latchkey :class:`~latchkey.session.Browser`. Returns a dict
    the tool layer renders."""
    max_steps = max(1, min(int(max_steps or DEFAULT_STEPS), MAX_STEPS))
    deadline = time.monotonic() + float(deadline_s)
    if url:
        browser.goto(url)
    start = browser.state()
    start_host = _host(start.url)
    type_pool = [({"text": v} if isinstance(v, str) else dict(v)) | {"used": False}
                 for v in (type_values or [])][:8]
    trace, history, tried = [], [], {}
    no_progress = 0
    last_sig = None

    outcome = "budget"
    detail = {}
    for step in range(1, max_steps + 1):
        if time.monotonic() >= deadline:
            outcome = "timeout"
            detail = {"reason": "judgment", "candidates": []}
            break
        state = browser.state()
        data = a11y.nodes(browser)
        a11y.remember(browser, data)
        nodes = data["nodes"]

        typed_all = all(v["used"] for v in type_pool) if type_pool else False
        if _until_met(state, until, typed_all):
            outcome = "reached"
            break

        items, actions = _roster(nodes, avoid=avoid, tried=tried, type_pool=type_pool)
        kitems, kactions = _key_options(keys, tried)
        items += kitems
        actions.update(kactions)

        visible = a11y.read(browser, mode="text")
        if isinstance(visible, dict):
            visible = visible.get("text", "")
        header = f"{getattr(state, 'title', '') or ''} — {start.url if step == 1 else state.url}"
        action_state = {"task": goal, "page": header, "history": history[-6:],
                        "visible_text": str(visible)[:VISIBLE_TEXT_CAP]}
        if type_pool and not typed_all:
            action_state["text_still_to_type"] = [v["text"] for v in type_pool if not v["used"]]
        goal_state = {"task": goal,
                      "current_page": {"url": state.url, "title": getattr(state, "title", ""),
                                       "headings": _headings(nodes), "selected": _selected(nodes)},
                      "actions_taken_on_this_page": history[-6:]}

        # Arrival is asked over the lean state, preferentially by the cloud when a
        # second opinion is configured; the action decision runs on the primary.
        arrival_ask = getattr(jev_ask, "second", None) or jev_ask
        goal_p = jev.prob((arrival_ask(goal_state, jev.GOAL_QUESTION)).get("goal_done"))
        decision = jev.decide(jev_ask, action_state, items,
                              chunk_size=getattr(jev_ask, "second_chunk", 240))
        pick = decision.get("action") or {}
        chosen = pick.get("choice")
        conf = float(pick.get("confidence") or 0)

        if goal_p >= jev.GOAL_THRESHOLD:
            outcome = "arrived"
            break

        stuck = decision.get("stuck", 0) >= jev.STUCK_THRESHOLD
        if (chosen in (None, jev.HANDBACK, "none")) or conf < min_confidence:
            if goal_p >= jev.GOAL_PROBABLE:
                outcome = "probably"
                break
            outcome = "handback"
            detail = {"reason": decision.get("reason") or ("not_here" if not items else "judgment"),
                      "candidates": _candidates(pick, actions)}
            break
        if stuck and no_progress >= 1:
            outcome = "stuck"
            break

        act = actions.get(chosen)
        if not act:
            outcome = "handback"
            detail = {"reason": "judgment", "candidates": _candidates(pick, actions)}
            break

        tried[chosen] = tried.get(chosen, 0) + 1
        did = _execute(browser, act, type_pool, nodes)
        history.append(did)
        trace.append(f"{step}. {did}")

        after = browser.state()
        # Walk back off-host wanderings unless allowed.
        if not offsite and _host(after.url) and _host(after.url) != start_host:
            browser.back()
            avoid = tuple(avoid) + (did,)
            history.append(f"(went off-site to {_host(after.url)}; came back)")
            after = browser.state()

        sig = _signature(after, a11y.nodes(browser)["nodes"])
        no_progress = no_progress + 1 if sig == last_sig else 0
        last_sig = sig
    else:
        outcome = "budget"

    return _report(browser, goal, outcome, detail, trace, usage, page)


def _execute(browser, act, type_pool, nodes) -> str:
    kind = act[0]
    if kind == "click":
        ref = act[1]
        label = next((_label(n) for n in nodes if n.get("ref") == ref), ref)
        browser.click(browser.refs.selector(ref))
        return f"clicked {label}"
    if kind == "type":
        _, vi, ref = act
        value = type_pool[vi]["text"]
        type_pool[vi]["used"] = True
        label = next((_label(n) for n in nodes if n.get("ref") == ref), ref)
        browser.fill(browser.refs.selector(ref), value)
        return f'typed "{value}" into {label}'
    if kind == "key":
        combo = act[1]
        browser.page.keyboard.press(_normalize_combo(combo))
        return f"pressed {combo}"
    if kind == "back":
        browser.back()
        return "went back"
    return "did nothing"


def _normalize_combo(combo: str) -> str:
    import sys
    mod = "Meta" if sys.platform == "darwin" else "Control"
    return re.sub(r"\bMod\b", mod, combo, flags=re.I)


def _report(browser, goal, outcome, detail, trace, usage, page) -> dict:
    state = browser.state()
    handed_back = outcome in ("handback", "stuck", "budget", "timeout")
    heads = {
        "arrived": "pilot: arrived at the goal",
        "reached": "pilot: reached the stop condition",
        "probably": "pilot: probably at the goal - verify the page yourself",
        "handback": "pilot: handed back - a step is yours",
        "stuck": "pilot: stuck - not making progress",
        "budget": "pilot: out of steps",
        "timeout": "pilot: paused near the time limit - call again with no url to resume",
    }
    lines = [heads.get(outcome, f"pilot: {outcome}")]
    lines += trace or ["(no actions taken)"]
    lines.append(jev.describe_usage(usage) + f" · ~${jev.cost_usd(usage):.4f}")
    if handed_back and detail.get("reason"):
        why = jev.REASONS.get(detail["reason"], detail["reason"])
        lines.append(f"reason: {why}")
        if detail.get("candidates"):
            lines.append("best candidates: " + ", ".join(detail["candidates"]))
        lines.append("Act on that one step, then call latchkey_pilot again with no url to resume.")
    elif outcome == "probably":
        lines.append("Confirm the page shows the result before relying on it.")
    lines.append(f"page: {getattr(state, 'title', '') or ''} — {state.url}")
    if page:
        try:
            outline = a11y.read(browser, mode="outline")
            lines.append(outline.get("text", "") if isinstance(outline, dict) else str(outline))
        except Exception:
            pass
    return {"outcome": outcome, "ok": not handed_back, "handedBack": handed_back,
            "goal": goal, "url": state.url, "usage": usage, "text": "\n".join(lines)}
