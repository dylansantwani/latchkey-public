"""Boot Chrome on a copy-on-write clone of your real profile.

This is the universal route, and it is different in kind from cookie injection.
Instead of transferring storage piece by piece, Chrome opens a clone of the
actual profile directory, so everything is simply *there* and correctly scoped,
because Chrome itself put it there:

  cookies (partitions intact)   localStorage   IndexedDB   service workers
  session storage               cache          history     saved logins

Cookie injection still has its place - it is fast, it never touches your real
profile, and it works while Chrome is open. Cloning is what covers the storage
that cannot be reconstructed, IndexedDB above all.

Two things this has to get right, both learned the hard way:

1. Use an APFS clone (`cp -cR`), not rsync. rsync silently dropped
   Default/Cookies on a live profile, so Chrome started with a fresh empty
   database and every site looked logged out. A clone of 3.4GB takes ~4s and
   costs almost no disk, because the blocks are shared copy-on-write.
2. Delete the inherited SingletonLock. It is a symlink to `hostname-pid`, and
   Chrome refuses to start while it exists ("Failed to create a ProcessSingleton").
"""
from __future__ import annotations

import functools
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import Callable

from . import paths
from .cookies import CHROME_ROOT

CLONE_DIR = os.environ.get("LATCHKEY_CLONE_DIR") or paths.clones_dir("throwaway-clone")
# Per-owner, because Chrome allows one browser per profile directory: a machine-wide path
# here is one window that two servers both want, and the loser's page dies mid-task when the
# winner clears the lock. LATCHKEY_CLONE_DIR still pins one path for callers who want to
# share deliberately (and who then get a ProfileBusy naming the owner, not a kill).
SINGLETONS = ("SingletonLock", "SingletonCookie", "SingletonSocket")
# Directories that are pure cache: cloning them wastes time for no benefit.
SKIP_ON_FALLBACK = ("Default/Cache", "Default/Code Cache", "Default/GPUCache",
                    "Default/DawnCache", "Default/DawnGraphiteCache",
                    "Default/Service Worker/CacheStorage", "component_crx_cache",
                    "extensions_crx_cache")


DEDICATED_DIR = paths.path("profile")
DEFAULT_ACCOUNT = "default"
# An account name is a directory name, so it is validated rather than trusted. See
# `normalize_account`.
_ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


DBSC_FILE = "Device Bound Sessions"


def bound_sessions(root: str | None = None) -> dict[str, list[str]]:
    """Which profiles hold a *device bound session*, and for which origin. No keys, no secrets.

    Chrome registers one of these when an origin asks for it, and Google does: the
    session's cookies are thereafter refreshed by signing a challenge with a private key
    kept in the OS keystore, one per profile, which by design is *not* in the profile
    directory. That is why a copied session cannot be honoured - and why copying one is
    not merely useless but dangerous: the origin sees a refresh it cannot verify and
    treats the session as stolen, which signs the user out of the browser it came from.

    Reading these registrations is how a copy that cannot work is told apart from a copy
    that can. A locked file (Chrome is running) is copied first, and an unreadable one is
    simply not evidence.
    """
    root = root or CHROME_ROOT
    found: dict[str, list[str]] = {}
    for profile_dir in sorted(glob.glob(os.path.join(root, "*"))):
        state = os.path.join(profile_dir, DBSC_FILE)
        if not os.path.isfile(state):
            continue
        keys: list[str] = []
        tmp = tempfile.mkdtemp(prefix="latchkey-dbsc-")
        try:
            copy = os.path.join(tmp, "bound")
            shutil.copy2(state, copy)          # Chrome holds it open; read a copy
            for suffix in ("-journal", "-wal", "-shm"):
                # A registration Chrome has not checkpointed yet lives only in the
                # sidecar, and a copy without it reads as "nothing is bound" - which is
                # the one wrong answer this whole check exists to avoid.
                if os.path.exists(state + suffix):
                    shutil.copy2(state + suffix, copy + suffix)
            con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            try:
                keys = [row[0] for row in con.execute("select key from dbsc_session_tbl")]
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            keys = []                          # not evidence of a binding either way
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if keys:
            found[os.path.basename(profile_dir)] = [k for k in keys if k]
    return found


