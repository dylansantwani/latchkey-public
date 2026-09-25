"""Starting the real Chrome binary ourselves, with exactly the flags we mean.

Two launches live here, and the difference between them is the whole point.

**A window for a human** (`launch_visible`). Google refuses a sign-in from a browser that
looks automated - "This browser or app may not be secure" - and it is right to: the flags
Playwright launches with (`--enable-automation`, a debugging port, `--use-mock-keychain`,
`--disable-sync`, `--disable-field-trial-config` and two dozen more) are exactly what an
automated browser looks like. So the one-time sign-in is an ordinary Chrome window on
latchkey's own profile directory and nothing else: no port, no automation switch, no
Playwright default. A person signs in to it the way they would to any new Chrome.

**A browser for an agent** (`launch_for_automation`). Afterwards the same profile has to be
driven, and the cookies it holds were written under the real macOS Keychain. Playwright's
persistent-context launch would bring its defaults back - including the mock keychain, which
makes every cookie that window wrote unreadable - so this starts the same binary with a
short, explicit list (a debugging port Chrome picks itself, headless, the fingerprint's
window size) and the session attaches over CDP. Nothing about the profile is emulated or
swapped: it is the directory the human signed in to, opened by the same Chrome.

Both refuse a profile another Chrome already holds. Chrome allows one process per profile
directory, and a second launch does not fail - it quietly hands its window to the running
one and exits, which from here looks like a Chrome that never started.
"""
from __future__ import annotations

import atexit
import contextlib
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import paths

DEVTOOLS_ACTIVE_PORT = "DevToolsActivePort"
SINGLETON_LOCK = "SingletonLock"

# What the human's sign-in window must never be launched with. Kept as data so a test can
# hold the launch to it: each of these is something Google's sign-in page can see or infer.
AUTOMATION_FLAGS = (
    "--enable-automation", "--remote-debugging-port", "--remote-debugging-pipe",
    "--use-mock-keychain", "--password-store=basic", "--disable-sync",
    "--disable-field-trial-config", "--disable-background-networking",
    "--disable-component-update", "--disable-extensions", "--disable-default-apps",
    "--headless", "--disable-blink-features=AutomationControlled", "--test-type",
    "--no-sandbox", "--remote-allow-origins",
)


# The Chromes this process started for its own sessions. A profile lock held by one of these
# is this server's own session, not "another Chrome", and is said differently.
_LAUNCHED: set[int] = set()


def launched_here(pid: int | None) -> bool:
    return bool(pid) and pid in _LAUNCHED


def _owner_file(pid: int) -> str:
    return os.path.join(paths.owners_dir(), f"{pid}.json")


