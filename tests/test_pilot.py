"""The pilot loop, driven with a fake browser and a scripted Jev so the control
flow (roster -> decide -> execute -> arrival/handback) is proven without a real
Chrome or a live model."""
import types

import pytest

from latchkey import jev, pilot


class FakeState:
    def __init__(self, url, title="", text=""):
        self.url, self.title, self.text = url, title, text


class FakeKeyboard:
    def __init__(self):
        self.pressed = []

    def press(self, combo):
        self.pressed.append(combo)


class FakeRefs:
    def selector(self, ref):
        return f'[data-lk-ref="{ref}"]'


class FakeBrowser:
    """Scripted page: ``script`` is a list of (state, nodes) per look; actions
    record what happened and advance the script."""
    def __init__(self, script):
        self.script = script
        self.i = 0
        self.refs = FakeRefs()
        self.page = types.SimpleNamespace(keyboard=FakeKeyboard())
        self.did = []

    def _cur(self):
        return self.script[min(self.i, len(self.script) - 1)]

    def state(self, *a, **k):
        return self._cur()[0]

    def _nodes(self):
        return self._cur()[1]

    def goto(self, url):
        self.did.append(("goto", url))

    def click(self, selector):
        self.did.append(("click", selector))
        self._advance()

    def fill(self, selector, value):
        self.did.append(("fill", selector, value))
        self._advance()

    def back(self):
        self.did.append(("back",))
        self._advance()

    def _advance(self):
        if self.i < len(self.script) - 1:
            self.i += 1


@pytest.fixture(autouse=True)
def _stub_a11y(monkeypatch):
    monkeypatch.setattr(pilot.a11y, "nodes", lambda b, **k: {"nodes": b._nodes(), "frames": []})
    monkeypatch.setattr(pilot.a11y, "remember", lambda b, d: None)
    monkeypatch.setattr(pilot.a11y, "read", lambda b, **k: {"text": "visible text"})


def scripted_jev(steps):
    """steps: list of dicts per step: {choice, conf?, goal?, stuck?, reason?, probs?}."""
    state = {"i": 0}

    def ask(_state, questions):
        step = steps[min(state["i"], len(steps) - 1)]
        if "goal_done" in questions:
            return {"goal_done": {"noul": step.get("goal", 0.0)}}
        # action call also advances the script step counter
        answer = {"action": {"choice": step.get("choice"), "confidence": step.get("conf", 1.0),
                             "probabilities": step.get("probs", {})},
                  "stuck": {"noul": step.get("stuck", 0.0)},
                  "handback_reason": {"choice": step.get("reason", "not_here")}}
        state["i"] += 1
        return answer

    ask.second = None
    ask.second_chunk = 240
    return ask


def run(browser, jev_ask, **kw):
    return pilot.run_errand(browser, kw.pop("goal", "reach the goal"),
                            jev_ask=jev_ask, usage=jev.new_usage(), **kw)


def test_clicks_the_ref_jev_picks_then_arrives():
    nodes = [{"ref": "e7", "role": "button", "name": "Next"}]
    browser = FakeBrowser([(FakeState("https://site.test/a", "A"), nodes),
                           (FakeState("https://site.test/b", "B"), nodes)])
    out = run(browser, scripted_jev([{"choice": "e7", "goal": 0.0}, {"choice": "e7", "goal": 0.95}]))
    assert ("click", '[data-lk-ref="e7"]') in browser.did
    assert out["outcome"] == "arrived" and out["ok"] is True


def test_types_supplied_value_into_a_field():
    nodes = [{"ref": "e1", "role": "searchbox", "name": "Search"}]
    browser = FakeBrowser([(FakeState("https://site.test", "S"), nodes),
                           (FakeState("https://site.test/q", "S"), nodes)])
    out = run(browser, scripted_jev([{"choice": "type:0:e1", "goal": 0.0}, {"choice": None, "goal": 0.95}]),
              type_values=["hello world"])
    assert ("fill", '[data-lk-ref="e1"]', "hello world") in browser.did
    assert out["outcome"] == "arrived"


