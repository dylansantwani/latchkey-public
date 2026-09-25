'use strict';

// The viewer reads one loopback API: /events for the stream, /frame/<session>?n= for
// images, /sessions for the list. It is a reader on purpose. Nothing here can drive a
// browser, so a viewer bug can lose the picture but never the agent's session, and
// every failure below ends as a line in the panel instead of an uncaught error.

// Frame failures are normal, not exceptional: the server answers 204 until the first
// screencast frame exists, and a session can be removed mid-flight.
// The action list is gone on purpose: the panel shows the page and nothing else. One
// thing from that list was worth keeping, so the verdict now sits in the meta row.
const BACKOFF_START_MS = 500;
const BACKOFF_MAX_MS = 5000;
const DEFAULT_PORT = 8788;
const PROBE_RETRY_INTERVAL_MS = 4000;

// Session list fields, and the event detail keys worth showing, in the order a person
// reads them. Unknown detail keys are counted rather than dumped, because a detail
// dict is whatever a verb chose to publish and printing all of it would be noise.
const DETAIL_KEYS = ['selector', 'text', 'value', 'key', 'tab', 'host', 'mode', 'backend'];
const KNOWN_DETAIL = new Set(DETAIL_KEYS.concat(['x', 'y', 'url', 'verdict']));

// Message types this renderer has decided to say nothing about, named once in the log
// so a developer can see them and the panel stays quiet.
const ignoredTypes = new Set();

const state = {
  port: DEFAULT_PORT,
  config: null,
  status: 'connecting',   // connecting | live | reconnecting | down
  everConnected: false,
  runId: null,             // Event sequence numbers are scoped to one server process.
  active: null,
  order: [],
  sessions: new Map(),
};

let source = null;      // the one live EventSource, or null between attempts
let retryTimer = null;
let backoff = BACKOFF_START_MS;

const els = {
  strip: document.getElementById('strip'),
  status: document.getElementById('status'),
  tabs: document.getElementById('tabs'),
  thru: document.getElementById('thru'),
  quit: document.getElementById('quit'),
  stage: document.getElementById('stage'),
  empty: document.getElementById('empty'),
  emptyTitle: document.getElementById('empty-title'),
  emptyText: document.getElementById('empty-text'),
  emptyCode: document.getElementById('empty-code'),
  emptyMeta: document.getElementById('empty-meta'),
  retry: document.getElementById('retry'),
  banners: document.getElementById('banners'),
};

const log = (...parts) => console.log('[viewer]', ...parts);

// ---------------------------------------------------------------------------
// failures, said out loud
// ---------------------------------------------------------------------------

const banners = new Map();

// A small window over someone's work is no place for a pile of notices. Only the ones that
// need an action from the user draw a banner - a close that failed, a stream that dropped,
// a viewer that cannot reach its own preload bridge; everything else (a session ending, an
// old server, the arming hint) goes to the log, where the curious can find it.
const BANNER_KEYS = new Set(['closed', 'sse', 'bridge', 'endpoint']);

function showBanner(key, text, kind, action) {
  if (!BANNER_KEYS.has(key)) {
    log(`(${key}) ${text}`);
    return;
  }
  let el = banners.get(key);
  if (!el) {
    el = document.createElement('div');
    banners.set(key, el);
    els.banners.append(el);
  }
  // Rebuilt rather than patched: a banner is a few nodes, and the button a banner
  // carries may not be the button the next message needs.
  el.className = `banner ${kind || 'error'}`;
  el.replaceChildren();
  const body = document.createElement('span');
  body.className = 'text';
  body.textContent = text;
  el.append(body);
  if (action) {
    const run = document.createElement('button');
    run.type = 'button';
    run.className = 'act';
    run.textContent = action.label;
    run.addEventListener('click', action.onClick);
    el.append(run);
  }
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'close';
  close.textContent = '✕';
  close.addEventListener('click', () => clearBanner(key));
  el.append(close);
}

function clearBanner(key) {
  const el = banners.get(key);
  if (!el) return;
  el.remove();
  banners.delete(key);
}

// The HTTP API is fixed, so an answer whose shape this renderer did not expect means
// the renderer is the stale half. Say so once, with the offending URL, rather than
// dying on an exception or dropping the message quietly.
function noteEndpoint(where, reason) {
  log(`unexpected response from ${where}: ${reason}`);
  showBanner('endpoint', `The viewer did not understand ${where}: ${reason}`, 'info');
}

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

function quote(value) {
  return /\s/.test(value) ? JSON.stringify(value) : value;
}