def _dbsc_stamp(root: str) -> tuple:
    """What the registrations look like from outside: their paths and mtimes.

    The cache key. Reading the registrations means copying and opening several sqlite
    files, and `bound_hosts` is now asked on every navigation rather than once per
    session - so the answer is kept until one of those files changes, which is exactly
    when a binding can appear or go away.
    """
    stamp = []
    for path in sorted(glob.glob(os.path.join(root, "*", DBSC_FILE))):
        try:
            info = os.stat(path)
        except OSError:
            continue
        stamp.append((path, info.st_mtime_ns, info.st_size))
    return tuple(stamp)


@functools.lru_cache(maxsize=8)
def _bound_hosts_at(root: str, _stamp: tuple) -> frozenset[str]:
    hosts: set[str] = set()
    for keys in bound_sessions(root).values():
        for key in keys:
            host = str(key).split("://", 1)[-1].split("/", 1)[0].strip().lower()
            if host:
                hosts.add(host)
    return frozenset(hosts)


def bound_hosts(root: str | None = None) -> set[str]:
    """The origins with a device bound session, as bare hosts: {"google.com"}."""
    root = root or CHROME_ROOT
    return set(_bound_hosts_at(root, _dbsc_stamp(root)))


def is_bound(host: str, roots: tuple = ()) -> str | None:
    """The registered origin that covers `host`, or None. Subdomains count."""
    wanted = (host or "").lstrip(".").lower().split(":")[0]
    if not wanted:
        return None
    for root in (roots or (CHROME_ROOT,)):
        for bound in bound_hosts(root):
            if wanted == bound or wanted.endswith("." + bound):
                return bound
    return None


def chrome_binary() -> str:
    """The Chrome you actually use, for the one mode that drives it instead of a copy.

    `channel="chrome"` finds this for Playwright; real mode has to name it itself,
    because it starts the browser rather than asking Playwright to.
    """
    candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                  shutil.which("google-chrome") or "", shutil.which("google-chrome-stable") or ""]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    raise FileNotFoundError(
        "could not find Google Chrome to drive; real mode needs the browser you use "
        "installed (or start it yourself and set LATCHKEY_CDP to its debugging endpoint).")


def normalize_account(name: str | None) -> str:
    """The canonical form of an account name, or raise if it could not be one.

    An account name becomes a directory under `~/.latchkey/accounts`, so it is checked
    rather than trusted: names are lower-case, and only letters, digits, `-` and `_` are
    allowed. That rules out `..`, a leading dot and a path separator - a name is a name,
    never a route out of latchkey's own directory.
    """
    text = (name or "").strip().lower()
    if not text:
        return DEFAULT_ACCOUNT
    if not _ACCOUNT_NAME.fullmatch(text):
        raise ValueError(
            f"{name!r} is not a usable account name: use letters, digits, '-' and '_' "
            f"(for example 'school' or 'work-gmail').")
    return text


def accounts_root() -> str:
    """Where the named accounts' profiles live. From the environment, so a test can move it."""
    home = os.path.expanduser(os.environ.get("LATCHKEY_HOME") or "~/.latchkey")
    return os.path.join(home, "accounts")


def account_path(name: str | None) -> str:
    """The directory a named account's profile uses, created or not.

    `default` is deliberately the directory latchkey has always used, `~/.latchkey/profile`,
    and not `~/.latchkey/accounts/default`. That directory may hold a login somebody signed
    in by hand months ago; moving it to make the layout tidier is how a rename loses a
    session. Every other name is a directory of its own under `profiles/`.
    """
    name = normalize_account(name)
    if name == DEFAULT_ACCOUNT:
        home = os.path.expanduser(os.environ.get("LATCHKEY_HOME") or "~/.latchkey")
        return os.path.join(home, "profile")
    return os.path.join(accounts_root(), name)


