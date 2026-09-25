"""Which hosts are Google's, and what a Google sign-in page looks like.

Google is the one site family latchkey cannot copy. The account session carries a
freshness token that Google re-issues to whichever client used it last, and Chrome keeps a
*device bound* session for it whose key never leaves the profile that registered it - so a
second browser holding a copy of the login signs the user out of both (see `cookies.py`
and the README). The working answer is a browser that signs in once on a profile of its
own: `mode="dedicated"`, signed into with `latchkey login` - one profile per account, so a
second Google account is a second profile rather than a second login in the first one's.

That makes "is this Google?" a routing question, asked on every navigation of a session
whose mode nobody chose, so it lives here in one place and is answered precisely: the
hosts whose pages use the Google *account* session, not every domain Google happens to own.

  google.com and every subdomain     accounts, mail, docs, drive, gemini, myaccount ...
  google.<country>                   google.co.uk, google.de, google.com.au
  gmail.com                          redirects into mail.google.com
  youtube.com, youtu.be              the account session is carried on .youtube.com too
  googleusercontent.com              Docs and Drive content behind the signed-in user

Not included, on purpose: googleapis.com and gstatic.com (endpoints and static files, no
page to sign in to), and most third-party sites that merely offer "Sign in with Google" -
their own cookies copy fine, and only the hop through accounts.google.com is Google's.
Known portals whose primary login is Google SSO are a separate, narrow routing list: they
must start in the clone because a click-driven redirect cannot change browser modes midway.

Nothing in this module touches a browser, a profile or a secret: it reads host names, URL
paths, page text and cookie *names*.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import urlparse

GOOGLE_SITES = ("google.com", "gmail.com", "youtube.com", "youtu.be",
                "googleusercontent.com")
# These are not Google hosts and must not inherit Google's cookie, verdict, or wait semantics.
# They only need the same *browser mode* because their normal login immediately redirects to
# Google. Keep this exact and boundary-checked: cloning a full profile for arbitrary hosts is
# a larger privacy surface than injecting that site's cookies.
# Third-party portals that begin by redirecting to Google need a Google-capable browser before
# navigation begins. They are configured by the user, rather than built into this public module.
GOOGLE_SSO_ACCOUNTS_ENV = "LATCHKEY_GOOGLE_SSO_ACCOUNTS"
# google.de, google.co.uk, google.com.au - the country sites share the account session.
_COUNTRY_SITE = re.compile(r"(?:^|\.)google\.(?:[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$")

ACCOUNTS_HOSTS = ("accounts.google.com", "accounts.youtube.com")

# The paths of the sign-in flow itself. `/signin/oauth` is left out deliberately: that is
# the "choose an account to continue to <app>" screen, which a signed-in user sees too.
_SIGNIN_PATH = re.compile(
    r"^/(?:v3/)?signin/(?!oauth)|^/ServiceLogin|^/InteractiveLogin|^/AccountChooser"
    r"|^/AddSession|^/speedbump/|^/(?:v3/)?signin$", re.IGNORECASE)

# Cookie names that exist only while an account is signed in. NID, AEC and friends are set
# for anonymous visitors too, so they are not evidence of anything.
SESSION_COOKIES = frozenset({"SID", "__Secure-1PSID", "__Secure-3PSID"})
SESSION_COOKIE_HOSTS = (".google.com", "google.com", "accounts.google.com")

# What a sign-in step says about itself, for sites that are not Google's too. Each phrase
# is one a *signed-in* page has no reason to show in its first screenful - and they are
# only consulted on a short page, because a long signed-in page can quote anything.
SIGNIN_PHRASES = (
    ("enter your password", "password page"),
    ("use your passkey", "passkey prompt"),
    ("sign in with a passkey", "passkey prompt"),
    ("sign in with your passkey", "passkey prompt"),
    ("choose how you want to sign in", "sign-in method choice"),
    ("verify it's you", "verification step"),
    ("verify it’s you", "verification step"),
)
SHORT_PAGE = 2500          # a sign-in step fits in this; a signed-in page rarely does


def host_of(url_or_host: str | None) -> str:
    """The bare, lower-case host of a URL or a host, without port or credentials."""
    text = (url_or_host or "").strip()
    if "://" in text:
        text = urlparse(text).netloc
    return text.split("@")[-1].split(":")[0].strip(".").lower()


def is_google_host(url_or_host: str | None) -> bool:
    """Does this host's page use the Google account session? Host boundaries, not substrings."""
    host = host_of(url_or_host)
    if not host:
        return False
    if any(host == site or host.endswith("." + site) for site in GOOGLE_SITES):
        return True
    return bool(_COUNTRY_SITE.search(host))


