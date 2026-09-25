"""Jev transport/client/decide, with a fake fetch so no network or key is touched."""
import pytest

from latchkey import jev


def test_transport_prefers_base_url_with_cloud_backup(monkeypatch, tmp_path):
    (tmp_path / "jev.key").write_text("sk-or-abc123")
    env = {"LATCHKEY_JEV_KEY_DIR": str(tmp_path), "LATCHKEY_JEV_BASE_URL": "http://127.0.0.1:8930"}
    t = jev.transport(env)
    assert t["via"] == "local" and t["url"] == "http://127.0.0.1:8930/v1/systemone"
    assert t["backup"]["via"] == "openrouter"          # cloud attached as second opinion


def test_transport_cloud_only_when_no_base(tmp_path):
    (tmp_path / "jev.key").write_text("sk-or-xyz")
    # No base URL and the local default is unreachable in a test, but resolution is
    # pure: an explicit empty base disables the local default so cloud stands alone.
    env = {"LATCHKEY_JEV_KEY_DIR": str(tmp_path), "LATCHKEY_JEV_BASE_URL": "", "JEV_BASE_URL": ""}
    t = jev.transport(env)
    assert t["via"] == "openrouter" and "backup" not in t


def test_transport_none_when_disabled(tmp_path):
    env = {"LATCHKEY_JEV_KEY_DIR": str(tmp_path), "LATCHKEY_PILOT": "off"}
    assert jev.transport(env) is None


def test_transport_empty_key_dir_finds_nothing():
    env = {"LATCHKEY_JEV_KEY_DIR": "", "JEV_BASE_URL": ""}
    assert jev.transport(env) is None                  # no base, no key file lookup


def test_escalate_off_keeps_it_local(tmp_path):
    (tmp_path / "jev.key").write_text("sk-or-abc")
    env = {"LATCHKEY_JEV_KEY_DIR": str(tmp_path), "JEV_BASE_URL": "http://x", "JEV_ESCALATE": "off"}
    t = jev.transport(env)
    assert t["via"] == "local" and "backup" not in t


def test_decide_single_chunk_picks_and_reads_judgments():
    t = {"via": "local", "url": "u", "key": "none", "model": "m", "paid": False, "chunk": 240}
    usage = jev.new_usage()

    def fetch(transport, state, questions):
        assert "action" in questions and "stuck" in questions
        return ({"action": {"choice": "e7", "confidence": 0.9, "probabilities": {"e7": 0.9, "e3": 0.1}},
                 "stuck": {"noul": 0.2}, "handback_reason": {"choice": "not_here"}}, 12)

    ask = jev.asker(t, usage, fetch)
    out = jev.decide(ask, {"task": "t"}, [{"ref": "e7", "label": "Login"}, {"ref": "e3", "label": "Cart"}])
    assert out["action"]["choice"] == "e7" and out["stuck"] == pytest.approx(0.2)
    assert out["reason"] == "not_here"
    assert usage["calls"] == 1 and usage["input"] == 0        # local is free


def test_asker_fails_over_to_backup_on_error():
    t = {"via": "local", "url": "u", "key": "none", "model": "m", "paid": False,
         "backup": {"via": "openrouter", "url": "c", "key": "k", "model": "jev", "paid": True}}
    usage = jev.new_usage()
    calls = {"n": 0}

    def fetch(transport, state, questions):
        if transport["via"] == "local":
            calls["n"] += 1
            raise jev.JevError("boom")
        return ({"goal_done": {"noul": 0.9}}, 5)

    ask = jev.asker(t, usage, fetch)
    a = ask({"task": "t"}, jev.GOAL_QUESTION)
    assert jev.prob(a["goal_done"]) == pytest.approx(0.9)
    assert usage["failover"] == 1 and usage["by"]["openrouter"] == 1


def test_prob_reads_noul_and_probability_shapes():
    assert jev.prob({"noul": 0.7}) == pytest.approx(0.7)
    assert jev.prob({"probability": 0.4}) == pytest.approx(0.4)
    assert jev.prob(None) == 0.0


def test_describe_usage_names_the_split():
    usage = {"calls": 3, "ms": 900, "input": 0, "by": {"local": 2, "openrouter": 1}, "failover": 1}
    text = jev.describe_usage(usage)
    assert "3 Jev calls" in text and "2 local + 1 openrouter" in text and "used the cloud" in text
