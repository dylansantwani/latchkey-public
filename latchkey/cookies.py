"""Read Chrome's cookie store on macOS.

Everything here is read-only and in-memory: decrypted cookie values are never
written to disk and never logged. The Keychain prompt you see is macOS asking
to release Chrome's "Safe Storage" key, which decrypts every cookie in the
profile you point this at.
"""
from __future__ import annotations

import functools
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CHROME_ROOT = os.path.expanduser("~/Library/Application Support/Google/Chrome")
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SPACES = b" " * 16
# Chrome timestamps are microseconds since 1601-01-01.
CHROME_EPOCH_OFFSET = 11_644_473_600
# Chrome's samesite column -> DevTools Protocol / Playwright vocabulary.
# -1 means "unspecified", which both APIs express by omitting the field.
SAMESITE = {-1: None, 0: "None", 1: "Lax", 2: "Strict"}


@dataclass(frozen=True)
class Cookie:
    host: str
    name: str
    value: str
    path: str
    secure: bool
    http_only: bool
    samesite: int
    expires: float          # unix seconds, or -1 for a session cookie
    partitioned: bool       # CHIPS: scoped to an embedded top-level site
    # The top-level site the cookie is scoped to, e.g. "https://quizlet.com".
    # NOT the cookie's own host: an embedded YouTube player on quizlet.com gets
    # its .youtube.com cookies partitioned under quizlet.com.
    partition_site: str | None = None

    @property
    def is_session(self) -> bool:
        return self.expires < 0

    @property
    def is_host_only(self) -> bool:
        """__Host- cookies must be set without a Domain attribute."""
        return self.name.startswith("__Host-")

    @property
    def site(self) -> str:
        return self.host.lstrip(".")


@functools.lru_cache(maxsize=4)
def key(service: str = "Chrome Safe Storage", account: str = "Chrome") -> bytes:
    """Fetch Chrome's AES key from the login Keychain. macOS will prompt once."""
    pw = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", service, "-a", account],
        capture_output=True, check=True).stdout.rstrip(b"\n")
    return hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1003, 16)


def _cbc(dek: bytes, iv: bytes, ct: bytes) -> bytes:
    d = Cipher(algorithms.AES(dek), modes.CBC(iv)).decryptor()
    return d.update(ct) + d.finalize()


def _printable(raw: bytes) -> bool:
    """Is every byte one a cookie value is allowed to be?"""
    return all(32 <= b < 127 for b in raw)


def _unpad(raw: bytes) -> bytes | None:
    """The plaintext without its PKCS#7 padding, or None if the padding is not valid."""
    if not raw:
        return None
    pad = raw[-1]
    if not 1 <= pad <= 16 or raw[-pad:] != bytes([pad]) * pad:
        return None
    return raw[:-pad]


def _without_domain_prefix(plain: bytes, host: str | None) -> bytes | None:
    """The cookie's own value, with Chrome's domain hash taken off if one is there.

    Chrome binds a cookie to its host by prefixing the plaintext with a 32-byte hash of
    the host before encrypting, and older values have no such prefix. Both layouts are
    in one profile at once, so the question has to be asked per cookie - and it can be
    asked *exactly* rather than guessed at:

      * the hash, when we know the host the row was read from: the prefix either is
        SHA-256 of that host or it is not;
      * failing that, the boundary itself. A cookie value is printable ASCII and a
        SHA-256 digest is not, so "the first 32 bytes are not text and the rest is" is
        a 32-byte prefix, and "all of it is text" is a value with no prefix.

    None means neither shape fits, and the caller falls back to searching the layouts.
    """
    if host is not None:
        # Measured against this machine's own store: every encrypted row's plaintext
        # begins with SHA-256 of `host_key` exactly as the database spells it, leading
        # dot and all. So when the host is known the question is closed either way - a
        # prefix that is not that hash is not a prefix, it is the value.
        if len(plain) >= 32 and plain[:32] == hashlib.sha256(host.encode()).digest():
            return plain[32:]
        return plain
    if _printable(plain):
        return plain
    if len(plain) > 32 and _printable(plain[32:]) and not _printable(plain[:32]):
        return plain[32:]
    return None


