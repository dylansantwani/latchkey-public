# latchkey viewer

A transparent, always-on-top overlay that shows what an agent is doing in latchkey:
the latest frame of each session, the agent's pointer drawn between frames, and every
action as it lands.

```
latchkey session  ->  /events (SSE)  ->  viewer  ->  your screen
                      /frame/<session>?n=<counter>
```

The viewer is a **reader**. It opens no page, drives no browser and owns no session -
it subscribes to the event stream and fetches frames over the same loopback API the
agent's server already publishes. Close it, kill it, or leave it pointed at a port
where nothing is listening; the agent does not notice either way.

## Finding the server

Every MCP connection gets its own latchkey server process, and the viewer socket lives
inside that process, so the sessions are on whichever port that process was given. The
app therefore looks for a live server instead of trusting one:

1. the ports in `~/.latchkey/viewers.json`, which each viewer writes as it starts (dead
   pids are ignored);
2. then `8788`-`8798`;
3. preferring a server that actually **has** sessions, because a wrong port is invisible -
   another latchkey server answers `200` with an empty session list, so the window looks
   connected and empty at the same time. It says which port it chose, and why:

```
[viewer] using 127.0.0.1:8791: 2 session(s); also listening: 8788 (0 sessions)
```

It re-checks every six seconds and moves to a better server if the agent turns out to be
using another one, or if the one it was on goes away - an overlay that needs restarting
whenever the agent reconnects is not worth much. `--port` (or `LATCHKEY_VIEWER_PORT`) pins
a port: it wins whenever it has sessions, and is only passed over when it has none and
another live server has some.

`latchkey_viewers` reports what is listening; `latchkey_viewer_start` returns the URL of
the viewer inside the server that holds the sessions.

## Run it

```bash
cd ~/tools/latchkey/viewer
npm install
npm start                      # finds the server that has the sessions
npm start -- --port=9000       # or pin one
npm start -- --port 9000       # both spellings work, as does LATCHKEY_VIEWER_PORT=9000
```

Start a server first, in another shell:

```bash
cd ~/tools/latchkey
python3 -m latchkey watch
```

In practice you usually do not: the MCP server starts its own viewer when
`LATCHKEY_VIEWER_PORT` is set (or when `latchkey_viewer_start` is called), and the app will
find it. If nothing is listening anywhere, the overlay says so, names the ports it tried,
and prints the command to start a server. It keeps looking, so starting one later attaches
without restarting the app.

## The window

| | |
|---|---|
| Transparent, frameless, always-on-top | it is an overlay, so it must not hide the page it is describing |
| **Sized to the page** | it takes the frame's aspect plus the strip, so there are no bands of nothing above or below the picture. It starts small in the **top-right** and refits when the page changes shape or you drag the window wider |
| **Nothing else** | the strip (tabs, click-through, quit) and the picture. The URL and the verdict are still in the DOM for the tooltip and the log, but they do not take space, and only trouble you have to act on (a close that failed, a dropped stream) is allowed to draw a banner over the frame |
| **Click-through by default** | clicks pass to whatever is underneath; the strip takes them back the moment the pointer is over it |
| `⇢` / `⇤` on the strip | click-through on / off, for reading a long selector or quitting |
| `⌘⇧A` | toggles click-through too - the way out if the strip is ever out of reach |
| drag the strip | moves the window |
| `✕` on a tab | stops and deletes that session (and closes its browser). Two clicks: the tab arms red on the first, goes on the second. This is the way to clear a session that has stopped answering || `✕` on the strip | quits the viewer only |

## What it shows

Just the page: the frame for the current session, with the agent's pointer drawn over it, and
one line under the strip naming the URL and the verdict (`logged-in` / `logged-out` /
`blocked` / `unclear`). The action list that used to sit at the bottom is gone - the frame is
the point, and the verdict was the only thing in that list worth keeping in view.

One tab per session, one pane per session:

- the session name, its mode (`inject` / `clone`) and the current URL
- the latest frame, fetched by counter - one request per new frame, never a poll
- the agent's pointer, animated between positions from the `cursor` and `event`
  messages, so it keeps moving across the seconds when no frame is encoded
- recent actions, newest first, with the selector and the verdict

## Design notes

**Frames are fetched, not pushed.** The event stream carries a counter and the
renderer asks for `/frame/<session>?n=<counter>`; base64 images through SSE would
cost more than the stream is worth. A 204 means no frame exists yet, so the last
image stays on screen rather than the box going blank. A 404 means the session is
gone, and the last frame is kept too - what a session left behind is often the most
useful thing about it.

**The session list comes from the stream, not from `/sessions`.** `GET /sessions` is
used only as a liveness probe - its status code, never its body - because the stream
already carries the list on every change, and the body's shape is the server's
business. Unknown SSE message types (the server's `heartbeat`, for instance) are
named once in the log and otherwise ignored: something harmless must not light up a
banner, or the banner stops meaning anything.

**Events that do not parse cost a banner, not the panel.** The API is fixed, so an
answer this renderer did not expect is a bug in the renderer: it says so, with the
URL and the reason, and keeps going. Every fetch is inside a `try`, a dead stream
reconnects with a backoff, and a session leaving the list greys its tab instead of
clearing the screen.

**`Access-Control-Allow-Origin` is added for loopback in `main.js`.** Chromium treats
a `file://` page as an opaque origin, so the stream and the frames would be dropped
as cross-origin unless the response carries that header, and the published API does
not promise it. The header is injected for `http://127.0.0.1:<port>/*` only.
`webSecurity: false` would have been one line and would have removed the same
protection from every request this window will ever make.

**The renderer is sandboxed.** `contextIsolation` on, `nodeIntegration` off, no
remote, `sandbox: true`, and a CSP that allows exactly one origin - the viewer port.
`preload.js` exposes four things and nothing else.

## Build the .app

```bash
npm run build        # -> dist/mac-arm64/latchkey viewer.app
open "dist/mac-arm64/latchkey viewer.app" --args --port=8788
```

electron-builder, **`dir` target**, no signing (`identity: null`). `dir` is not a
weaker choice here: the target that used to be called `app` no longer exists in
electron-builder 26 (its schema rejects the name), and `dir` is what still produces
the standalone `latchkey viewer.app` bundle - a `dmg` would only wrap that same
bundle in a disk image. The bundle runs locally and is not notarised, so Gatekeeper
will object if it is ever copied to another machine. No frameworks and no bundler
anywhere: plain files, so what runs from source is what ships in the bundle.

## Files

```
viewer/
  main.js             window, click-through, the server probe, the loopback CORS header
  preload.js          the whole bridge: config, interactive, toggle, quit
  renderer/index.html the panel
  renderer/app.js     SSE client, frame fetching, cursor drawing
  renderer/style.css
```