@contextlib.contextmanager
def _owners_lock(exclusive: bool = True):
    """Serialize changes to the shared ownership notes across server processes.

    A note is a live-server veto on killing Chrome.  Consequently a reader must never
    mistake a note being written by another process for malformed stale state.  The
    lock covers cleanup as well as replacement; the note itself is atomically replaced
    so readers outside this module still see either generation, never a partial JSON.
    """
    root = paths.owners_dir()
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, ".lock"), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield root
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_owner(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


def _write_owner(path: str, record: dict) -> None:
    """Durably replace one ownership note without exposing a partial document."""
    fd, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.",
                                     suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def record_owner(pid: int, profile_dir: str, session: str | None = None) -> None:
    """Write down that *this* server opened that Chrome, before anything can doubt it.

    A Chrome launched here is detached (`start_new_session=True`), so the kernel re-parents
    it to launchd at once: its parent is 1 for its whole life, while the server that opened
    it is very much alive and working. Reading that silence as "its server is gone" made
    every latchkey server a licence to kill every other one's browser - two models on the
    same profile took turns terminating each other's window, and re-seeding the profile
    under it. The fix is not a better guess about a parent pid; it is a note, written by
    the only process that actually knows.
    """
    try:
        with _owners_lock() as root:
            for name in os.listdir(root):
                if not name.endswith(".json"):
                    continue
                note = os.path.join(root, name)
                old = _read_owner(note)
                # A note whose server is gone is stale, and the Chrome it named is exactly the
                # orphan this record exists to describe - so it goes, and that Chrome is then
                # clearable by whoever wants the profile next. So does a note about a Chrome that
                # has exited, or about a profile directory that no longer exists: there is nothing
                # left to protect either way, and pid numbers are handed out again.
                if (not isinstance(old, dict) or not pid_alive(old.get("server_pid"))
                        or not pid_alive(old.get("pid"))
                        or not os.path.isdir(old.get("profile_dir") or "")):
                    try:
                        os.remove(note)
                    except OSError:
                        pass
            _write_owner(_owner_file(pid), {"pid": int(pid), "profile_dir": profile_dir,
                         "session": session, "server_pid": os.getpid(),
                         "server": paths.owner_id(), "started_at": time.time()})
    except OSError:
        pass            # a missing note is survivable; a wrong kill is not


def owner_record(pid: int | None) -> dict | None:
    """What is written about this Chrome, whether or not its server is still up."""
    if not pid:
        return None
    try:
        with _owners_lock(exclusive=False):
            return _read_owner(_owner_file(int(pid)))
    except OSError:
        return None


def live_owner(pid: int | None) -> dict | None:
    """The note for this Chrome if the server that opened it is still running.

    None means nobody is claiming it: either no latchkey server opened it, or the one that
    did has exited and left it behind.
    """
    record = owner_record(pid)
    if not record:
        return None
    server = record.get("server_pid")
    if isinstance(server, int) and server == os.getpid():
        return record
    return record if pid_alive(server) else None


def clear_owner(pid: int | None) -> None:
    """Forget a Chrome this server closed itself."""
    if not pid:
        return
    try:
        with _owners_lock():
            os.remove(_owner_file(int(pid)))
    except OSError:
        pass


@atexit.register
def _stop_launched() -> None:
    """A server that exits without closing its sessions must not leave their Chrome behind.

    Playwright's own launch tied the browser to its driver's pipe, so it died with the
    process. A Chrome started here is a detached process, so it is asked to quit here. This
    does not run on SIGKILL; `orphaned_automation` is what covers that.
    """
    for pid in list(_LAUNCHED):
        found = process_info(pid)
        # Only a Chrome that still looks like the one started here: a pid is a number, and
        # the operating system hands numbers out again.
        if not found or "--remote-debugging-port=0" not in found[1]:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        clear_owner(pid)


def process_info(pid: int) -> tuple[int, str] | None:
    """(parent pid, command line) of a running process, or None."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    head, _, command = out.partition(" ")
    if not head.strip().isdigit():
        parts = out.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return None
        head, command = parts
    return int(head), command.strip()


def orphaned_automation(pid: int, profile_dir: str,
                        info: Callable[[int], Any] = process_info) -> bool:
    """Is this the headless Chrome of a latchkey server that has died without closing it?

    Such a Chrome still holds the profile lock, so without this one crash - a SIGKILL, a
    client that pulled the plug - locks the Google profile until a person finds the pid. It
    looks like our Chrome: our profile, the port Chrome picks for itself, headless. What it
    has to answer is whether the server that started it is *still running*.

    The parent pid cannot answer that. Every launch here is detached
    (`start_new_session=True`), so the kernel re-parents Chrome to launchd immediately: its
    parent is 1 from the first second, while its server is alive and mid-task. Judging by
    `parent == 1` therefore declared every other server's live browser an orphan, and two
    models on the same profile took turns terminating each other's window - the bug where
    models appear to use each other's windows. The note `record_owner` writes is the answer
    instead: a Chrome a live server claims is never an orphan. The parent test remains for
    Chromes from before that note existed.
    """
    found = info(pid)
    if not found:
        return False
    parent, command = found
    # A live server's note vetoes the verdict, but only about *this* profile: a note is
    # written per Chrome pid, and pid numbers are handed out again after a Chrome exits.
    held = live_owner(pid)
    if held and held.get("profile_dir") == profile_dir:
        return False            # a server that is still running opened it, and wants it
    return (parent == 1 and f"--user-data-dir={profile_dir}" in command
            and "--remote-debugging-port=0" in command and "--headless" in command)


def busy_here(pid: int, profile_dir: str) -> "ProfileBusy":
    """A ProfileBusy that says whose window this is, and never suggests taking it.

    "Another session of this server" and "another server's session" need different answers:
    only the first is a name that exists here. Telling a model to pass the second one along
    as `session` is how a model ends up reaching for a window that is not its own - it makes
    a local session with a name borrowed from someone else's server.
    """
    record = owner_record(pid) or {}
    session = record.get("session")
    # A Chrome this process started is this server's whether or not its note survived
    # (the note is best-effort; the launch set is not).
    if record.get("server_pid") == os.getpid() or launched_here(pid):
        return ProfileBusy(pid, profile_dir, (
            f"another session of this latchkey server already has {profile_dir} open "
            f"(Chrome pid {pid}{f', session {session!r}' if session else ''}), and Chrome "
            f"allows one browser per profile. Use that session - pass its name as `session` "
            f"- or close it first."))
    who = "another latchkey server"
    if record.get("server_pid"):
        who += f" (pid {record['server_pid']})"
    return ProfileBusy(pid, profile_dir, (
        f"{profile_dir} is open in Chrome pid {pid}, which {who} opened"
        + (f" for its session {session!r}" if session else "")
        + ". That window is not this server's: its session names do not exist here, and "
          "closing it would pull the page out from under a model that is working. Open a "
          "session of your own instead - this server's clones and windows live under its "
          "own directory - or ask that server to close it."))


class ProfileBusy(RuntimeError):
    """A profile directory is already open in another Chrome process."""

    def __init__(self, pid: int, profile_dir: str, detail: str = "") -> None:
        self.pid = pid
        self.profile_dir = profile_dir
        super().__init__(
            detail or
            f"latchkey's profile {profile_dir} is already open in another Chrome (pid {pid}) - "
            f"another latchkey server's session, or a window someone opened on it by hand. "
            f"Chrome allows one process per profile: close that one (latchkey_session_close "
            f"in the server that opened it), or point LATCHKEY_PROFILE_DIR somewhere else.")


def binary() -> str:
    """The installed Google Chrome. The same finder real mode uses."""
    from .profile import chrome_binary
    return chrome_binary()


def app_bundle(path: str) -> str | None:
    """The .app a Chrome binary lives in, for `open -a`. None when it is not in one."""
    marker = ".app/Contents/MacOS/"
    if marker not in path:
        return None
    return path.split(marker, 1)[0] + ".app"


def pid_alive(pid: int | None) -> bool:
    """Is this process still running? A child that has exited but not been reaped is not."""
    if not pid or pid <= 0:
        return False
    try:
        # A Chrome this process started itself lingers as a zombie after it exits, and a
        # zombie still answers kill(0) - which read as "never closed" for fifteen seconds.
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass                     # not our child: kill(0) below is the whole answer
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_owner(profile_dir: str) -> int | None:
    """The live process holding a profile directory, from its SingletonLock, or None.

    The lock is a symlink to `<hostname>-<pid>`. Reading where it points is read-only and
    touches nothing inside the profile, which is what makes this safe to ask about the
    user's real Chrome directory too. A lock left behind by a crash names a dead pid, and
    Chrome treats that as free, so this does as well.
    """
    try:
        target = os.readlink(os.path.join(profile_dir, SINGLETON_LOCK))
    except OSError:
        return None
    tail = target.rsplit("-", 1)[-1]
    if not tail.isdigit():
        return None
    pid = int(tail)
    return pid if pid_alive(pid) else None


def major_version(version: str | None = None) -> int | None:
    """Chrome's major version, from `153.0.8010.37`."""
    if version is None:
        from .cookies import chrome_version
        version = chrome_version()
    head = str(version or "").split(".", 1)[0]
    return int(head) if head.isdigit() else None


def devtools_active_port(profile_dir: str, *, probe: bool = True) -> tuple[int, str] | None:
    """The (port, browser path) a Chrome with debugging on wrote into its profile, if live.

    Chrome writes this file whenever it listens for DevTools, whether the port was given or
    chosen with `--remote-debugging-port=0`. A file whose port answers nothing is a leftover
    from a browser that has gone, not an endpoint.
    """
    try:
        with open(os.path.join(profile_dir, DEVTOOLS_ACTIVE_PORT), encoding="utf-8") as fh:
            lines = [line.strip() for line in fh.read().splitlines() if line.strip()]
    except OSError:
        return None
    if not lines or not lines[0].isdigit():
        return None
    port = int(lines[0])
    path = lines[1] if len(lines) > 1 and lines[1].startswith("/") else ""
    if probe and not port_open(port):
        return None
    return port, path


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tail(path: str, lines: int = 8) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).strip()
    except OSError:
        return ""