function detailBody(detail) {
  if (!detail || typeof detail !== 'object') return '';
  const parts = [];
  const used = new Set();
  for (const key of DETAIL_KEYS) {
    const value = detail[key];
    if (value === undefined || value === null || value === '') continue;
    used.add(key);
    parts.push(`${key}=${typeof value === 'string' ? quote(value) : String(value)}`);
  }
  if (!parts.length && typeof detail.url === 'string') parts.push(detail.url);
  const extra = Object.keys(detail).filter((k) => !used.has(k) && !KNOWN_DETAIL.has(k));
  if (extra.length) parts.push(`+${extra.length} more`);
  return parts.join(' ');
}

function clockTime(ts) {
  const ms = Number(ts) * 1000;
  const when = Number.isFinite(ms) ? new Date(ms) : new Date();
  return when.toTimeString().slice(0, 8);
}

function sessionRecord(name) {
  return {
    name,
    label: name,
    mode: '',
    alive: true,
    url: '',
    counter: 0,
    everHadFrame: false,
    logged204: false,
    cursor: null,
    lastSeq: 0,
    tabEl: null,
    pane: null,
    imgEl: null,
    viewEl: null,
    pointerEl: null,
    noteEl: null,
    rippleEl: null,
    badgeEl: null,
    urlEl: null,
  };
}

function getOrCreate(name) {
  let rec = state.sessions.get(name);
  if (rec) return rec;
  rec = sessionRecord(name);
  state.sessions.set(name, rec);
  state.order.push(name);
  log(`session ${name} appeared`);
  buildTab(rec);
  buildPane(rec);
  if (!state.active) selectSession(name);
  updateEmptyState();
  updateLiveBanner();
  return rec;
}

// Sessions that have ended keep their tab, their last frame, so
// a list where every session is gone has nothing left to say "live" about. The user
// gets told that, and the button that removes the remains is theirs to press.
function updateLiveBanner() {
  const records = [...state.sessions.values()];
  if (!records.length || records.some((rec) => rec.alive)) {
    clearBanner('ended');
    return;
  }
  showBanner('ended', 'No live session. What is on screen is what the last one left behind.', 'info', {
    label: 'clear',
    onClick: clearEndedSessions,
  });
}

function clearEndedSessions() {
  for (const rec of [...state.sessions.values()]) {
    if (rec.alive) continue;
    if (rec.imgEl && rec.imgEl.dataset.objectUrl) {
      URL.revokeObjectURL(rec.imgEl.dataset.objectUrl);
      delete rec.imgEl.dataset.objectUrl;
    }
    if (rec.tabEl) rec.tabEl.remove();
    if (rec.pane) rec.pane.remove();
    // The per-session notes refer to panes that are now gone, so they go too - a banner
    // about a tab nobody can see is just litter.
    clearBanner(`gone:${rec.name}`);
    state.sessions.delete(rec.name);
  }
  state.order = state.order.filter((name) => state.sessions.has(name));
  if (!state.sessions.has(state.active)) {
    state.active = null;
    if (state.order.length) selectSession(state.order[state.order.length - 1]);
  }
  clearBanner('ended');
  updateEmptyState();
}

// ---------------------------------------------------------------------------
// chrome: tabs and panes
// ---------------------------------------------------------------------------

function buildTab(rec) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'tab';
  // The name goes on the node as well as into JS: this overlay is inspected from the
  // outside when something looks wrong, and a pane nobody can name is a pane nobody
  // can check.
  button.dataset.session = rec.name;
  const dot = document.createElement('span');
  dot.className = 'dot';
  const name = document.createElement('span');
  name.className = 'name';
  button.append(dot, name);
  button.addEventListener('click', () => selectSession(rec.name));
  els.tabs.append(button);
  const close = document.createElement('span');
  close.className = 'x';
  close.textContent = '✕';
  close.title = `Stop and delete session ${rec.name}`;
  close.addEventListener('click', (event) => {
    event.stopPropagation();     // closing a tab must not also select it
    askClose(rec.name);
  });
  button.append(close);

  rec.tabEl = button;
}

