# LatchkeyBar — the viewer as a menu bar toggle

The latchkey viewer's web UI, one click away in the menu bar, and one click away again.

- **left click** — the panel: the web UI of whichever latchkey server has the sessions
- **esc** — put it away
- **right click** — the menu: status, open in browser, copy the address, sessions, a
  viewer of its own when no agent is running, launch at login, quit

The icon says which of the two states it is in: filled while a viewer is live, a dimmed
hollow rectangle while nothing is listening.

## Which server it shows

Every latchkey process has its own viewer port, and a port that answers with no sessions
looks connected and empty at the same time. So the same policy the Electron viewer uses
applies here:

1. the ports in `~/.latchkey/viewers.json`, ignoring entries whose pid has died;
2. then `8788`-`8798`;
3. preferring the server that actually has sessions, because that is the one an agent is
   driving. `--port N` pins a port instead, and is still passed over when it has no
   sessions and another live server has some.

## Nothing is listening

The panel says so, and offers to start a viewer of its own — `python3 -m latchkey watch`
on 8788. Worth knowing before you press it: a viewer serves the sessions of *its own*
process, so a standalone one shows the sessions of a script you run there, not the ones
an agent process is driving. An agent's viewer comes up with the agent, when its
`LATCHKEY_VIEWER_PORT` is set.

## Build and install

```sh
menubar/install.sh          # build, install to ~/Applications, start it, print its status
```

That compiles `LatchkeyBar/main.swift` with `swiftc -O` (no dependencies beyond AppKit
and WebKit), signs it ad-hoc, writes the login agent `~/Library/LaunchAgents/
com.dylan.latchkeybar.plist` (the same plist the menu's *Launch at login* writes), and
kickstarts it. It is safe to run again after any change: it stops the running copy first,
so there is only ever one icon.

`LATCHKEY_ROOT` (default `~/tools/latchkey`) and `LATCHKEY_PYTHON`
(default `/usr/bin/python3`) say where latchkey and its python are.

## Looking at it without a menu bar

```sh
LatchkeyBar --status                 # "latchkeybar live=8794 sessions=3 source=scan registry=0 button=ok"
LatchkeyBar --render out.png         # the panel: the live page, or the empty state
LatchkeyBar --renderlabel out.png    # the menu-bar glyph, both states
LatchkeyBar --port 8788 --render out.png
LatchkeyBar --login on               # write the login agent and load it if it is not loaded
LatchkeyBar --help
```

`--status` prints one line and writes the same line to `/tmp/latchkeybar.status` (the
running app rewrites that file every two seconds), so what the icon believes can be read
from anywhere. `button=ok` in it is only added by the running app, and only if the status
item actually took - the one thing a screenshot would otherwise be needed for. `--render`
needs no screen-recording permission either: the app draws itself into a PNG, which is how
the panel was checked against a live viewer mid-session.