def _decrypt_by_search(body: bytes, dek: bytes) -> bytes:
    """The layout that looks most like a cookie value, for a row neither shape fits.

    Each plausible header length, scored on how much of the result reads as text. Note
    the IV: skipping `hlen` bytes of *plaintext* means starting at that block, and in CBC
    the IV for a block is the ciphertext before it - which is why this can skip a prefix
    without decrypting it.
    """
    best = (-1.0, b"")
    for hlen in (0, 16, 32, 48):
        ct = body[hlen:]
        if not ct or len(ct) % 16:
            continue
        iv = body[hlen - 16:hlen] if hlen >= 16 else SPACES
        raw = _cbc(dek, iv, ct)
        text = _unpad(raw)
        padded = text is not None
        if text is None:
            text = raw
        if not text:
            continue
        printable = sum(32 <= b < 127 for b in text) / len(text)
        score = printable + (0.5 if padded else 0.0)
        if score > best[0]:
            best = (score, text)
    return best[1]


def decrypt(blob: bytes, dek: bytes, host: str | None = None) -> bytes:
    """Decrypt one `encrypted_value`.

    One AES pass for every row that fits either layout Chrome writes, which is all of
    them in practice. The four-pass search below it used to run for every cookie, and it
    decided between the layouts by comparing how printable each result was overall - a
    tie-break that could, for a value with any non-ASCII in it, prefer the candidate that
    is simply 32 characters shorter and hand back a silently truncated cookie.
    """
    if not blob.startswith((b"v10", b"v11")):
        return blob
    body = blob[3:]
    if body and len(body) % 16 == 0:
        plain = _unpad(_cbc(dek, SPACES, body))
        if plain is not None:
            value = _without_domain_prefix(plain, host)
            if value is not None:
                return value
    return _decrypt_by_search(body, dek)