function buildPane(rec) {
  const pane = document.createElement('div');
  pane.className = 'pane';
  pane.dataset.session = rec.name;
  pane.hidden = true;

  const view = document.createElement('div');
  view.className = 'frame-view';
  const image = document.createElement('img');
  image.className = 'frame';
  image.alt = `latest frame from session ${rec.name}`;
  image.addEventListener('error', () => {
    // Reached when the bytes were not an image after all, and when a frame was swapped
    // out mid-load. Either way the previous picture is still there and still better
    // than an empty box.
    log(`frame from ${rec.name} did not decode`);
    setFrameNote(rec, 'the last frame did not decode - keeping the one before it');
  });
  image.addEventListener('load', () => {
    setFrameNote(rec, '');
    rec.everHadFrame = true;
    // A frame arriving is also the moment a pointer position from the stream finally
    // has a picture to sit on.
    drawCursor(rec);
  });
  const pointer = document.createElement('div');
  pointer.className = 'pointer';
  pointer.hidden = true;
  const dot = document.createElement('div');
  dot.className = 'dot';
  pointer.append(dot);
  const ripple = document.createElement('div');
  ripple.className = 'ripple';
  const note = document.createElement('div');
  note.className = 'frame-note';
  note.hidden = true;
  view.append(image, pointer, ripple, note);

  const meta = document.createElement('div');
  meta.className = 'meta';
  const badge = document.createElement('span');
  badge.className = 'badge';
  const url = document.createElement('span');
  url.className = 'url';
  // The verdict used to live in the action list. It is the one thing from that list worth
  // keeping on screen: whether the page the agent is on is signed in.
  const verdict = document.createElement('span');
  verdict.className = 'verdict';
  verdict.hidden = true;
  meta.append(badge, verdict, url);

  pane.append(view, meta);
  els.stage.append(pane);

  rec.pane = pane;
  rec.viewEl = view;
  rec.imgEl = image;
  rec.pointerEl = pointer;
  rec.rippleEl = ripple;
  rec.noteEl = note;
  rec.badgeEl = badge;
  rec.verdictEl = verdict;
  rec.urlEl = url;
}

function selectSession(name) {
  const rec = state.sessions.get(name);
  if (!rec) return;
  state.active = name;
  for (const other of state.sessions.values()) {
    const on = other.name === name;
    if (other.pane) other.pane.hidden = !on;
    if (other.tabEl) other.tabEl.classList.toggle('active', on);
  }
  drawCursor(rec);
}

// ---------------------------------------------------------------------------
// stopping a session
// ---------------------------------------------------------------------------

// This is the only control here that changes the agent's world, so it takes two clicks:
// the first arms the tab for a few seconds, the second goes through. It is also how a
// session that has stopped answering gets cleared - the HTTP API stays answerable when a
// session's own thread is wedged.
let armingClose = null;
let armTimer = null;

function disarmClose() {
  armingClose = null;
  clearTimeout(armTimer);
  clearBanner('close-arm');
  for (const other of state.sessions.values()) {
    if (other.tabEl) other.tabEl.classList.remove('arming');
  }
}

function askClose(name) {
  const rec = state.sessions.get(name);
  if (armingClose === name) {
    disarmClose();
    closeSession(name);
    return;
  }
  disarmClose();
  armingClose = name;
  if (rec && rec.tabEl) rec.tabEl.classList.add('arming');
  showBanner('close-arm', `Click the ✕ on ${name} again to stop it and close its browser`, 'info');
  armTimer = setTimeout(disarmClose, 4000);
}

async function closeSession(name) {
  const rec = state.sessions.get(name);
  if (rec && rec.tabEl) {
    rec.tabEl.classList.remove('arming');
    rec.tabEl.classList.add('closing');
  }
  log(`closing session ${name} from the viewer`);
  try {
    const response = await fetch(
      `http://127.0.0.1:${state.port}/session/${encodeURIComponent(name)}/close`,
      { method: 'POST' },
    );
    const body = await response.json().catch(() => ({}));
    const running = Array.isArray(body.running) ? body.running : null;
    if (response.status === 501) {
      // 501 means the endpoint is not there at all: this server was started before the
      // viewer could stop a session. Saying so is the difference between "the button is
      // broken" and "restart that process".
      showBanner('closed', `This latchkey server is too old to close sessions (HTTP 501): it was `
        + `started before the viewer could stop one. Restart it - or the app that launched `
        + `it - and the ✕ will work.`);
      return;
    }
    if (response.ok && body.ok) {
      showBanner('closed', `Closed ${name}. Still running: ${((running || []).join(', ')) || 'none'}`, 'info');
    } else {
      showBanner('closed', `Could not close ${name}: ${body.error || `HTTP ${response.status}`}`
        + (running ? `. Running: ${running.join(', ') || 'none'}` : ''));
    }
  } catch (err) {
    showBanner('closed', `Could not close ${name}: ${err.message}. Is the viewer server still up?`);
  } finally {
    if (rec && rec.tabEl) rec.tabEl.classList.remove('closing');
  }
}

function renderChrome(rec) {
  if (rec.tabEl) {
    rec.tabEl.classList.toggle('gone', !rec.alive);
    rec.tabEl.querySelector('.name').textContent = rec.label || rec.name;
    rec.tabEl.title = rec.alive
      ? `${rec.name}${rec.mode ? ` - ${rec.mode}` : ''}`
      : `${rec.name} - no longer in the session list; showing what it left behind`;
  }
  if (rec.badgeEl) {
    rec.badgeEl.textContent = rec.alive
      ? (rec.mode ? `mode ${rec.mode}` : 'mode unknown')
      : 'session gone';
  }
  if (rec.urlEl) {
    rec.urlEl.textContent = rec.url || (rec.everHadFrame ? '' : 'no url reported yet');
    rec.urlEl.title = rec.url || '';
  }
  if (rec.verdictEl) {
    const verdict = rec.verdict || '';
    rec.verdictEl.textContent = verdict;
    rec.verdictEl.className = `verdict ${verdict}`;
    rec.verdictEl.hidden = !verdict;
  }
}

