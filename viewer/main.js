'use strict';

// The viewer watches latchkey through its loopback HTTP API and never talks to
// Chrome. That is deliberate: an observer that can act on the agent is a bug, so
// this process only reads, and every failure here has to end up as text in the
// panel rather than as an exception.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { app, BrowserWindow, ipcMain, globalShortcut, screen, session } = require('electron');

const DEFAULT_PORT = 8788;
const SCAN_RANGE = 11;            // 8788..8798, for a viewer older than the registry file
// Small, and only a starting point: the window takes the page's shape as soon as the first
// frame decodes (see fitWindow), because a fixed height leaves bands of nothing above and
// below the picture. It sits in the top-left so it is out of the way of what it describes.
const WINDOW_WIDTH = 360;
const WINDOW_HEIGHT = 300;
const WINDOW_MARGIN = 20;
const MIN_FIT_WIDTH = 160;
const MIN_FIT_HEIGHT = 80;
const PROBE_TIMEOUT_MS = 1500;
// Every MCP connection gets its own latchkey server process, and the viewer socket lives
// inside that process, so the sessions are on whichever port that process was given. The
// default port is therefore a guess, and a wrong guess is invisible: another latchkey
// server answers 200 with an empty session list, so the window looks connected and empty
// at the same time. That was the intermittent failure this file now defends against.
const SWITCH_INTERVAL_MS = 6000;
// The pointer reaches the strip whenever mouse forwarding works, so this chord is
// insurance rather than a feature: it is the one way out if click-through is on and
// the pointer never lands on the panel.
const TOGGLE_ACCELERATOR = 'CommandOrControl+Shift+A';

const explicitPort = readPort(process.argv);
let port = explicitPort;
const log = (...parts) => console.log('[viewer]', ...parts);

let win = null;
let clickThrough = true;
let probePromise = Promise.resolve(null);
let switching = false;

function readPort(argv) {
  // Both spellings, because the space form is how anyone would type it and it used
  // to be ignored in silence - the app then talked to the default port and reported
  // "latchkey is not running" at a port nobody was using.
  const at = argv.findIndex((arg) => arg === '--port' || arg.startsWith('--port='));
  const raw = at < 0 ? process.env.LATCHKEY_VIEWER_PORT
    : argv[at] === '--port' ? argv[at + 1]
      : argv[at].slice('--port='.length);
  if (raw === undefined || raw === null || raw === '') return DEFAULT_PORT;
  const value = Number.parseInt(raw, 10);
  // A silently ignored bad port would look like "the server is not running", which
  // sends the user to debug the wrong process.
  if (!Number.isInteger(value) || value < 1 || value > 65535) {
    log(`--port=${raw} is not a port; using ${DEFAULT_PORT}`);
    return DEFAULT_PORT;
  }
  return value;
}

// -- finding the viewer that has the sessions -------------------------------------------

function registryPorts() {
  // Each viewer writes itself down as it starts (see latchkey/viewer.py), which is the
  // difference between asking and guessing. Dead pids are ignored: a server that was
  // killed leaves its entry behind.
  const file = path.join(os.homedir(), '.latchkey', 'viewers.json');
  let entries = [];
  try {
    entries = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (err) {
    return [];
  }
  if (!Array.isArray(entries)) return [];
  return entries
    .filter((entry) => entry && typeof entry.port === 'number' && pidAlive(entry.pid))
    .sort((a, b) => (b.started_at || 0) - (a.started_at || 0))
    .map((entry) => entry.port);
}

function pidAlive(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';        // someone else's process, but it exists
  }
}

function candidatePorts() {
  const ports = [explicitPort, ...registryPorts()];
  for (let i = 0; i < SCAN_RANGE; i += 1) ports.push(DEFAULT_PORT + i);
  return [...new Set(ports)];
}