@functools.lru_cache(maxsize=1)
def chrome_version() -> str | None:
    """The installed Chrome's version. Cached: it costs a process, and it cannot change
    under a running server without that server being restarted anyway."""
    try:
        out = subprocess.run([CHROME_BIN, "--version"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        return out.split()[-1] or None
    except Exception:  # noqa: BLE001
        return None


@functools.lru_cache(maxsize=1)
def user_agent() -> str:
    ver = chrome_version() or "140.0.0.0"
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver} Safari/537.36")


def profiles() -> dict[str, str]:
    """Map profile directory name -> Cookies database path."""
    if not os.path.isdir(CHROME_ROOT):
        return {}
    out = {}
    for entry in sorted(os.listdir(CHROME_ROOT)):
        db = os.path.join(CHROME_ROOT, entry, "Cookies")
        if os.path.isfile(db) and (entry == "Default" or entry.startswith("Profile ")):
            out[entry] = db
    return out


def resolve_profiles(spec: list[str] | str | None) -> list[str]:
    """Turn None / "all" / a name / a list of names into concrete profile names.

    Every profile in one Chrome install shares the same Keychain key, so cookies
    from all of them decrypt with the same call. Reading more than one is cheap.

    "all" means all of them wherever it appears - `"all"`, `'all'`, `['all']`,
    `['Default', 'all']`. The list form is what an agent passes after reading a tool
    description that says "pass ['all'] or 'all'", and it used to raise
    `KeyError: unknown profile(s): all` because only the bare string was understood.
    The documented shorthand failing while the long way round works is the worst kind
    of bug: it costs a whole dig through Chrome's SQLite files to find the name that
    would have worked.
    """
    available = profiles()
    if not available:
        raise FileNotFoundError(f"no Chrome profiles under {CHROME_ROOT}")
    if spec is None:
        return ["Default"] if "Default" in available else [next(iter(available))]
    names = [spec] if isinstance(spec, str) else list(spec)
    cleaned: list[str] = []
    for name in names:
        text = str(name).strip()
        if text.lower() in ("all", "*"):
            return list(available)
        if text:
            cleaned.append(text)
    if not cleaned:
        return ["Default"] if "Default" in available else [next(iter(available))]
    missing = [name for name in cleaned if name not in available]
    if missing:
        raise KeyError(f"unknown profile(s): {', '.join(missing)}; "
                       f"have: {', '.join(available)} (or 'all' for every one)")
    return cleaned


def _host_candidates(host: str) -> list[str]:
    """A host and its parent domain, because a session is rarely stored on the exact page.

    A site signed in at `www.webassign.net` keeps its cookie on `.webassign.net`, and the
    cookie filter is a substring match on the cookie's own host - so asking about
    `www.webassign.net` finds nothing and the answer looks like "no profile has it",
    which is the wrong answer in the most common case of all (a `www.` landing page).
    """
    host = (host or "").strip().lower().lstrip(".")
    if not host:
        return []
    out = [host]
    parts = host.split(".")
    if len(parts) > 2:
        parent = ".".join(parts[1:])
        if parent not in out:
            out.append(parent)
    return out


def host_counts(host: str, names: list[str] | None = None,
                available: dict[str, str] | None = None) -> dict[str, dict]:
    """Per profile: how many of a site's cookies it holds, and how many look like auth.

    Chrome keeps one jar per profile, and a site is routinely signed in in one profile
    and not the others. Nothing here decrypts values - it counts names - so this is the
    cheap answer to "which profile am I signed in to this site in?", and it neither
    touches the Keychain nor reads a single cookie's contents.

    This exists because an agent had to answer that question the expensive way: reading
    Chrome's cookie SQLite files by hand (and its History) to work out that a course site
    lived in Profile 3, then calling session_open(profiles=['Profile 3']). The information
    was always here; no tool was asking for it.
    """
    jars = available if available is not None else profiles()
    candidates = _host_candidates(host) or [host]
    out: dict[str, dict] = {}
    for name, db in jars.items():
        if names and name not in names:
            continue
        try:
            # Names only: this counts, and `authish` reads the name. Nothing here is
            # decrypted, which is what makes "which profile am I signed in under?" a
            # question worth asking on the way past a logged-out page.
            rows = _rows(db, candidates)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"cookies": 0, "auth_like": 0,
                         "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            continue
        # An expired session-looking name is evidence of a past login, not a profile the
        # agent should prefer now. Session cookies (`has_expires == 0`) remain candidates.
        chrome_now = (time.time() + CHROME_EPOCH_OFFSET) * 1_000_000
        seen = {(row[1], row[0], row[3]) for row in rows
                if row[0].lower().lstrip(".") in candidates
                and (not row[7] or float(row[8] or 0) > chrome_now)}
        out[name] = {"cookies": len(seen),
                     "auth_like": sum(1 for cookie_name, _host, _path in seen
                                      if authish(cookie_name))}
    return out


def load_many(names: list[str], host: str | None = None):
    """Merge cookies from several profiles.

    Returns (cookies, per-profile counts, per-profile errors). On a clash of
    (host, name, path) the later profile wins, so listing profiles in priority
    order does what you expect.
    """
    merged: dict[tuple[str, str, str], Cookie] = {}
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    available = profiles()
    for name in names:
        db = available.get(name)
        if not db:
            errors[name] = "no cookie database"
            continue
        try:
            jar = load(host, db)
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        counts[name] = len(jar)
        for c in jar:
            merged[(c.host, c.name, c.path)] = c
    return list(merged.values()), counts, errors


# -- a login is not storage ----------------------------------------------------
#
# Copying a *site's* cookies into another browser is ordinary, and it is what every
# "log me in on this device" flow does: they are long-lived, per-site, and a replayed
# one is nothing to write home about.
#
# A Google *account session* is not that. It is a live session whose freshness token
# (`__Secure-1PSIDTS`, `__Secure-1PSIDRTS`, `__Secure-1PSIDCC`, `SIDCC`) is re-issued
# to whichever client used it last. Hand that family to a second browser and the
# second browser retires the copy the first one is still holding; the first browser's
# next request then carries a token the service has already rotated away, and the
# session is ended - in the real Chrome, on a machine latchkey never wrote to. That
# is the whole of "latchkey keeps signing me out of Chrome": not damage to a profile
# (nothing in this package writes to one) but two clients holding one login.
#
# So: share storage, never share a live login. Site cookies travel. The account
# session stays where it was minted, unless the human says otherwise out loud with
# LATCHKEY_SHARE_LIVE_SESSION=1 - and the honest way to keep using Google *and* keep
# your Chrome signed in is not this switch at all, it is `mode="dedicated"` in
# session.py: run a second browser that signs in as you once and then owns its own
# session. Same account; not the same login.
LIVE_SESSION_HOSTS = ("google.com", "youtube.com", "googleusercontent.com", "withgoogle.com")
LIVE_SESSION_NAMES = frozenset({
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LSID", "OSID", "SIDCC",
    "__Secure-OSID", "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID", "__Host-GAPS", "ACCOUNT_CHOOSER",
})
# Names that rotate per use rather than per login: PSID "timestamp", "rotation
# timestamp" and "counter" variants. A copy of one of these is a copy of a value the
# service is about to consider spent, whatever it is called on the day.
LIVE_SESSION_SUFFIXES = ("PSIDTS", "PSIDRTS", "PSIDCC")
TRUTHY = ("1", "true", "yes", "on")


def share_live_session() -> bool:
    """Has the human said, out loud, to hand their account login to a second browser?"""
    return (os.environ.get("LATCHKEY_SHARE_LIVE_SESSION") or "").strip().lower() in TRUTHY


def is_live_session(cookie: "Cookie") -> bool:
    """Is this part of a live account login, rather than storage a browser can copy?"""
    return is_live_cookie(cookie.host, cookie.name)


def is_live_cookie(host: str | None, name: str | None) -> bool:
    """The same question for a cookie known only by its host and name - a saved CDP dict.

    The country sites count too: `.google.co.uk` carries the same account session as
    `.google.com`, and a rule that only knew the one spelling copied the other.
    """
    from . import google
    bare = str(host or "").lstrip(".").lower()
    if not (google.is_google_host(bare)
            or any(bare == h or bare.endswith("." + h) for h in LIVE_SESSION_HOSTS)):
        return False
    name = str(name or "")
    return name in LIVE_SESSION_NAMES or name.endswith(LIVE_SESSION_SUFFIXES)


def split_live_session(cookies: list["Cookie"], *, share: bool | None = None
                       ) -> tuple[list["Cookie"], list["Cookie"]]:
    """(kept, held back) - the cookies safe to carry, and the live login left alone.

    Held back cookies are reported, not silently forgotten: the session report names
    them, so "why is this browser logged out of Google" has an answer on the first line.
    """
    share = share_live_session() if share is None else share
    if share:
        return list(cookies), []
    kept: list[Cookie] = []
    held: list[Cookie] = []
    for cookie in cookies:
        (held if is_live_session(cookie) else kept).append(cookie)
    return kept, held


def _copy_db(db_path: str) -> tuple[str, str]:
    """Chrome holds the DB open, so read a copy. Returns (tmpdir, copy_path)."""
    tmp = tempfile.mkdtemp(prefix="latchkey-")
    dest = os.path.join(tmp, "Cookies")
    shutil.copy2(db_path, dest)
    for suffix in ("-journal", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.copy2(db_path + suffix, dest + suffix)
    return tmp, dest


COOKIE_COLUMNS = ("select host_key, name, encrypted_value, path, is_secure, is_httponly, "
                  "samesite, has_expires, expires_utc, top_frame_site_key from cookies")


def db_stamp(db_path: str) -> tuple:
    """What a cookie database looks like from outside: size and mtime, sidecars included.

    A cheap answer to "has anything changed?", which is the whole question the login
    handoff asks three times a second. Reading the store to find out costs a copy, a
    query and several thousand decryptions; `os.stat` costs nothing, and Chrome cannot
    write a cookie without touching one of these files.
    """
    stamp = []
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            info = os.stat(db_path + suffix)
        except OSError:
            continue
        stamp.append((suffix, info.st_mtime_ns, info.st_size))
    return tuple(stamp)


def _rows(db_path: str, hosts: list[str] | None = None) -> list[tuple]:
    """Raw rows from one profile's store: one copy, one query, however many hosts.

    Chrome holds the database open, so it is read from a copy. That copy used to be made
    once per *host candidate* - `host_counts` asks about a site and its parent domain, in
    every profile - which is the same file copied six times to answer one question.
    """
    tmp, copy = _copy_db(db_path)
    try:
        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        sql, params = COOKIE_COLUMNS, ()
        wanted = [h for h in (hosts or []) if h]
        if wanted:
            sql += " where " + " or ".join(["host_key like ?"] * len(wanted))
            params = tuple(f"%{h}%" for h in wanted)
        sql += " order by host_key, name"
        try:
            return list(con.execute(sql, params))
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _cookies(rows: list[tuple], dek: bytes) -> list[Cookie]:
    """Decrypted cookies from raw rows. The host goes into `decrypt`: it is what decides
    the layout exactly instead of by guesswork."""
    out = []
    for (host_key, name, blob, path, secure, http_only, samesite,
         has_expires, expires_utc, top_frame) in rows:
        expires = (expires_utc / 1_000_000 - CHROME_EPOCH_OFFSET) if has_expires else -1.0
        out.append(Cookie(
            host=host_key, name=name,
            value=decrypt(blob, dek, host_key).decode("utf-8", "replace"),
            path=path or "/", secure=bool(secure), http_only=bool(http_only),
            samesite=samesite, expires=expires, partitioned=bool(top_frame),
            partition_site=top_frame or None))
    return out


def load(host: str | None = None, db_path: str | None = None) -> list[Cookie]:
    """Return decrypted cookies, optionally only those whose host matches `host`.

    `host` is a substring match, so "github.com" picks up github.com and
    .github.com. Omit it to read the entire store — which is the default, and
    the right choice: a site's login often spans several hosts, and the filter
    silently drops the ones you forgot (chatgpt.com needs .openai.com and
    .auth.openai.com too, or Cloudflare ends up with no clearance).
    """
    db_path = db_path or profiles().get("Default")
    if not db_path or not os.path.isfile(db_path):
        raise FileNotFoundError(f"no Chrome cookie database at {db_path!r}")
    return _cookies(_rows(db_path, [host] if host else None), key())


def to_cdp(cookies: list[Cookie]) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Convert to DevTools Protocol CookieParam dicts: (plain, risky, dropped).

    CDP is used instead of Playwright's add_cookies because only CDP can express
    a partition key. Without it, Cloudflare's cf_clearance has to be dropped and
    the site challenges you as a stranger.
    """
    kept: list[dict] = []
    risky: list[dict] = []
    dropped = {"non_ascii": 0, "unpartitionable": 0}
    for c in cookies:
        # A value with a byte outside printable ASCII is not something CDP promises to
        # take, and one bad cookie fails the whole batch. So it is set apart rather than
        # thrown away: the batch stays fast, and these are then offered one at a time.
        odd = not all(32 <= ord(ch) < 127 for ch in c.value)
        entry: dict = {"name": c.name, "value": c.value, "path": c.path,
                       "secure": c.secure, "httpOnly": c.http_only}
        if c.partitioned:
            if not c.partition_site:
                # A partitioned cookie without its partition key cannot be set
                # correctly, and guessing the key makes Chrome reject it.
                dropped["unpartitionable"] += 1
                continue
            # Chrome rejects a partition key given as just {topLevelSite} with an
            # unhelpful "Invalid parameters". hasCrossSiteAncestor is required
            # alongside it - without it, all 293 partitioned cookies fail to set.
            entry["partitionKey"] = {"topLevelSite": c.partition_site,
                                    "hasCrossSiteAncestor": False}
        if c.is_host_only:
            entry["url"] = f"https://{c.site}{c.path}"
        else:
            entry["domain"] = c.host
        if c.expires > 0:
            entry["expires"] = c.expires
        same = SAMESITE.get(c.samesite)
        if same:
            entry["sameSite"] = same
        (risky if odd else kept).append(entry)
    dropped["non_ascii"] = len(risky)
    return kept, risky, dropped


def redact(value: str, keep: int = 4) -> str:
    """Mask a secret for display: first/last few chars only."""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]} ({len(value)} chars)"


def authish(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in
               ("session", "token", "auth", "jwt", "sid", "sso", "login", "credential"))