function setFrameNote(rec, text) {
  if (!rec.noteEl) return;
  rec.noteEl.textContent = text;
  rec.noteEl.hidden = !text;
}

// ---------------------------------------------------------------------------
// the pointer
// ---------------------------------------------------------------------------

// The frame is the viewport, so viewport coordinates land at the same relative spot in
// the picture. Scaling is computed from the image's own pixels rather than assumed,
// which is what keeps the pointer on the button when the window has been resized.
// ---------------------------------------------------------------------------
// the window takes the shape of the page
// ---------------------------------------------------------------------------

let fittedAspect = null;

// Measures what the window has to be: the strip, the meta line, and the frame at the
// picture's own aspect. It is asked for on a new page shape and on a resize the user makes,
// never on every frame - refitting per frame would undo a drag a second after it happened.
function requestFit(naturalWidth, naturalHeight, force) {
  if (!naturalWidth || !naturalHeight) return;
  const aspect = naturalHeight / naturalWidth;
  if (!force && fittedAspect && Math.abs(fittedAspect - aspect) / aspect < 0.02) return;
  const width = Math.max(document.documentElement.clientWidth, 160);
  const strip = document.getElementById('strip');
  const meta = document.querySelector('.pane:not([hidden]) .meta');
  const chrome = (strip ? strip.offsetHeight : 0) + (meta ? meta.offsetHeight : 0);
  const height = Math.round(width * aspect) + chrome;
  // Setting the size fires a resize, which comes back here: without this the two would
  // bounce off each other for as long as the window stayed open.
  if (Math.abs(height - document.documentElement.clientHeight) <= 2 && !force) return;
  fittedAspect = aspect;
  log(`fitting the window to the page: ${width}x${height} (aspect ${aspect.toFixed(3)})`);
  if (window.viewer && window.viewer.fit) window.viewer.fit({ width, height });
}

function frameGeometry(rec) {
  if (!rec.imgEl || !rec.viewEl) return null;
  const picture = rec.imgEl.naturalWidth;
  const pictureHeight = rec.imgEl.naturalHeight;
  const box = rec.viewEl.getBoundingClientRect();
  if (!picture || !pictureHeight || !box.width || !box.height) return null;
  // What the pointer is measured in: the page's CSS pixels, not the picture's. A frame is a
  // picture of the viewport at the screencast's own width, so the two are different numbers
  // and the page's is the one a cursor event is in. A frame that arrived before any cursor
  // event falls back to the picture, which is what it used to always do.
  const vw = (rec.viewport && rec.viewport.vw) || picture;
  const vh = (rec.viewport && rec.viewport.vh) || pictureHeight;
  const scale = Math.min(box.width / vw, box.height / vh);
  return {
    scale,
    left: (box.width - vw * scale) / 2,
    top: (box.height - vh * scale) / 2,
  };
}

function drawCursor(rec) {
  if (!rec.pointerEl) return;
  if (!rec.cursor) {
    rec.pointerEl.hidden = true;
    return;
  }
  const geometry = frameGeometry(rec);
  if (!geometry) {
    rec.pointerEl.hidden = true;
    return;
  }
  rec.pointerEl.hidden = false;
  rec.pointerEl.style.transform =
    `translate(${geometry.left + rec.cursor.x * geometry.scale}px, ${geometry.top + rec.cursor.y * geometry.scale}px)`;
}

function pulse(rec) {
  if (!rec.rippleEl) return;
  const geometry = frameGeometry(rec);
  if (!geometry || !rec.cursor) return;
  rec.rippleEl.style.left = `${geometry.left + rec.cursor.x * geometry.scale}px`;
  rec.rippleEl.style.top = `${geometry.top + rec.cursor.y * geometry.scale}px`;
  rec.rippleEl.classList.remove('go');
  void rec.rippleEl.offsetWidth;   // restart the animation on a rapid second click
  rec.rippleEl.classList.add('go');
}

function movePointer(rec, x, y, click, kind) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  rec.cursor = { x, y, kind: typeof kind === 'string' ? kind : 'idle' };
  drawCursor(rec);
  if (click) pulse(rec);
}

function redrawPointers() {
  for (const rec of state.sessions.values()) {
    if (rec.pane && !rec.pane.hidden) drawCursor(rec);
  }
}

// ---------------------------------------------------------------------------
// stream messages
// ---------------------------------------------------------------------------