@dataclass
class Launched:
    """A Chrome this process started for automation, and how to put it away again."""
    pid: int
    endpoint: str                      # ws://127.0.0.1:<port>/devtools/browser/<id>
    profile_dir: str
    log: str = ""
    process: Any = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        return pid_alive(self.pid)

    def close(self, timeout_s: float = 10.0, *, sleep: Callable[[float], None] = time.sleep) -> bool:
        """Wait for Chrome to leave (a graceful Browser.close was sent first), then insist.

        Graceful matters: the cookie store commits in batches, and a Chrome killed outright
        loses the last half minute of a login it just earned. So it is given `timeout_s` to
        exit on its own, then SIGTERM, which Chrome also handles as an orderly shutdown, and
        only then SIGKILL.
        """
        for sig, patience in ((None, timeout_s), (signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            if not self.alive:
                _LAUNCHED.discard(self.pid)
                clear_owner(self.pid)
                return True
            if sig is not None:
                try:
                    os.kill(self.pid, sig)
                except OSError:
                    pass
            deadline = time.monotonic() + patience
            while self.alive and time.monotonic() < deadline:
                sleep(0.1)
        if not self.alive:
            _LAUNCHED.discard(self.pid)
            clear_owner(self.pid)
            return True
        return False


def launch_for_automation(profile_dir: str, *, headless: bool = True,
                          args: list[str] | tuple = (), timeout_s: float = 25.0,
                          chrome: str | None = None, session: str | None = None,
                          popen: Callable[..., Any] = subprocess.Popen,
                          sleep: Callable[[float], None] = time.sleep) -> Launched:
    """Start Chrome on a profile for a session to attach to, and return its endpoint.

    A profile another Chrome holds is not taken: `busy_here` says whose it is. The one case
    that clears the way is a Chrome whose *server* is gone, and that is decided by the note
    `record_owner` left behind - a note whose server is still running is never overruled.
    """
    owner = lock_owner(profile_dir)
    if owner and not launched_here(owner) and orphaned_automation(owner, profile_dir):
        terminate(owner, 10.0, sleep=sleep)
        owner = lock_owner(profile_dir)
    if owner:
        raise busy_here(owner, profile_dir) if launched_here(owner) else \
            ProfileBusy(owner, profile_dir)
    active = os.path.join(profile_dir, DEVTOOLS_ACTIVE_PORT)
    try:
        os.remove(active)          # a stale one would be read as this launch's port
    except OSError:
        pass
    flags = [f"--user-data-dir={profile_dir}", "--remote-debugging-port=0",
             "--no-first-run", "--no-default-browser-check"]
    flags += [flag for flag in args if flag not in flags]
    if headless:
        flags.append("--headless=new")
    flags.append("about:blank")
    log = os.path.join(tempfile.gettempdir(), "latchkey-dedicated-chrome.log")
    with open(log, "w", encoding="utf-8") as handle:
        process = popen([chrome or binary(), *flags], stdout=handle, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = devtools_active_port(profile_dir, probe=False)
        if found:
            port, path = found
            _LAUNCHED.add(int(process.pid))
            record_owner(int(process.pid), profile_dir, session)
            return Launched(pid=int(process.pid), endpoint=f"ws://127.0.0.1:{port}{path}",
                            profile_dir=profile_dir, log=log, process=process)
        if process.poll() is not None:
            raise RuntimeError(
                f"Chrome exited before it opened a debugging port on {profile_dir} (exit "
                f"{process.poll()}). If another Chrome has that profile open, it handed the "
                f"window over and left. Chrome said: {_tail(log)[-400:] or '(nothing)'}")
        sleep(0.1)
    launched = Launched(pid=int(process.pid), endpoint="", profile_dir=profile_dir, log=log,
                        process=process)
    launched.close(timeout_s=0.5)
    raise RuntimeError(f"Chrome did not open a debugging port on {profile_dir} within "
                       f"{timeout_s:.0f}s. Chrome said: {_tail(log)[-400:] or '(nothing)'}")


# Whether a window latchkey opens for a human grabs focus. Off by default: an agent
# opening a sign-in window while the user is reading a chat should not yank them out of it -
# the window is there when they want it (a login step says so), it just does not steal the
# screen. LATCHKEY_WINDOW_FOREGROUND=1 restores the old bring-to-front behaviour, and the
# `foreground` argument overrides both for a caller that knows a person is waiting on it.
FOREGROUND_ENV = "LATCHKEY_WINDOW_FOREGROUND"


def window_foreground(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return (os.environ.get(FOREGROUND_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def visible_command(profile_dir: str, url: str, chrome: str | None = None,
                    platform: str | None = None, *, foreground: bool | None = None) -> list[str]:
    """The command that opens an ordinary Chrome window on a profile. No automation in it."""
    chrome = chrome or binary()
    flags = [f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check"]
    app = app_bundle(chrome)
    if (platform or sys.platform) == "darwin" and app and shutil.which("open"):
        # `open -n` is a new instance; `-g` opens it in the background so it does not steal
        # focus (the fix for "an agent brought a window to the front without asking"). A
        # caller with a person waiting - the `latchkey login` CLI - passes foreground=True.
        opts = ["-n"] if window_foreground(foreground) else ["-g", "-n"]
        return ["open", *opts, "-a", app, "--args", *flags, url]
    return [chrome, *flags, url]


def launch_visible(profile_dir: str, url: str, *, chrome: str | None = None,
                   popen: Callable[..., Any] = subprocess.Popen, wait_s: float = 15.0,
                   foreground: bool | None = None,
                   sleep: Callable[[float], None] = time.sleep) -> int | None:
    """Open a normal Chrome window on a profile, for a human. Returns the browser's pid.

    Returns as soon as Chrome holds the profile; it does not wait for the person. The
    process is started in a session of its own, so it outlives the latchkey that opened it -
    a sign-in can take longer than an MCP server stays up. The window opens in the background
    (no focus steal) unless `foreground` or LATCHKEY_WINDOW_FOREGROUND says otherwise.
    """
    owner = lock_owner(profile_dir)
    if owner:
        raise ProfileBusy(owner, profile_dir)
    command = visible_command(profile_dir, url, chrome, foreground=foreground)
    log = os.path.join(tempfile.gettempdir(), "latchkey-login-chrome.log")
    with open(log, "w", encoding="utf-8") as handle:
        popen(command, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
              start_new_session=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        pid = lock_owner(profile_dir)
        if pid:
            # The sign-in window is a real window a person may be typing into, and the login
            # state file that names it is one file per machine. Without this note a second
            # latchkey server read that shared pid, matched it against the profile lock, and
            # quit a window it had never opened - one model's sign-in disappearing under
            # another model's. Written down by the server that does know.
            record_owner(pid, profile_dir, session="login-window")
            return pid
        sleep(0.2)
    return None


def terminate(pid: int, timeout_s: float = 15.0, *,
              sleep: Callable[[float], None] = time.sleep,
              kill: Callable[[int, int], None] = os.kill) -> bool:
    """Ask a Chrome to quit (SIGTERM is an orderly shutdown for Chrome) and wait for it."""
    if not pid_alive(pid):
        return True
    try:
        kill(pid, signal.SIGTERM)
    except OSError:
        return not pid_alive(pid)
    deadline = time.monotonic() + timeout_s
    while pid_alive(pid) and time.monotonic() < deadline:
        sleep(0.2)
    return not pid_alive(pid)