def account_names() -> list[str]:
    """Every account latchkey has a profile directory for, `default` first when it exists."""
    names = []
    if os.path.isdir(account_path(DEFAULT_ACCOUNT)):
        names.append(DEFAULT_ACCOUNT)
    root = accounts_root()
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        entries = []
    for entry in entries:
        if entry == DEFAULT_ACCOUNT or not os.path.isdir(os.path.join(root, entry)):
            continue
        try:
            names.append(normalize_account(entry))
        except ValueError:
            continue        # something else somebody put there; not ours to report
    return names


def account_of_path(path: str | None) -> str | None:
    """The account whose profile is at this path, when it is one of latchkey's own."""
    if not path:
        return None
    target = os.path.abspath(os.path.expanduser(path))
    for name in [DEFAULT_ACCOUNT] + account_names():
        if os.path.abspath(account_path(name)) == target:
            return name
    return None


def dedicated_dir(explicit: str | None = None, account: str | None = None) -> str:
    """latchkey's own long-lived profile: its own login, seeded from nothing.

    Not a clone of yours and not a child of yours - this directory is the browser's
    own identity, and the point of it is that it is the *only* place its session
    lives. Never point it at the real profile: Chrome would be handed a directory it
    is already running on, which is the one way to corrupt the thing we spend this
    whole module avoiding writing to.

    `account` picks one of several: latchkey keeps a profile per account, so a second
    Google account is a second directory with its own sign-in, its own cookies and its
    own device-bound key, never a second login inside the first one's.
    """
    path = dedicated_path(explicit, account)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def dedicated_path(explicit: str | None = None, account: str | None = None) -> str:
    """Where latchkey's own profile is, checked but not created - for asking about it.

    An explicit directory wins, then an account name, then `LATCHKEY_PROFILE_DIR`, then
    the default account. A *name* beats the environment variable on purpose: asking for
    the `school` account is a more specific request than a variable set for the process.
    """
    # The home is read here rather than taken from the constant, so a caller (or a test) that
    # points LATCHKEY_HOME elsewhere cannot end up opening a Chrome on the *real*
    # ~/.latchkey/profile - a lock, a window and a login that belong to somebody's session.
    if explicit:
        path = explicit
    elif account:
        path = account_path(account)
    else:
        path = os.environ.get("LATCHKEY_PROFILE_DIR") or account_path(DEFAULT_ACCOUNT)
    path = os.path.abspath(os.path.expanduser(path))
    root = os.path.abspath(CHROME_ROOT)
    if path == root or path.startswith(root + os.sep):
        raise ValueError(
            f"refusing to use {path!r} as latchkey's own profile: that is inside your "
            f"real Chrome profile ({root}). Dedicated mode needs a directory of its "
            f"own, e.g. LATCHKEY_PROFILE_DIR=~/.latchkey/profile.")
    return path


def signed_in_accounts(root: str | None = None) -> list[str]:
    """The accounts a Chrome profile has signed in, by email. No tokens, no secrets.

    Clone mode hands Chrome the whole profile, and a signed-in profile brings the
    account bookkeeping and any cached sync credentials with it. A second client
    presenting the account's refresh token is a *reuse*, which is the one failure mode
    that ends the session on every device at once, so it is worth naming before the
    clone is launched rather than after.
    """
    root = root or CHROME_ROOT
    try:
        with open(os.path.join(root, "Local State"), encoding="utf-8", errors="replace") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return []
    cache = (state.get("profile") or {}).get("info_cache") or {}
    accounts = {entry.get("user_name", "") for entry in cache.values()
                if isinstance(entry, dict) and entry.get("gaia_id") and entry.get("user_name")}
    return sorted(accounts)


def _remove_singletons(root: str) -> list[str]:
    removed = []
    for name in SINGLETONS:
        path = os.path.join(root, name)
        if os.path.islink(path) or os.path.exists(path):
            try:
                os.remove(path)
                removed.append(name)
            except OSError:
                pass
    return removed


def _cookie_rows(path: str) -> int:
    if not os.path.isfile(path):
        return -1
    tmp = path + ".probe"
    try:
        shutil.copy2(path, tmp)
        for suffix in ("-journal", "-wal"):
            if os.path.exists(path + suffix):
                shutil.copy2(path + suffix, tmp + suffix)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            return con.execute("select count(*) from cookies").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return -1
    finally:
        for suffix in ("", "-journal", "-wal"):
            if os.path.exists(tmp + suffix):
                os.remove(tmp + suffix)