function applySessions(list) {
  if (!Array.isArray(list)) {
    noteEndpoint('/sessions', 'the session list was not an array');
    return;
  }
  const present = new Set();
  for (const raw of list) {
    if (!raw || typeof raw.name !== 'string' || !raw.name) {
      noteEndpoint('/sessions', 'a session had no name');
      continue;
    }
    present.add(raw.name);
    const rec = getOrCreate(raw.name);
    const wasGone = !rec.alive;
    rec.alive = raw.alive !== false;
    if (typeof raw.label === 'string' && raw.label) rec.label = raw.label;
    if (typeof raw.mode === 'string') rec.mode = raw.mode;
    // cursor:false means nobody is injecting the on-page pointer, so a pointer left
    // over from before would be a stale lie.
    if (raw.cursor === false) {
      rec.cursor = null;
      if (rec.pointerEl) rec.pointerEl.hidden = true;
    }
    if (wasGone && rec.alive) {
      clearBanner(`gone:${rec.name}`);
      log(`session ${rec.name} is back in the session list`);
    }
    renderChrome(rec);
  }

  for (const rec of state.sessions.values()) {
    if (present.has(rec.name) || !rec.alive) continue;
    // A session leaving the list is not a reason to wipe the panel: the frame, the URL
    // and the action list are still the last true thing anyone saw, and often the
    // reason the session ended is exactly what the user wants to read.
    rec.alive = false;
    const stillRunning = [...state.sessions.values()]
      .filter((other) => other.alive).map((other) => other.name);
    log(`session ${rec.name} left the session list; keeping its last frame`);
    showBanner(`gone:${rec.name}`,
      `Session ${rec.name} is no longer running - its last frame is kept. `
      + (stillRunning.length ? `Still running: ${stillRunning.join(', ')}. ` : 'Nothing else is running. ')
      + 'It was closed, it crashed, or its browser was shut down.', 'info');
    renderChrome(rec);
  }
  updateLiveBanner();
}

function applyEvent(event) {
  if (!event || typeof event !== 'object') {
    noteEndpoint('/events', 'an event was not an object');
    return;
  }
  if (typeof event.session !== 'string') {
    noteEndpoint('/events', 'an event arrived with no session name');
    return;
  }
  resetRun(event.run);
  const rec = getOrCreate(event.session);
  const seq = Number(event.seq) || 0;
  // A reconnect replays the tail of the bus, so without this the same click shows up
  // twice in the list every time the stream blinks.
  if (seq && seq <= rec.lastSeq) return;
  if (seq) rec.lastSeq = seq;

  const detail = (event.detail && typeof event.detail === 'object') ? event.detail : {};
  if (typeof detail.url === 'string' && detail.url) rec.url = detail.url;
  // The verdict is the one thing the removed action list was carrying that is still worth
  // having on screen: whether the page the agent is on is signed in.
  if (event.detail && typeof event.detail.verdict === 'string' && event.detail.verdict) {
    rec.verdict = event.detail.verdict;
    renderChrome(rec);
  }
  // One line per action, because this is the only place a person watching the
  // terminal can see that the stream is really moving and the panel is not stale.
  log(`action ${rec.name} #${seq} ${event.kind || 'event'}${detailBody(detail) ? ` ${detailBody(detail)}` : ''}`);
  if (typeof detail.x === 'number' && typeof detail.y === 'number') {
    movePointer(rec, detail.x, detail.y, event.kind === 'click', event.kind);
  }
  renderChrome(rec);
}

function resetRun(runId) {
  if (typeof runId !== 'string' || !runId || runId === state.runId) return;
  state.runId = runId;
  // A restarted server starts its sequence at one.  Keep the last picture and session
  // metadata, but make those new events eligible instead of treating them as reconnect
  // replays from the previous server.
  for (const rec of state.sessions.values()) rec.lastSeq = 0;
  log('event stream changed server run; reset event dedupe');
}

function applyFrame(name, counter) {
  const n = Number(counter);
  if (!Number.isFinite(n)) {
    noteEndpoint('/events', 'a frame counter was not a number');
    return;
  }
  if (typeof name !== 'string' || !name) {
    noteEndpoint('/events', 'a frame arrived with no session name');
    return;
  }
  const rec = getOrCreate(name);
  rec.counter = n;
  fetchFrame(rec);
}

function applyCursor(message) {
  if (typeof message.session !== 'string') {
    noteEndpoint('/events', 'a cursor message arrived with no session name');
    return;
  }
  const rec = getOrCreate(message.session);
  // The pointer is placed in the *page's* own pixels, so the page's own size has to travel
  // with it: a frame is a picture of the viewport taken at the screencast's width (1100 by
  // default) while a page is 1280 of them, and scaling by the picture drew the pointer a
  // fifth of the way off. See frameGeometry.
  if (Number(message.vw) > 0 && Number(message.vh) > 0) {
    rec.viewport = { vw: Number(message.vw), vh: Number(message.vh) };
  }
  movePointer(rec, Number(message.x), Number(message.y), message.click === true, message.kind);
}

