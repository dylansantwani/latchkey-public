"""Where latchkey keeps its own things, and the one-time move from its old name.

Everything the package writes lives under `~/.latchkey`: saved sessions, the dedicated
profile (which holds a real login of yours), the optional credential vault, the site
hints file, and the note each viewer leaves saying which port it is on.

The package used to be called abrowser, so it used to write to `~/.abrowser`. A rename
that quietly abandons a directory holding a signed-in Chrome profile is a rename that
loses somebody's login, so anything still there is moved across the first time this is
imported - only what is not already here, and never over the top of anything.
"""
from __future__ import annotations

import os
import shutil
import time

HOME = os.path.expanduser(os.environ.get("LATCHKEY_HOME") or "~/.latchkey")
FORMER_HOME = os.path.expanduser("~/.abrowser")

# When this process started, for `owner_id()`. Read once, at import.
_STARTED = int(time.time())


# What we look for when moving in. Anything else in the old directory is left alone.
CARRIED = ("sessions", "profile", "credentials.json", "hints.json")


def path(*parts: str) -> str:
    """A path inside latchkey's own directory."""
    return os.path.join(HOME, *parts)


def owner_id() -> str:
    """A name for this latchkey process, unique on the machine for its lifetime.

    It exists because a browser window and a Chrome profile directory are things two
    servers cannot share - Chrome allows exactly one browser per profile - so anything of
    that kind has to belong to a *process*, not to the machine. With this in the path, two
    models that both want a clone of the same profile get two clones and two windows; with
    a fixed path they got one window, and took turns killing each other's to get it.

    Stable for the whole run (so a session can find the clone it made), and different for
    the next run (so a dead server's leftovers are never adopted by a live one).
    """
    return f"{os.getpid()}-{_STARTED}"


def clones_dir(*parts: str) -> str:
    """This process's own clone directories. Never a path another server also has.

    The home directory is read here rather than taken from `HOME`, so a caller (or a test)
    that points LATCHKEY_HOME somewhere else gets its clones there too - these directories
    hold real browser profiles, and they must never land in someone's actual latchkey
    directory by accident.
    """
    home = os.path.expanduser(os.environ.get("LATCHKEY_HOME") or "~/.latchkey")
    return os.path.join(home, "clones", owner_id(), *parts)


def owners_dir() -> str:
    """Where a server leaves a note saying which Chrome it opened, so no other server
    mistakes a live window for an abandoned one and closes it."""
    home = os.path.expanduser(os.environ.get("LATCHKEY_HOME") or "~/.latchkey")
    return os.path.join(home, "owners")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # someone else's process, but it exists
    except OSError:
        return False
    return True


def prune_clones() -> int:
    """Delete clone directories whose latchkey process is gone. Returns how many went.

    Each server gets its own clone - that is what stops two models fighting over one Chrome
    window - and a clone is a whole browser profile. Left alone they would pile up one per
    server run. The directory is named with the owner id, `<pid>-<started>`, so a directory
    whose process is not running is a leftover by construction; a live server's clone is
    somebody's open browser and is never touched.
    """
    home = os.path.expanduser(os.environ.get("LATCHKEY_HOME") or "~/.latchkey")
    root = os.path.join(home, "clones")
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    removed = 0
    for name in names:
        head = name.split("-", 1)[0]
        if not head.isdigit() or _alive(int(head)):
            continue
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        removed += 1
    return removed


def migrate(old: str = FORMER_HOME, new: str | None = None) -> list[str]:
    """Move anything left under the old name across. Returns what moved."""
    new = new or HOME
    if not os.path.isdir(old) or os.path.abspath(old) == os.path.abspath(new):
        return []
    moved = []
    for name in CARRIED:
        source, dest = os.path.join(old, name), os.path.join(new, name)
        if not os.path.exists(source) or os.path.exists(dest):
            continue
        try:
            os.makedirs(new, exist_ok=True)
            shutil.move(source, dest)
            moved.append(name)
        except OSError:
            continue          # a directory we cannot move is not worth failing an import
    return moved
