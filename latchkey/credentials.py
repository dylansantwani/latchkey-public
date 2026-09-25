"""Where a username and password come from.

Searched in order, first hit wins:

  1. explicit arguments              login(url, username=..., password=...)
  2. the vault                       ~/.latchkey/credentials.json (mode 0600)
  3. environment                     LATCHKEY_<SITE>_USERNAME / _PASSWORD
  4. macOS Keychain                  an "internet password" for the site
  5. Chrome's saved logins           Login Data (empty on this machine)

Nothing here ever prints a password. `resolve()` returns the secret; callers are
responsible for not logging it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass

from . import paths
from .cookies import CHROME_ROOT, decrypt, key, profiles

VAULT = paths.path("credentials.json")


@dataclass
class Credential:
    site: str
    username: str
    password: str
    source: str

    def __repr__(self) -> str:      # never leak the password into logs
        return f"Credential(site={self.site!r}, username={self.username!r}, source={self.source!r})"


def _slug(site: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", site).strip("_").upper()


# -- sources -----------------------------------------------------------------

def from_vault(site: str) -> tuple[str, str] | None:
    if not os.path.isfile(VAULT):
        return None
    try:
        with open(VAULT, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    bare = site.split(":")[0].removeprefix("www.")
    for key_ in (site, bare, f"www.{bare}"):
        entry = data.get(key_)
        if isinstance(entry, dict) and entry.get("username") and entry.get("password"):
            return entry["username"], entry["password"]
    return None


def from_env(site: str) -> tuple[str, str] | None:
    slug = _slug(site.split(":")[0].removeprefix("www."))
    user = os.environ.get(f"LATCHKEY_{slug}_USERNAME")
    secret = os.environ.get(f"LATCHKEY_{slug}_PASSWORD")
    if user and secret:
        return user, secret
    return None


def from_keychain(site: str, account: str | None = None) -> tuple[str, str] | None:
    """A macOS 'internet password' entry for the site."""
    host = site.split(":")[0].removeprefix("www.")
    cmd = ["security", "find-internet-password", "-s", host, "-w"]
    if account:
        cmd += ["-a", account]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        return None
    secret = result.stdout.rstrip(b"\n").decode("utf-8", "replace")
    if not secret:
        return None
    if not account:
        meta = subprocess.run(["security", "find-internet-password", "-s", host],
                              capture_output=True, text=True).stdout
        match = re.search(r'"acct"<blob>="([^"]*)"', meta)
        account = match.group(1) if match else ""
    return account, secret


def chrome_saved_logins(db_path: str | None = None) -> list[tuple[str, str, str]]:
    """(origin, username, password) from Chrome's Login Data. Often empty."""
    path = db_path or os.path.join(profiles().get("Default", ""), "Login Data")
    if not path or not os.path.isfile(path):
        return []
    tmp = tempfile.mkdtemp(prefix="latchkey-logins-")
    try:
        dest = os.path.join(tmp, "Login Data")
        shutil.copy2(path, dest)
        con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        dek = key()
        out = []
        for origin, user, blob in con.execute(
                "select origin_url, username_value, password_value from logins"):
            if not blob:
                continue
            secret = decrypt(blob, dek).decode("utf-8", "replace")
            if secret and all(32 <= ord(ch) < 127 for ch in secret):
                out.append((origin, user, secret))
        return out
    except (sqlite3.Error, OSError):
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- resolution --------------------------------------------------------------

def resolve(site: str, username: str | None = None,
            password: str | None = None) -> Credential | None:
    """Find a credential for `site`, or None. First source to yield both wins."""
    if username and password:
        return Credential(site, username, password, "explicit")

    found = from_vault(site)
    if found:
        return Credential(site, *found, "vault")

    found = from_env(site)
    if found:
        return Credential(site, *found, "env")

    found = from_keychain(site, account=username)
    if found:
        return Credential(site, *found, "keychain")

    host = site.split(":")[0].removeprefix("www.")
    for origin, user, secret in chrome_saved_logins():
        if host in origin:
            return Credential(site, user, secret, "chrome")
    return None


def sources_available(site: str) -> list[str]:
    """Which sources could serve this site, for diagnostics. No secrets."""
    out = []
    if from_vault(site):
        out.append("vault")
    if from_env(site):
        out.append("env")
    if from_keychain(site):
        out.append("keychain")
    if any(site.split(":")[0].removeprefix("www.") in o for o, _, _ in chrome_saved_logins()):
        out.append("chrome")
    return out


def put_in_vault(site: str, username: str, password: str) -> str:
    """Store a credential for later runs. File is mode 0600."""
    os.makedirs(os.path.dirname(VAULT), exist_ok=True)
    existing = {}
    if os.path.isfile(VAULT):
        try:
            with open(VAULT, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing[site.split(":")[0]] = {"username": username, "password": password}
    with open(VAULT, "w", encoding="utf-8") as fh:
        os.chmod(VAULT, 0o600)
        json.dump(existing, fh, indent=2)
    return VAULT
