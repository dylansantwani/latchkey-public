"""The decision model behind ``latchkey_pilot``.

Jev is a fast *decision* model, not a chat model: it is handed a state and a set
of typed questions (choose one of these refs; is the task done, true/false) and
answers each with a pick and a confidence, never free text. The wire format is
the ``/v1/systemone`` shape openbrowser's pilot speaks, so latchkey reuses the
same self-hosted server and the same key files - one endpoint lights the pilot
up in every client.

Transport resolution (env first, then a key file, so one machine-wide key works):

* ``LATCHKEY_JEV_BASE_URL`` / ``JEV_BASE_URL`` - a self-hosted server that speaks
  ``/v1/systemone``. When neither is set latchkey still tries the well-known
  local server ``http://127.0.0.1:8930`` (openbrowser's ``jev-local-server``);
  if it is not up the asker falls over to the cloud.
* cloud key - ``TYPESAFE_API_KEY`` / ``~/.openbrowser/typesafe.key`` (typesafe),
  or an ``sk-or-`` ``OPENROUTER_API_KEY`` / ``~/.openbrowser/jev.key``
  (OpenRouter). Used alone when no base URL resolves, or as the local server's
  second opinion and stand-in otherwise (``JEV_ESCALATE=off`` keeps it local).

The key directory is ``LATCHKEY_JEV_KEY_DIR`` or ``~/.openbrowser`` (shared with
openbrowser on purpose); ``LATCHKEY_JEV_KEY_DIR`` pointed at nothing disables the
file lookup, which is how tests keep a real machine key out of their output.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

# Confidence gates, ported verbatim from openbrowser's pilot so behaviour matches.
GOAL_THRESHOLD = 0.85
GOAL_PROBABLE = 0.6
STUCK_THRESHOLD = 0.85
DEFAULT_MIN_CONFIDENCE = 0.2
PRICE_PER_INPUT_TOKEN = 0.042 / 1e6      # USD; output is free

# The well-known local Jev server (openbrowser's jev-local-server.mjs default).
LOCAL_DEFAULT_URL = "http://127.0.0.1:8930"

NO_KEY = (
    "latchkey_pilot needs a Jev decision endpoint and none answered - nothing was done. "
    "Run a local Jev server (openbrowser's scripts/jev-local-server.mjs on :8930), set "
    "LATCHKEY_JEV_BASE_URL to any /v1/systemone server, or put an sk-or- OpenRouter key in "
    "~/.openbrowser/jev.key. Until then, drive the page with latchkey_snapshot + latchkey_act."
)


def _key_dir(env) -> Optional[Path]:
    raw = env.get("LATCHKEY_JEV_KEY_DIR")
    if raw is not None:
        return Path(raw) if raw else None      # explicit empty string disables file lookup
    return Path(env.get("HOME", str(Path.home()))) / ".openbrowser"


def _key_file(env, name: str) -> str:
    base = _key_dir(env)
    if base is None:
        return ""
    try:
        return (base / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cloud_transport(env) -> Optional[dict]:
    typesafe = env.get("TYPESAFE_API_KEY") or _key_file(env, "typesafe.key")
    if typesafe:
        return {"via": "typesafe", "url": "https://api.typesafe.ai/v1/systemone",
                "key": typesafe, "model": "jev-latest", "chunk": 240, "paid": True}
    openrouter = env.get("OPENROUTER_API_KEY") or _key_file(env, "jev.key")
    if openrouter.startswith("sk-or-"):
        return {"via": "openrouter", "url": "https://openrouter.ai/api/alpha/decisions",
                "key": openrouter, "model": "typesafe/jev-1.13", "chunk": 240, "paid": True}
    return None


def transport(env=None) -> Optional[dict]:
    """Resolve the Jev transport, or ``None`` when nothing is configured."""
    env = os.environ if env is None else env
    if env.get("LATCHKEY_PILOT", "").lower() == "off":
        return None
    try:
        chunk = max(8, min(240, int(env["JEV_MAX_OPTIONS"]) - 2))
    except (KeyError, ValueError):
        chunk = 240
    cloud = _cloud_transport(env)
    # An explicit base (even empty, meaning "no local server") is respected; only
    # an unset base falls back to the well-known local server.
    if "LATCHKEY_JEV_BASE_URL" in env or "JEV_BASE_URL" in env:
        base = env.get("LATCHKEY_JEV_BASE_URL") or env.get("JEV_BASE_URL")
    else:
        base = LOCAL_DEFAULT_URL
    if base:
        local = {"via": "local", "url": base.rstrip("/") + "/v1/systemone",
                 "key": env.get("JEV_API_KEY", "none"),
                 "model": env.get("JEV_MODEL", "jev-latest"), "chunk": chunk, "paid": False}
        if cloud and env.get("JEV_ESCALATE") != "off":
            local["backup"] = cloud
        return local
    if cloud:
        return {**cloud, "chunk": chunk}
    return None


# ------------------------------------------------------------------ questions

def choice(instructions: str, criteria: dict) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def noul(instructions: str, criteria: dict) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": criteria}


def prob(answer: Any) -> float:
    if isinstance(answer, dict):
        if isinstance(answer.get("noul"), (int, float)):
            return float(answer["noul"])
        try:
            return float(answer.get("probability") or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


HANDBACK = "handback"
BACK = "back"
HANDBACK_LABEL = ("Stop and ask the supervising agent: none of the offered actions can "
                  "advance the task from this page")

REASONS = {
    "needs_text": "the next step needs text typed that was not supplied",
    "needs_login": ("the page is itself a sign-in or verification step (a login form or "
                    "password field is on it). A page that merely offers a log-in link is not this"),
    "judgment": "several options look equally plausible and picking one is a judgment call",
    "irreversible": "the next step would submit, buy, send, delete or otherwise commit something",
    "not_here": "nothing on this page leads toward the goal",
}

ACTION = ("Which single action (clicking an element, or typing the given text into a field) "
          "best advances the task from the current page?")

JUDGMENTS = {
    "handback_reason": choice(
        "If the task cannot simply continue by clicking or typing the supplied text, "
        "what is the obstacle?", REASONS),
    "stuck": noul(
        "The actions so far are not making progress toward the task (repeats, loops, or no change)",
        {"true": "Recent actions repeat or nothing changes; a different strategy is needed",
         "false": "Progress is visible or the first steps are still reasonable"}),
}

GOAL_QUESTION = {
    "goal_done": noul(
        "The task is complete: the current page shows the end result the task asks for",
        {"true": ("The url, title, headings and selected items show the page the task wanted to "
                  "reach, or show that what the task asks to be done (chosen, ticked, added, typed, "
                  "applied) has been done"),
         "false": ("This is a different page, or it is the right page but what the task asks to be "
                   "done has not been done yet")})
}


# -------------------------------------------------------------------- client

class JevError(RuntimeError):
    """Jev endpoint failed; the pilot stops and hands back to snapshot + act."""


def _client(t: dict, usage: dict, fetch: Optional[Callable] = None) -> Callable:
    timeout = 45 if t.get("via") == "local" else 20

    def ask(state: dict, questions: dict) -> dict:
        started = time.time()
        if fetch is not None:                       # test seam
            answers, tokens = fetch(t, state, questions)
        else:
            body = json.dumps({"model": t["model"], "state": state, "questions": questions}).encode()
            req = urllib.request.Request(
                t["url"], data=body, method="POST",
                headers={"Authorization": f"Bearer {t['key']}", "Content-Type": "application/json",
                         "X-Title": "latchkey"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                # Never echo the request: it carries the user's page content.
                raise JevError(f"Jev ({t['via']}) answered {exc.code}: "
                               f"{exc.read()[:200].decode('utf-8', 'replace')}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise JevError(f"Jev ({t['via']}) unreachable: {exc}") from None
            answers = payload.get("answers") or {}
            tokens = (payload.get("usage") or {}).get("input_tokens") or 0
        usage["calls"] += 1
        usage["ms"] += int((time.time() - started) * 1000)
        if t.get("paid") is not False:
            usage["input"] += int(tokens)
        usage["by"][t["via"]] = usage["by"].get(t["via"], 0) + 1
        if os.environ.get("LATCHKEY_PILOT_DEBUG"):
            brief = json.dumps(answers)[:600]
            import sys
            sys.stderr.write(f"[pilot] {t['via']} {','.join(questions)} -> {brief}\n")
        return answers

    return ask


def new_usage() -> dict:
    return {"calls": 0, "ms": 0, "input": 0, "by": {}, "failover": 0}


def asker(t: dict, usage: dict, fetch: Optional[Callable] = None) -> Callable:
    """The asker the loop uses: primary transport, cloud backup on error or a big
    option list, exposed second opinion for the decisions that end an errand."""
    primary = _client(t, usage, fetch)
    backup = _client(t["backup"], usage, fetch) if t.get("backup") else None
    state = {"down": False}
    try:
        local_max = int(os.environ.get("JEV_LOCAL_MAX_OPTIONS", "120"))
    except ValueError:
        local_max = 120

    def size(questions: dict) -> int:
        return max([0] + [len(q.get("criteria") or {}) for q in questions.values()
                          if q.get("type") == "choice"])

    def ask(state_obj: dict, questions: dict) -> dict:
        if state["down"] and backup:
            return backup(state_obj, questions)
        if backup and t.get("via") == "local" and size(questions) > local_max:
            return backup(state_obj, questions)
        if backup is None:
            return primary(state_obj, questions)
        try:
            return primary(state_obj, questions)
        except JevError:
            state["down"] = True
            usage["failover"] += 1
            return backup(state_obj, questions)

    ask.second = backup                                    # type: ignore[attr-defined]
    ask.second_chunk = (t.get("backup") or {}).get("chunk", 240)  # type: ignore[attr-defined]
    ask.is_down = lambda: state["down"]                    # type: ignore[attr-defined]
    return ask


def decide(ask: Callable, state: dict, items: list, chunk_size: int = 240) -> dict:
    """One step's decision: pick the ref that best advances the task, plus the
    stuck/handback judgments. A page with more options than one Choice can hold
    is asked in parallel chunks whose winners meet in a run-off."""
    def criteria_of(rows, with_none):
        crit = {row["ref"]: row["label"] for row in rows}
        if with_none:
            crit["none"] = "None of these elements helps with the task"
        else:
            crit[HANDBACK] = HANDBACK_LABEL
        return crit

    chunks = [items[i:i + chunk_size] for i in range(0, max(1, len(items)), chunk_size)] or [[]]
    if len(chunks) == 1:
        a = ask(state, {"action": choice(ACTION, criteria_of(chunks[0], False)), **JUDGMENTS})
        return {"action": a.get("action"), "stuck": prob(a.get("stuck")),
                "reason": (a.get("handback_reason") or {}).get("choice")}

    answers = [ask(state, {"action": choice(ACTION, criteria_of(rows, True)),
                           **(JUDGMENTS if i == 0 else {})})
               for i, rows in enumerate(chunks)]
    base = {"stuck": prob(answers[0].get("stuck")),
            "reason": (answers[0].get("handback_reason") or {}).get("choice")}
    winners = [a for a in answers if (a.get("action") or {}).get("choice") not in (None, "none")]
    if not winners:
        return {**base, "action": None}
    if len(winners) == 1:
        return {**base, "action": winners[0]["action"]}
    by_ref = {row["ref"]: row for row in items}
    finalists = [by_ref[a["action"]["choice"]] for a in winners if a["action"]["choice"] in by_ref]
    final = ask(state, {"action": choice(ACTION, criteria_of(finalists, False))})
    return {**base, "action": final.get("action")}


def describe_usage(usage: dict) -> str:
    by = list(usage.get("by", {}).items())
    split = ""
    if len(by) > 1 or (len(by) == 1 and by[0][0] == "local"):
        split = ", " + " + ".join(f"{n} {k}" for k, n in by)
    down = ", local server down: used the cloud" if usage.get("failover") else ""
    return f"{usage['calls']} Jev calls ({usage['ms']}ms{split}{down})"


def cost_usd(usage: dict) -> float:
    return usage.get("input", 0) * PRICE_PER_INPUT_TOKEN