def test_irreversible_controls_are_never_offered():
    seen = {}
    nodes = [{"ref": "e2", "role": "button", "name": "Submit order"},
             {"ref": "e3", "role": "link", "name": "Help"}]
    browser = FakeBrowser([(FakeState("https://s.test", "S"), nodes)] * 2)

    def ask(state, questions):
        if "action" in questions:
            seen.update(questions["action"]["criteria"])
        if "goal_done" in questions:
            return {"goal_done": {"noul": 0.0}}
        return {"action": {"choice": jev.HANDBACK, "confidence": 1.0, "probabilities": {}},
                "stuck": {"noul": 0.0}, "handback_reason": {"choice": "irreversible"}}
    ask.second = None
    ask.second_chunk = 240
    out = run(browser, ask, max_steps=1)
    assert "e2" not in seen and "e3" in seen          # Submit filtered, Help offered
    assert out["outcome"] == "handback" and out["handedBack"] is True


def test_handback_carries_reason_and_ranked_candidates():
    nodes = [{"ref": "e4", "role": "button", "name": "Maybe"},
             {"ref": "e5", "role": "button", "name": "Perhaps"}]
    browser = FakeBrowser([(FakeState("https://s.test", "S"), nodes)] * 2)
    out = run(browser, scripted_jev([{"choice": jev.HANDBACK, "goal": 0.0, "reason": "judgment",
                                      "probs": {"e4": 0.4, "e5": 0.3, "handback": 0.3}}]), max_steps=1)
    assert out["outcome"] == "handback"
    assert "several options look equally plausible" in out["text"]
    assert "e4 (p=0.40)" in out["text"] and "e5 (p=0.30)" in out["text"]


def test_low_confidence_hands_back():
    nodes = [{"ref": "e9", "role": "button", "name": "Unsure"}]
    browser = FakeBrowser([(FakeState("https://s.test", "S"), nodes)] * 2)
    out = run(browser, scripted_jev([{"choice": "e9", "conf": 0.1, "goal": 0.0}]),
              min_confidence=0.3, max_steps=2)
    assert out["outcome"] == "handback"
    assert browser.did == []                           # nothing clicked below confidence


def test_offsite_click_is_walked_back():
    nodes = [{"ref": "e1", "role": "link", "name": "External"}]
    browser = FakeBrowser([(FakeState("https://site.test", "Home"), nodes),
                           (FakeState("https://elsewhere.test/x", "Away"), nodes),
                           (FakeState("https://site.test", "Home"), nodes)])
    out = run(browser, scripted_jev([{"choice": "e1", "goal": 0.0}, {"choice": None, "goal": 0.0}]),
              max_steps=2)
    assert ("click", '[data-lk-ref="e1"]') in browser.did
    assert ("back",) in browser.did                    # wandered off-host, walked back


def test_until_url_stops_the_errand():
    nodes = [{"ref": "e1", "role": "button", "name": "Go"}]
    browser = FakeBrowser([(FakeState("https://s.test/done", "Done"), nodes)])
    out = run(browser, scripted_jev([{"choice": "e1", "goal": 0.0}]), until={"url": "/done"})
    assert out["outcome"] == "reached" and browser.did == []


def test_pressing_a_supplied_key_normalizes_mod():
    nodes = [{"ref": "e1", "role": "button", "name": "Canvas"}]
    browser = FakeBrowser([(FakeState("https://app.test", "App"), nodes)] * 2)
    run(browser, scripted_jev([{"choice": "key:Mod+z", "goal": 0.0}, {"choice": None, "goal": 0.95}]),
        keys={"undo": "Mod+z"})
    assert browser.page.keyboard.pressed and browser.page.keyboard.pressed[0] in ("Meta+z", "Control+z")
