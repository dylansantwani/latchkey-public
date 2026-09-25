"""Never-sign-out guarantees: latchkey must not route a Google host through the
clone path (two browsers on one device-bound session is what signs the user out),
and the docs agents read must name the dedicated profile, not the old clone."""
from latchkey import google, mcp_server as m


def _stub_probe(monkeypatch, url):
    """Make the guard's `current url` probe return `url` without a real browser."""
    monkeypatch.setattr(m, "_on_session", lambda s, fn, **k: url)


def test_use_clone_refuses_on_a_google_host(monkeypatch):
    monkeypatch.setattr(google, "google_mode", lambda: "dedicated")
    _stub_probe(monkeypatch, "https://mail.google.com/mail/u/0")
    out = m.tool_use_clone(session="default")
    assert out.get("refused") is True and out["mode"] == "unchanged"
    assert "signs the user out" in out["note"] and "dedicated" in out["note"]


def test_use_clone_refuses_on_a_google_sso_portal(monkeypatch):
    monkeypatch.setattr(google, "google_mode", lambda: "dedicated")
    monkeypatch.setenv(google.GOOGLE_SSO_ACCOUNTS_ENV, '{"portal.example.edu": "school"}')
    _stub_probe(monkeypatch, "https://portal.example.edu/campus/portal")
    assert m.tool_use_clone(session="default").get("refused") is True


def test_use_clone_allows_a_non_google_host(monkeypatch):
    monkeypatch.setattr(google, "google_mode", lambda: "dedicated")
    seq = iter(["https://example.com/page", {"clone": {}, "report": {}}])
    monkeypatch.setattr(m, "_on_session", lambda s, fn, **k: next(seq))
    monkeypatch.setattr(m.registry, "spec_of", lambda s: None)
    monkeypatch.setattr(m.registry, "get", lambda *a, **k: None)
    out = m.tool_use_clone(session="default")
    assert out["mode"] == "clone" and not out.get("refused")


def test_force_overrides_the_google_guard(monkeypatch):
    monkeypatch.setattr(google, "google_mode", lambda: "dedicated")
    # force=true means the guard never even probes; go straight to the clone.
    monkeypatch.setattr(m, "_on_session", lambda s, fn, **k: {"clone": {}, "report": {}})
    monkeypatch.setattr(m.registry, "spec_of", lambda s: None)
    monkeypatch.setattr(m.registry, "get", lambda *a, **k: None)
    out = m.tool_use_clone(session="default", force=True)
    assert out["mode"] == "clone" and not out.get("refused")


def test_guard_is_skipped_when_google_mode_is_clone(monkeypatch):
    # A user who deliberately set LATCHKEY_GOOGLE_MODE=clone is not second-guessed.
    monkeypatch.setattr(google, "google_mode", lambda: "clone")
    monkeypatch.setattr(m, "_on_session", lambda s, fn, **k: {"clone": {}, "report": {}})
    monkeypatch.setattr(m.registry, "spec_of", lambda s: None)
    monkeypatch.setattr(m.registry, "get", lambda *a, **k: None)
    out = m.tool_use_clone(session="default")
    assert out["mode"] == "clone" and not out.get("refused")


def test_google_docs_name_the_dedicated_default_not_clone():
    topic = m.HELP_TOPICS["google"]
    assert "dedicated" in topic and "signed in once" in topic
    # the old, sign-out-causing claim must be gone
    assert "mode clone, chosen\n" not in topic and "moves to clone" not in topic
    assert "Do NOT switch Google to mode clone" in topic


def test_open_and_session_open_help_and_alias_are_consistent():
    assert m.MODE_ALIASES["google"] == "dedicated"
    open_detail = m.HELP_DETAIL["latchkey_open"]
    assert "dedicated (latchkey's own signed-in-once profile) for Google" in open_detail
    assert "clone (a copy of the user's own profile) for Google" not in open_detail
    session_help = m.HELP_DETAIL["latchkey_session_open"]
    assert "dedicated for Google" in session_help
    assert "never what Google needs" not in session_help


def test_session_open_error_names_dedicated_for_google():
    try:
        m._session_mode("bogus-mode")
    except ValueError as exc:
        assert "dedicated for Google-backed sites" in str(exc)
    else:
        raise AssertionError("bad mode should raise")