function handle(payload) {
  let message;
  try {
    message = JSON.parse(payload);
  } catch (err) {
    noteEndpoint('/events', `a message was not JSON (${err.message})`);
    return;
  }
  if (!message || typeof message !== 'object') {
    noteEndpoint('/events', 'a message was not an object');
    return;
  }
  switch (message.type) {
    case 'hello':
      // The server announces itself before replaying; a connect that never gets this
      // is a stream that opened and said nothing.
      resetRun(message.run);
      log('stream connected: hello');
      clearBanner('sse');
      setStatus('live');
      break;
    case 'sessions':
      applySessions(message.sessions);
      break;
    case 'event':
      applyEvent(message.event);
      break;
    case 'frame':
      applyFrame(message.session, message.counter);
      break;
    case 'cursor':
      applyCursor(message);
      break;
    default:
      // Not every type has to mean something here: the real server also sends a
      // periodic heartbeat, which is none of this panel's business. An unknown type is
      // worth one line in the log; raising it in the panel would train the user to
      // ignore banners.
      if (!ignoredTypes.has(message.type)) {
        ignoredTypes.add(message.type);
        log(`ignoring message type ${JSON.stringify(message.type)}`);
      }
      break;
  }
}

// ---------------------------------------------------------------------------
// frames
// ---------------------------------------------------------------------------

async function fetchFrame(rec) {
  if (rec.fetching) {
    rec.again = true;   // a counter that changed while a fetch was in flight
    return;
  }
  rec.fetching = true;
  const counter = rec.counter;
  const url = `http://127.0.0.1:${state.port}/frame/${encodeURIComponent(rec.name)}?n=${counter}`;
  try {
    // `n` only ever changes when the picture does, so this is the same request the
    // server expects: one per new frame, never a poll.
    const response = await fetch(url, { cache: 'no-store' });
    if (response.status === 204) {
      // No frame encoded yet for this session. Not an error, and not a reason to drop
      // the picture already on screen.
      if (!rec.logged204) {
        rec.logged204 = true;
        log(`frame ${rec.name}: HTTP 204 (no frame encoded yet) - keeping the last image`);
      }
      setFrameNote(rec, 'waiting for the first frame');
      return;
    }
    if (response.status === 404) {
      // Verbose on purpose. A session that is not there is worth naming in full - which
      // one, what IS running, and the likeliest reason - because "404" on its own sends
      // the reader to debug the wrong process. The server answers this with that list.
      const body = await response.json().catch(() => ({}));
      const running = Array.isArray(body.running) ? body.running : [];
      showFrameProblem(rec, `session ${rec.name} does not exist on this viewer (404: `
        + `${body.error || 'no such session'}). `
        + (running.length ? `Running: ${running.join(', ')}. ` : 'No sessions are running. ')
        + 'It may have been closed, or the agent is driving another latchkey server.');
      return;
    }
    if (!response.ok) {
      showFrameProblem(rec, `frame request failed with HTTP ${response.status} - keeping the last image`);
      return;
    }
    const bytes = await response.blob();
    if (!bytes.size) {
      showFrameProblem(rec, 'the frame response was empty - keeping the last image');
      return;
    }
    // Blob URLs keep the frame off disk and out of the page's memory once replaced;
    // an <img> this page did not decode could not be trusted to keep the last picture.
    const next = URL.createObjectURL(bytes);
    const previous = rec.imgEl.dataset.objectUrl;
    rec.imgEl.dataset.objectUrl = next;
    rec.imgEl.src = next;
    if (previous) URL.revokeObjectURL(previous);
    // The picture is decoded by the time naturalWidth means anything, so the fit is asked
    // for after that - which is also exactly when the pane's shape is final.
    if (rec.imgEl.complete && rec.imgEl.naturalWidth) {
      requestFit(rec.imgEl.naturalWidth, rec.imgEl.naturalHeight);
    } else {
      rec.imgEl.addEventListener('load', () => {
        requestFit(rec.imgEl.naturalWidth, rec.imgEl.naturalHeight);
      }, { once: true });
    }
    log(`frame ${rec.name} n=${counter}: ${bytes.size} bytes, ${bytes.type || 'unknown type'}`);
  } catch (err) {
    // fetch rejects on a refused connection and on a CORS refusal alike; both mean the
    // picture is stale, neither is fatal.
    showFrameProblem(rec, `could not fetch the frame (${err.message}) - keeping the last image`);
  } finally {
    rec.fetching = false;
    if (rec.again) {
      rec.again = false;
      fetchFrame(rec);
    }
  }
}