def is_google_sso_clone_host(url_or_host: str | None) -> bool:
    """Does a configured third-party portal need a Google-capable browser first?

    ``LATCHKEY_GOOGLE_SSO_ACCOUNTS`` is a JSON object mapping bare portal hosts to the
    dedicated account name that should be used for their Google SSO. For example,
    ``{"portal.example.edu": "school"}`` routes the portal and its subdomains to the
    ``school`` account. It defaults to an empty mapping. Invalid configuration is ignored so
    it cannot accidentally broaden Google-session routing.
    """
    host = host_of(url_or_host)
    return any(host == site or host.endswith("." + site)
               for site in google_sso_accounts())


def google_sso_accounts() -> dict[str, str]:
    """Return the valid host-to-account Google SSO mapping from the environment.

    Set ``LATCHKEY_GOOGLE_SSO_ACCOUNTS`` to a JSON object such as
    ``{"portal.example.edu": "school", "login.example.org": "work"}``. Keys must be
    bare host names and values must be non-empty account names. Host matching remains at a
    label boundary, so a mapping for ``example.edu`` includes ``portal.example.edu`` but not
    ``example.edu.attacker.test``.
    """
    raw = (os.environ.get(GOOGLE_SSO_ACCOUNTS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(configured, dict):
        return {}
    accounts: dict[str, str] = {}
    for site, account in configured.items():
        if not isinstance(site, str) or not isinstance(account, str):
            continue
        normalized = host_of(site)
        if normalized != site.strip().strip(".").lower() or not normalized or not account.strip():
            continue
        accounts[normalized] = account.strip()
    return accounts


def uses_google_session(url_or_host: str | None) -> bool:
    """Whether successful navigation needs the profile-bound Google account session."""
    return is_google_host(url_or_host) or is_google_sso_clone_host(url_or_host)


def account_for(url_or_host: str | None) -> str | None:
    """The dedicated account a known portal should use, or None for the default login.

    Host-boundary matched, so a subdomain of a mapped site inherits it and an unrelated host
    that merely ends in the same string does not. Only consulted for `dedicated` routing.
    """
    host = host_of(url_or_host)
    for site, account in google_sso_accounts().items():
        if host == site or host.endswith("." + site):
            return account
    return None


def google_mode() -> str:
    """The mode a Google host uses when nobody chose one: latchkey's own profile.

    This used to be `clone` - a copy of the real profile, run on the real macOS keychain,
    which can sign the device-bound challenge because the key lives in the machine's
    keystore rather than the profile directory. It does work, and on a good day the copy
    rotates its own `__Secure-1PSIDTS` without disturbing the original. What it cannot do
    is be *reliable*, and the two failures are the ones a person actually feels:

      - A copy that sits unused goes stale. The freshness token is re-issued to whichever
        client presented it last, so a clone taken on Monday and opened on Friday arrives
        holding a value Google retired days ago, and reads as signed out. Re-seeding fixes
        that run and sets up the next one.
      - Two browsers holding one account session is precisely the shape of a replayed
        session. When Google reads it that way it does not end the copy's session, it ends
        the *account's* - which is a sign-out in the real Chrome, on a profile latchkey
        never wrote to. That is the failure worth designing against, because the cost of
        it lands on the person, not the agent.

    `dedicated` has neither failure, and the reason is that nothing is shared. The profile
    signs in once, by hand, in an ordinary Chrome window; it registers a device-bound key
    of *its own*; and from then on it renews its own session, in its own directory, the
    way a second phone signed into the same account does. There is no copy to go stale and
    no second holder of one session to be mistaken for a replay. The cost is one sign-in
    per account, once - which is the trade this default now makes on purpose.

    `LATCHKEY_GOOGLE_MODE` still overrides it, `clone` included, for anyone who would
    rather carry their Chrome's login than sign in once.
    """
    choice = (os.environ.get("LATCHKEY_GOOGLE_MODE") or "").strip().lower()
    return choice if choice in ("clone", "dedicated", "inject", "real") else "dedicated"


def mode_for(url_or_host: str | None) -> str:
    """The mode a session that did not choose one should use for this site."""
    return google_mode() if uses_google_session(url_or_host) else "inject"


def signin_step(url: str | None) -> str | None:
    """Name the Google sign-in step a URL is, or None when it is not one."""
    parsed = urlparse(url or "")
    if host_of(parsed.netloc) not in ACCOUNTS_HOSTS:
        return None
    path = parsed.path or "/"
    if not _SIGNIN_PATH.search(path):
        return None
    low = path.lower()
    if "challenge/pwd" in low:
        return "Google password page"
    if "challenge/pk" in low or "passkey" in low:
        return "Google passkey prompt"
    if "accountchooser" in low:
        return "Google account chooser"
    if "identifier" in low:
        return "Google email page"
    if "challenge" in low or "speedbump" in low:
        return "Google verification step"
    return "Google sign-in page"


def signin_page(url: str | None, title: str = "", body: str = "") -> str | None:
    """Why this page is a step of signing in, or None.

    The URL decides for Google's own flow. For everything else the words do, and only on a
    short page: the account chooser that lists accounts as "Signed out", a password or
    passkey prompt, "verify it's you". Every one of those was reported `logged-in` on
    11 September, because an element whose label merely contained "account" counted as an
    avatar - and the tool text then told the model to proceed, so it asked the user to type
    their password into the chat.
    """
    step = signin_step(url)
    if step:
        return step
    text = f"{title}\n{body}"
    if len(body or "") > SHORT_PAGE:
        return None
    low = text.lower().replace("’", "'")
    if "choose an account" in low and "signed out" in low:
        return "account chooser, accounts signed out"
    for phrase, reason in SIGNIN_PHRASES:
        if phrase.replace("’", "'") in low:
            return reason
    return None


# -- the account session on disk, by name only --------------------------------

def session_cookie_names(cookie_db: str) -> set[str]:
    """Google account-session cookie names present and unexpired in one Cookies database.

    Names and expiry only: nothing is decrypted, so this needs no Keychain and touches no
    secret. Chrome holds the database open, so a copy is read; an unreadable one is simply
    "nothing found", because a sign-in that has not been written yet looks the same.
    """
    if not cookie_db or not os.path.isfile(cookie_db):
        return set()
    tmp = tempfile.mkdtemp(prefix="latchkey-google-")
    try:
        copy = os.path.join(tmp, "Cookies")
        shutil.copy2(cookie_db, copy)
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.exists(cookie_db + suffix):
                shutil.copy2(cookie_db + suffix, copy + suffix)
        con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            marks = ",".join("?" * len(SESSION_COOKIE_HOSTS))
            rows = list(con.execute(
                f"select name, has_expires, expires_utc from cookies where host_key in ({marks})",
                SESSION_COOKIE_HOSTS))
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return set()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    now = time.time()
    found = set()
    for name, has_expires, expires_utc in rows:
        if name not in SESSION_COOKIES:
            continue
        if has_expires and expires_utc and expires_utc / 1_000_000 - 11_644_473_600 < now:
            continue
        found.add(name)
    return found


def signed_in(cookie_db: str) -> bool:
    """Is a Google account signed in, in the profile this Cookies database belongs to?"""
    return bool(session_cookie_names(cookie_db))


# -- the persistent Google clone ----------------------------------------------

def google_clone_dir() -> str:
    """Where this server's Google clone lives - under its own owner id.

    It is its own directory, apart from the throwaway clone other clone sessions use, and
    apart from every *other* latchkey server's Google clone: a shared path is a single Chrome
    window that two models take turns killing each other to hold. The contents are re-seeded
    from the real profile on every open, never reused: a clone rotates its own copy of the
    account session the moment it is used, so a clone kept from last time holds a token the
    real Chrome has moved past, and a device-bound session revokes exactly that.
    `LATCHKEY_GOOGLE_CLONE_DIR` pins one path instead (shared on purpose, and then a second
    server gets a ProfileBusy naming the owner rather than a stolen window); `LATCHKEY_HOME`
    moves the whole latchkey directory.
    """
    from . import paths
    explicit = (os.environ.get("LATCHKEY_GOOGLE_CLONE_DIR") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    base = os.path.expanduser(os.environ.get("LATCHKEY_HOME") or "~/.latchkey")
    return os.path.join(base, "clones", paths.owner_id(), "google-clone")


# The short-lived, device-bound rotating cookies: the "timestamp" and "rotation timestamp"
# halves of the PSID family. Their value is what Google re-issues on every rotation, and a
# copy of one is a copy of a value the service may already have moved past.
ROTATING_SESSION_COOKIES = ("__Secure-1PSIDTS", "__Secure-3PSIDTS",
                            "__Secure-1PSIDRTS", "__Secure-3PSIDRTS")


def strip_rotating_session(clone_dir: str) -> int:
    """Drop the rotating device-bound cookies from a fresh clone, so it mints its own.

    A clone copies the real profile's *current* rotating token. If the real Chrome has since
    moved past it - which happens the moment any other client rotates the shared session -
    that copy is superseded, and a device-bound session revokes a superseded token on sight:
    the clone reads signed out even though the account is signed in. The anchor cookies (SID,
    __Secure-1PSID, __Secure-3PSID) and the session's registration are enough on their own:
    with the rotating cookies absent, Chrome's first authenticated request finds the bound
    cookie missing, runs the DBSC refresh once (signing the challenge with the key this machine
    holds), and is issued a *fresh* token it minted itself rather than one copied and possibly
    stale. So the clone never presents a superseded value, and opens signed in every time.

    Returns how many rows were removed. Called only on a freshly seeded Google clone, before
    Chrome opens it; a missing or unreadable store is simply "nothing removed".
    """
    db = os.path.join(clone_dir, "Default", "Cookies")
    if not os.path.isfile(db):
        return 0
    try:
        con = sqlite3.connect(db)
        try:
            marks = ",".join("?" * len(ROTATING_SESSION_COOKIES))
            removed = con.execute(
                f"delete from cookies where name in ({marks}) and host_key like '%google.com'",
                ROTATING_SESSION_COOKIES).rowcount
            con.commit()
            return int(removed or 0)
        finally:
            con.close()
    except sqlite3.Error:
        return 0