// One request, with Node's own http client: no CORS, no renderer, so the decision can be
// made and logged before any window exists.
function askSessions(portNumber) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port: portNumber, path: '/sessions', timeout: PROBE_TIMEOUT_MS },
      (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => { body += chunk; });
        res.on('end', () => {
          let sessions = null;
          try {
            const parsed = JSON.parse(body);
            if (parsed && Array.isArray(parsed.sessions)) sessions = parsed.sessions;
          } catch (err) {
            sessions = null;             // answered, but not with latchkey's JSON
          }
          resolve({ port: portNumber, status: res.statusCode, sessions,
            ok: res.statusCode === 200 && sessions !== null });
        });
      },
    );
    req.on('timeout', () => req.destroy(new Error('timed out')));
    req.on('error', (err) => resolve({ port: portNumber, status: null, sessions: null,
      ok: false, error: err.code || err.message }));
  });
}

async function findViewer() {
  // Prefer a viewer that actually has sessions. That is the whole point: a 200 with an
  // empty list is a real server showing nothing, and it used to win simply by being the
  // default port.
  const probed = await Promise.all(candidatePorts().map(askSessions));
  const live = probed.filter((p) => p.ok);
  const busy = live.filter((p) => p.sessions.length > 0);
  const chosen = busy[0] || live[0] || null;
  if (!chosen) return null;
  const others = live.filter((p) => p !== chosen)
    .map((p) => `${p.port} (${p.sessions.length} sessions)`);
  log(`using 127.0.0.1:${chosen.port}: ${chosen.sessions.length} session(s)`
    + (others.length ? `; also listening: ${others.join(', ')}` : ''));
  return chosen;
}

async function adoptViewer(initial) {
  // Adopt a different viewer if one turns up with sessions while this one shows none.
  // Without this the window would sit empty for as long as the agent used another server.
  if (switching || !win || win.isDestroyed()) return;
  switching = true;
  try {
    const found = await findViewer();
    const current = initial || await askSessions(port);
    if (!found || found.port === port) {
      probePromise = Promise.resolve(current);
      return;
    }
    if (current && current.ok && current.sessions && current.sessions.length > 0) return;
    log(`switching to 127.0.0.1:${found.port} (this one has no sessions)`);
    port = found.port;
    allowLoopbackCors();
    probePromise = Promise.resolve(found);
    win.webContents.reload();          // the renderer re-reads the port from viewer:config
  } catch (err) {
    log(`could not pick a viewer: ${err.message}`);
  } finally {
    switching = false;
  }
}

function allowLoopbackCors() {
  // The renderer is a file:// page, so its EventSource and frame fetches are
  // cross-origin and Chromium drops them unless the response carries
  // Access-Control-Allow-Origin. The latchkey API is fixed and promises no CORS
  // headers, so the header is added here, for this one loopback origin only. The
  // alternative - webSecurity: false - would drop the same protection for every
  // request the window will ever make, including anything a served page tried.
  const filter = { urls: [`http://127.0.0.1:${port}/*`] };
  session.defaultSession.webRequest.onHeadersReceived(filter, (details, callback) => {
    const headers = details.responseHeaders || {};
    const present = Object.keys(headers).some((k) => k.toLowerCase() === 'access-control-allow-origin');
    if (!present) headers['Access-Control-Allow-Origin'] = ['*'];
    callback({ responseHeaders: headers });
  });
}

function setClickThrough(on) {
  clickThrough = !!on;
  applyIgnoreMouse();
  log(`click-through ${clickThrough ? 'on' : 'off'}`);
  return clickThrough;
}

function applyIgnoreMouse() {
  if (!win || win.isDestroyed()) return;
  // forward:true is what makes the click-through mode usable at all - clicks pass
  // through to the page underneath while move events still reach the strip, so the
  // strip can ask for the clicks back as soon as the pointer is over it.
  win.setIgnoreMouseEvents(clickThrough, { forward: true });
}

function wireIpc() {
  ipcMain.handle('viewer:config', async () => ({
    port,
    clickThrough,
    server: await probePromise,
  }));
  ipcMain.on('viewer:interactive', (_event, on) => {
    if (!clickThrough || !win || win.isDestroyed()) return;
    if (on) win.setIgnoreMouseEvents(false);
    else applyIgnoreMouse();
  });
  ipcMain.handle('viewer:toggle-click-through', () => setClickThrough(!clickThrough));
  ipcMain.handle('viewer:viewer-port', () => port);
  ipcMain.on('viewer:fit', (_event, size) => fitWindow(size));
  ipcMain.on('viewer:quit', () => app.quit());
}