def source_is_newer(dest: str = CLONE_DIR, source: str = CHROME_ROOT) -> bool:
    """True when the real profile has changed since the clone was taken."""
    dest_db = os.path.join(dest, "Default", "Cookies")
    src_db = os.path.join(source, "Default", "Cookies")
    if not os.path.exists(dest_db):
        return True
    return os.path.getmtime(src_db) > os.path.getmtime(dest_db) + 1


def clone(source: str = CHROME_ROOT, dest: str = CLONE_DIR, *, force: bool = False,
          verify_cookies: bool = True, reuse_ok: "Callable[[str], bool] | None" = None) -> dict:
    """Clone-on-write the profile, unless a fresh enough clone already exists.

    Returns a small report: path, seconds, size, whether it was reused, the
    cookie row count in the clone, and which singletons were removed.

    `reuse_ok(dest)` overrides the freshness test: when it returns True an existing clone is
    kept whatever the source's mtime says. A persistent Google clone uses this - it renews its
    own copy of the account session, so it must not be discarded and re-seeded every time the
    real Chrome updates its own cookies (which is constant); it is kept while it still reads as
    signed in, and re-seeded only when it does not.
    """
    # A clone belongs to the server that made it (two models must never want one window), so
    # a server that died leaves a whole browser profile behind: clear the dead ones first.
    paths.prune_clones()
    if os.path.isdir(dest) and not force:
        keep = reuse_ok(dest) if reuse_ok is not None else not source_is_newer(dest, source)
        if keep:
            rows = _cookie_rows(os.path.join(dest, "Default", "Cookies"))
            if rows > 0 or not verify_cookies:
                return {"path": dest, "reused": True, "rows": rows,
                        "removed_singletons": _remove_singletons(dest)}

    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    parent = os.path.dirname(dest.rstrip("/")) or "/"
    os.makedirs(parent, exist_ok=True)

    started = time.time()
    method = "apfs-clone"
    result = subprocess.run(["cp", "-cR", source, dest], capture_output=True, text=True)
    if result.returncode != 0:
        method = "copy"
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        result = subprocess.run(["cp", "-R", source, dest], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"profile clone failed: {result.stderr.strip()[:200]}")

    removed = _remove_singletons(dest)
    rows = _cookie_rows(os.path.join(dest, "Default", "Cookies"))
    if verify_cookies and rows <= 0:
        raise RuntimeError(
            f"cloned profile has {rows} cookies at {dest} - the copy is not usable. "
            f"Try again, or set LATCHKEY_CLONE_DIR to a path on the same volume as "
            f"{source}.")

    size = subprocess.run(["du", "-sh", dest], capture_output=True,
                          text=True).stdout.split("\t")[0]
    return {"path": dest, "reused": False, "method": method, "rows": rows,
            "removed_singletons": removed, "seconds": round(time.time() - started, 1),
            "size": size}


def describe() -> dict:
    """What is in the clone right now, without launching anything."""
    if not os.path.isdir(CLONE_DIR):
        return {"path": CLONE_DIR, "exists": False}
    out = {"path": CLONE_DIR, "exists": True, "profiles": {}}
    for entry in sorted(os.listdir(CLONE_DIR)):
        db = os.path.join(CLONE_DIR, entry, "Cookies")
        if os.path.isfile(db):
            out["profiles"][entry] = _cookie_rows(db)
    idb = os.path.join(CLONE_DIR, "Default", "IndexedDB")
    out["indexeddb_origins"] = len(os.listdir(idb)) if os.path.isdir(idb) else 0
    ls = os.path.join(CLONE_DIR, "Default", "Local Storage", "leveldb")
    out["local_storage_files"] = len(os.listdir(ls)) if os.path.isdir(ls) else 0
    out["stale"] = source_is_newer()
    return out


def discard(dest: str = CLONE_DIR) -> bool:
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
        return True
    return False