function showFrameProblem(rec, text) {
  setFrameNote(rec, text);
}

// ---------------------------------------------------------------------------
// the stream itself
// ---------------------------------------------------------------------------

function setStatus(status) {
  state.status = status;
  const label = { connecting: 'connecting', live: 'live', reconnecting: 'reconnecting', down: 'no server' }[status];
  els.status.textContent = label;
  els.status.className = `pill ${status === 'live' ? 'live' : status === 'down' ? 'down' : 'warn'}`;
  updateEmptyState();
}

function connect() {
  if (source) {
    source.close();
    source = null;
  }
  try {
    source = new EventSource(`http://127.0.0.1:${state.port}/events`);
  } catch (err) {
    noteEndpoint('/events', `EventSource could not be created (${err.message})`);
    scheduleReconnect();
    return;
  }
  source.onopen = () => {
    // The connection is working, so the delay goes back to the floor; the next outage
    // should be reported as fast as the first one was.
    backoff = BACKOFF_START_MS;
    state.everConnected = true;
    clearBanner('sse');
    setStatus('live');
    log(`connected to http://127.0.0.1:${state.port}/events`);
  };
  source.onmessage = (message) => handle(message.data);
  source.onerror = () => {
    // EventSource retries on its own, but at a fixed interval and with no way to say
    // why it failed. Rebuilding it here is what makes the backoff ours and what lets
    // the panel show "starting..." vs "stopped" as different things.
    setStatus(state.everConnected ? 'reconnecting' : 'down');
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  if (source) {
    source.close();
    source = null;
  }
  if (retryTimer) return;
  const delay = backoff;
  backoff = Math.min(BACKOFF_MAX_MS, Math.round(backoff * 1.7));
  log(`stream down; retrying in ${delay}ms`);
  showBanner('sse', `No event stream from 127.0.0.1:${state.port}. Retrying in ${(delay / 1000).toFixed(1)}s. `
    + 'Start it with: python3 -m latchkey watch', 'error');
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, delay);
}

function retryNow() {
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  backoff = BACKOFF_START_MS;
  log('retrying on demand');
  connect();
}

// ---------------------------------------------------------------------------
// the empty state and the server probe
// ---------------------------------------------------------------------------

function updateEmptyState() {
  const showEmpty = state.sessions.size === 0;
  els.empty.hidden = !showEmpty;
  if (!showEmpty) return;

  const probe = state.config ? state.config.server : null;
  // A probe that failed *with* a status code means something is listening and it is not
  // latchkey. A probe that failed without one means nothing is listening. Same empty
  // panel, opposite fixes, so the two get different words.
  const refused = !!(probe && !probe.ok && !probe.status);
  const foreign = (probe && !probe.ok && probe.status) ? probe.status : null;
  const show = (title, text, code, meta) => {
    els.emptyTitle.textContent = title;
    els.emptyText.textContent = text;
    els.emptyCode.hidden = !code;
    if (code) els.emptyCode.textContent = code;
    els.emptyMeta.textContent = meta || '';
  };

  if (state.status === 'live') {
    show('no sessions',
      'latchkey is up, but no agent has opened a session yet. A session appears here '
      + 'the moment one is opened.',
      'python3 -m latchkey watch',
      'from an agent: latchkey_session_open(name="canvas")');
  } else if (refused) {
    show('no latchkey server',
      `Nothing answered on 127.0.0.1:${state.port} (${probe.error}). The viewer only reads, so the agent is unaffected.`,
      'python3 -m latchkey watch',
      'the viewer keeps retrying - start the server and it attaches by itself');
  } else if (foreign) {
    show('something else is on that port',
      `127.0.0.1:${state.port} answered, but /sessions returned HTTP ${foreign}.`,
      `curl -i http://127.0.0.1:${state.port}/sessions`,
      'if latchkey is on another port, start the viewer with --port=<n>');
  } else {
    show(state.everConnected ? 'reconnecting' : 'connecting',
      `Looking for the latchkey server on 127.0.0.1:${state.port}...`, null, null);
  }
}

// ---------------------------------------------------------------------------
// window behaviour: moving, click-through, quitting
// ---------------------------------------------------------------------------

function wireStrip() {
  els.thru.addEventListener('click', async () => {
    const on = await window.viewer.toggleClickThrough();
    renderThru(on);
  });
  els.quit.addEventListener('click', () => window.viewer.quit());
  els.retry.addEventListener('click', () => retryNow());

  // The window ignores mouse events by default so the page underneath stays clickable.
  // For that to be usable the strip has to take the clicks back the moment the pointer
  // is over it, which is only possible because the ignore call forwards move events.
  let interactive = false;
  const setInteractive = (on) => {
    if (on === interactive) return;
    interactive = on;
    window.viewer.setInteractive(on);
  };
  const overPanel = (target) => !!(target && target.closest && target.closest('[data-interactive], #strip'));
  window.addEventListener('mousemove', (event) => {
    if (!state.clickThrough) return;
    setInteractive(overPanel(event.target));
  });
  window.addEventListener('mouseleave', () => setInteractive(false));
  window.addEventListener('blur', () => setInteractive(false));
}

