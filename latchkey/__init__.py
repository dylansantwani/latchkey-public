"""latchkey - headless Chrome that is already logged in, for agents.

    from latchkey import Browser, logged_in

    with logged_in() as browser:          # every cookie, headless
        state = browser.goto("https://chatgpt.com/")
        if state.verdict == "logged-out":
            # the user signs in on their real browser
            print(browser.wait_for_login("https://chatgpt.com/"))
        browser.screenshot("/tmp/out.png")

Several agents at once, each with its own browser:

    from latchkey import SessionRegistry

    registry = SessionRegistry()
    canvas = registry.get("canvas", spec=SessionSpec(mode="clone", label="canvas"))
    canvas.submit(lambda browser: browser.goto("https://canvas.example/"))
"""
from . import paths as _paths

# The package was called abrowser, so it wrote to ~/.abrowser. Anything still there -
# saved sessions, the dedicated profile, which holds a real login of yours - is moved
# across once, here, rather than quietly abandoned under the old name.
_paths.migrate()

from . import credentials, paths, profile, store  # noqa: E402
from .cookies import Cookie, load, profiles, redact, to_cdp, user_agent
from .detect import PageState
from .events import Event, bus
from .intervene import Intervention, InterventionManager
from .intervene import manager as interventions
from .reaper import IdleReaper
from .session import (
    Browser,
    InjectionReport,
    SessionSpec,
    capture,
    logged_in,
)
from .sessions import Session, SessionError, SessionRegistry, registry

__all__ = [
    "Browser", "Cookie", "Event", "InjectionReport", "PageState", "Session",
    "SessionError", "SessionRegistry", "SessionSpec",
    "Intervention", "InterventionManager", "IdleReaper",
    "bus", "registry", "interventions",
    "load", "profiles", "to_cdp", "user_agent", "redact",
    "capture", "logged_in", "store", "credentials", "profile",
]
__version__ = "1.3.0"
