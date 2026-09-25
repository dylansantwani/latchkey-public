"""Session persistence: keep a login you performed, so it survives restarts.

Cookies and localStorage are saved per site under ~/.latchkey/sessions/ with mode
0600. Cookies are stored in CDP's own shape so they can be fed straight back into
Network.setCookies, partition keys included.
"""
from __future__ import annotations

import json
import os
import time

from . import paths
from dataclasses import dataclass, field

SESSION_DIR = paths.path("sessions")


@dataclass
class Session:
    site: str
    cookies: list[dict] = field(default_factory=list)
    local_storage: dict[str, dict[str, str]] = field(default_factory=dict)
    saved_at: float = 0.0

    def as_dict(self) -> dict:
        return {"site": self.site, "cookies": self.cookies,
                "local_storage": self.local_storage, "saved_at": self.saved_at}


def _path(site: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in site)
    return os.path.join(SESSION_DIR, f"{safe}.json")


def save(site: str, cookies: list[dict],
         local_storage: dict[str, dict[str, str]] | None = None) -> str:
    os.makedirs(SESSION_DIR, exist_ok=True)
    session = Session(site=site, cookies=cookies,
                      local_storage=local_storage or {}, saved_at=time.time())
    path = _path(site)
    with open(path, "w", encoding="utf-8") as fh:
        os.chmod(path, 0o600)
        json.dump(session.as_dict(), fh, indent=2)
    return path


def load(site: str) -> Session | None:
    path = _path(site)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return Session(**json.load(fh))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def load_all() -> list[Session]:
    if not os.path.isdir(SESSION_DIR):
        return []
    out = []
    for name in sorted(os.listdir(SESSION_DIR)):
        if name.endswith(".json"):
            session = load(name[:-5])
            if session:
                out.append(session)
    return out


def clear(site: str) -> bool:
    path = _path(site)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def describe() -> list[dict]:
    """Summary of stored sessions - names and counts, never values."""
    out = []
    for session in load_all():
        origins = {c.get("domain") or c.get("url", "") for c in session.cookies}
        out.append({
            "site": session.site,
            "cookies": len(session.cookies),
            "partitioned": sum(1 for c in session.cookies if c.get("partitionKey")),
            "local_storage_origins": len(session.local_storage),
            "saved_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(session.saved_at))
            if session.saved_at else None,
            "domains": sorted(d for d in origins if d)[:6],
        })
    return out


# -- browser-side helpers ----------------------------------------------------

CAPTURE_LOCAL_STORAGE_JS = "() => Object.fromEntries(Object.entries(localStorage))"


def without_live_session(cookies: list[dict]) -> list[dict]:
    """Saved cookies minus a live Google account session, unless the human said to share it.

    A saved session is loaded into every later inject session, so a Google login saved from
    a dedicated browser (or by an older version) would put the one thing latchkey never
    copies into a second client after all - the route item 20 of TODO.md closed for the
    cookie jar and left open here.
    """
    from . import cookies as ck
    if ck.share_live_session():
        return list(cookies)
    return [c for c in cookies
            if not ck.is_live_cookie(c.get("domain") or _url_host(c.get("url")), c.get("name"))]


def _url_host(url: str | None) -> str:
    from urllib.parse import urlparse
    return urlparse(url or "").netloc


def snapshot_cookies(cdp) -> list[dict]:
    """All cookies the browser currently holds, in setCookies-compatible shape.

    A live Google account session is left out: see `without_live_session`.
    """
    raw = cdp.send("Network.getAllCookies").get("cookies", [])
    out = []
    for c in raw:
        entry = {k: c[k] for k in ("name", "value", "domain", "path", "secure",
                                   "httpOnly", "expires")
                 if k in c and c[k] is not None}
        if c.get("sameSite"):
            entry["sameSite"] = c["sameSite"]
        if c.get("partitionKey"):
            entry["partitionKey"] = c["partitionKey"]
        out.append(entry)
    return without_live_session(out)


def local_storage_init_script(session: Session) -> str | None:
    """An init script that reinstates a session's localStorage on the right origin."""
    if not session.local_storage:
        return None
    blocks = []
    for origin, data in session.local_storage.items():
        blocks.append(f"""if (location.origin === {json.dumps(origin)}) {{
            const d = {json.dumps(data)};
            for (const k in d) {{ try {{ localStorage.setItem(k, d[k]); }} catch (e) {{}} }}
        }}""")
    return "(() => {\n" + "\n".join(blocks) + "\n})();"