function renderThru(on) {
  state.clickThrough = on;
  els.thru.textContent = on ? '⇢' : '⇤';
  els.thru.classList.toggle('thru', on);
  els.thru.title = on
    ? 'Click-through on: clicks reach the page underneath. Clicks land here only while the pointer is over this strip.'
    : 'Click-through off: the panel captures every click.';
}

// ---------------------------------------------------------------------------
// startup
// ---------------------------------------------------------------------------

async function boot() {
  window.addEventListener('error', (event) => noteEndpoint('the viewer itself', `uncaught ${event.message}`));
  window.addEventListener('unhandledrejection', (event) => noteEndpoint('the viewer itself', `unhandled rejection ${event.reason}`));
  window.addEventListener('resize', redrawPointers);

  if (!window.viewer) {
    // A preload that failed to load leaves no bridge; without this the first call
    // would be an uncaught TypeError and the panel would stay blank.
    showBanner('bridge', 'The preload bridge is missing, so the viewer cannot read its port or take clicks. '
      + 'Reinstall with: npm install', 'error');
    setStatus('down');
    return;
  }

  wireStrip();

  let config;
  try {
    config = await window.viewer.getConfig();
  } catch (err) {
    log(`could not read the viewer config: ${err.message}`);
    config = { port: DEFAULT_PORT, clickThrough: true, server: { ok: false, error: err.message } };
  }
  state.config = config;
  state.port = config.port || DEFAULT_PORT;
  renderThru(config.clickThrough !== false);

  const probe = config.server;
  if (probe && probe.ok) {
    log(`main process probe: /sessions answered HTTP ${probe.status}`);
  } else if (probe && probe.status) {
    log(`main process probe: 127.0.0.1:${state.port} answered HTTP ${probe.status} for /sessions - not latchkey`);
  } else if (probe) {
    log(`main process probe: /sessions unreachable (${probe.error}) - start it with: python3 -m latchkey watch`);
  }

  log(`reading http://127.0.0.1:${state.port}`);
  setStatus('connecting');
  updateEmptyState();
  connect();

  // The probe is a one-off snapshot from before the window existed. Re-checking keeps
  // the empty panel from claiming "no latchkey server" after the user has started one.
  setInterval(async () => {
    if (state.status === 'live' || !state.config) return;
    const before = state.config.server || {};
    try {
      const response = await fetch(`http://127.0.0.1:${state.port}/sessions`, { cache: 'no-store' });
      state.config.server = { ok: response.ok, status: response.status, error: response.ok ? null : `HTTP ${response.status}` };
    } catch (err) {
      // A refused fetch from this side says only "Failed to fetch", while the main
      // process probe knew it was ECONNREFUSED. Keeping the better explanation is what
      // stops the panel getting vaguer the longer the server stays away.
      state.config.server = { ok: false, status: null, error: before.error || err.message };
    }
    updateEmptyState();
  }, PROBE_RETRY_INTERVAL_MS);

  // A window the user drags wider should get taller to match, or the picture starts
  // letterboxing in the box they just made. This is the one refit that is forced: the page
  // has not changed shape, the window has.
  window.addEventListener('resize', () => {
    const rec = state.active ? state.sessions.get(state.active) : null;
    if (rec && rec.imgEl && rec.imgEl.naturalWidth) {
      requestFit(rec.imgEl.naturalWidth, rec.imgEl.naturalHeight, true);
    }
  });

  probeCloseSupport();
}

// Ask the server, once, whether it can close a session. A process started before the viewer
// could stop one answers 501 to that endpoint, and the alternative to asking is the user
// clicking a ✕ that appears to do nothing. The name is one that cannot exist, so the probe
// is a no-op everywhere except in what it reports.
async function probeCloseSupport() {
  try {
    const response = await fetch(
      `http://127.0.0.1:${state.port}/session/__probe__/close`, { method: 'POST' },
    );
    if (response.status === 501) {
      log('this server answered 501 for the close endpoint: it predates closing from here');
      showBanner('stale-server',
        'This latchkey server was started before the viewer could stop a session, so the ✕ '
        + 'on a tab will not work here. Restart that process - or the app that launched it - '
        + 'and it will.', 'info');
    } else {
      log(`close endpoint answered HTTP ${response.status}: closing from here is available`);
    }
  } catch (err) {
    log(`could not probe the close endpoint: ${err.message}`);
  }
}

boot();