// The renderer measures the strip, the meta line and the frame's own aspect, and asks for a
// window that fits them exactly. It decides when: on a new page shape, and on a resize the
// user makes - never on every frame, or dragging the window would be undone a second later.
function fitWindow(size) {
  if (!win || win.isDestroyed()) return;
  const width = Math.round(Number(size && size.width) || 0);
  const height = Math.round(Number(size && size.height) || 0);
  if (width < MIN_FIT_WIDTH || height < MIN_FIT_HEIGHT) return;
  const area = screen.getPrimaryDisplay().workArea;
  const capped = Math.min(height, Math.round(area.height * 0.9));
  win.setContentSize(Math.min(width, area.width - 2 * WINDOW_MARGIN), capped);
}

function createWindow() {
  const area = screen.getPrimaryDisplay().workArea;
  win = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    // Top-right, out of the way of the page it is describing and of anything centred.
    x: area.x + area.width - WINDOW_WIDTH - WINDOW_MARGIN,
    y: area.y + WINDOW_MARGIN,
    transparent: true,
    frame: false,
    hasShadow: false,
    resizable: true,
    maximizable: false,
    fullscreenable: false,
    title: 'latchkey viewer',
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  win.setAlwaysOnTop(true, 'floating');
  // Watching headless Chrome usually means Chrome is fullscreen, which is exactly
  // when an overlay on the desktop stops being visible.
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  applyIgnoreMouse();

  // The renderer console is where the viewer reports what it connected to and what
  // failed, and a GUI window has no terminal attached. Mirror it, so `npx electron .`
  // in a shell tells the same story as the panel.
  win.webContents.on('console-message', (...args) => {
    // Electron changed this to a single event object; the old three-argument form is
    // still checked, because the version this runs under is whatever npm installed.
    const message = args.length >= 3 ? args[2] : (args[0] && args[0].message);
    // The renderer's own lines already start with [viewer]; a second prefix only made
    // the two processes look like one confusing one.
    if (message) console.log(message);
  });
  win.webContents.on('render-process-gone', (_event, details) => log(`renderer gone: ${details.reason}`));
  win.webContents.on('unresponsive', () => log('viewer window is unresponsive'));

  // The panel draws a page it does not own; a stray link or a redirect must not turn
  // the overlay into a browser with its own history.
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event) => event.preventDefault());

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

app.whenReady().then(async () => {
  wireIpc();

  const found = await findViewer();
  if (found) {
    port = found.port;
    probePromise = Promise.resolve(found);
  } else {
    port = explicitPort;
    probePromise = Promise.resolve({ ok: false, status: null, error: 'nothing listening',
      port });
  }
  allowLoopbackCors();
  createWindow();

  const probe = await probePromise;
  if (probe.ok) {
    log(`latchkey server on 127.0.0.1:${port}: answered HTTP ${probe.status}`);
  } else if (probe.status) {
    // "refused" and "someone else's server" are fixed in completely different places,
    // and to the panel they would otherwise look the same.
    log(`127.0.0.1:${port} answered HTTP ${probe.status} for /sessions - that is not latchkey's API`);
  } else {
    log(`no latchkey viewer found on 127.0.0.1 (tried ${candidatePorts().length} ports, `
      + `starting with ${explicitPort}). start one with: python3 -m latchkey watch`);
  }

  // Keep looking. A session opened in another server process, or a viewer started after
  // this window, would otherwise be invisible here for the life of the window.
  setInterval(() => { adoptViewer(null); }, SWITCH_INTERVAL_MS);

  const registered = globalShortcut.register(TOGGLE_ACCELERATOR, () => setClickThrough(!clickThrough));
  if (!registered) log(`could not register ${TOGGLE_ACCELERATOR}; use the strip button to toggle click-through`);
});

app.on('will-quit', () => globalShortcut.unregisterAll());

// Unlike most macOS apps this one has no reason to outlive its window: there is no
// document to reopen, only a live stream that stops being useful without a screen.
app.on('window-all-closed', () => app.quit());
