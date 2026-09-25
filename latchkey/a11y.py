"""A page as a person would describe it, with a handle on every part worth acting on.

Why this exists next to `latchkey_text` and `latchkey_links`: text alone leaves an agent
guessing what to click, and a screenshot leaves it guessing what anything *is*. A snapshot
is the middle - "heading 'Sign in', textbox 'Email', button 'Continue'" - and each thing it
names carries a short handle (`e7`) that `latchkey_act` takes in place of a selector.

The shape is openbrowser's (`browser_snapshot`), which earns its place: `[ref=eN]` handles,
a tree that mentions only what is worth mentioning, modes for reading it (interactive /
full / text / outline / diff), and a character budget that ends in an outline of the page
rather than in the middle of a name.

A ref is minted once per element and kept, never reused. That is what makes a stale ref
safe: the element it named is gone, so the ref is gone, and the answer says which button it
used to be instead of quietly clicking whatever inherited the number.
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Any, Iterable

# The page walker: one entry per element worth naming, in document order, each tagged with
# a ref on the element itself so the same handle can be used to act on it.
SNAPSHOT_JS = """
(payload) => {
  const opts = payload || {};
  const ATTR = opts.attr || 'data-lk-ref';
  const SEQ_KEY = opts.seqKey || '__lkRefSeq';
  const MAX_NODES = opts.maxNodes || 1500;
  const SECRET = /pass|secret|token|otp|cvv|ccnum|cc-number|card|ssn|\\bpin\\b|security.?code|one.?time/i;
  const out = [];
  let seq = window[SEQ_KEY] || 0;
  let budget = MAX_NODES;
  let elided = 0;
  const clean = (text, limit) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, limit || 120);
  const refOf = (el) => {
    let ref = el.getAttribute(ATTR);
    if (ref) return ref;                       // minted once, so it never moves to another element
    ref = 'e' + (++seq);
    try { el.setAttribute(ATTR, ref); } catch (err) { return null; }
    return ref;
  };
  const visible = (el, box) => {
    if (!box || (box.width < 1 && box.height < 1)) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity || '1') === 0) return false;
    if (!opts.viewportOnly) return true;
    const vh = window.innerHeight || 0, vw = window.innerWidth || 0;
    return box.bottom > 0 && box.top < vh && box.right > 0 && box.left < vw;
  };
  const pathOf = (el) => {
    if (el.getRootNode() !== document) return '';   // a shadow path is not a document selector
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
      return '#' + CSS.escape(el.id);
    }
    const bits = [];
    let node = el;
    while (node && node.nodeType === 1 && bits.length < 4) {
      let bit = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = [...parent.children].filter((c) => c.tagName === node.tagName);
        if (same.length > 1) bit += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      bits.unshift(bit);
      node = parent;
    }
    return bits.join(' > ');
  };
  const textOf = (el, limit) => clean(el.textContent, limit || 120);
  const nameOf = (el, role) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const scope = el.getRootNode();
      const named = by.split(/\\s+/).map((id) => {
        const target = scope.getElementById ? scope.getElementById(id) : document.getElementById(id);
        return target ? target.textContent : '';
      }).join(' ');
      if (clean(named)) return clean(named);
    }
    if (el.labels && el.labels.length) return clean([...el.labels].map((l) => l.textContent).join(' '));
    if (role === 'img') return clean(el.getAttribute('alt') || el.getAttribute('title') || '');
    const tag = el.tagName;
    if (tag === 'INPUT') {
      if (['submit', 'button', 'reset'].includes(el.type)) return clean(el.value || el.type);
      if (el.type === 'file') return clean(el.getAttribute('title') || 'choose a file');
    }
    if (['SELECT', 'TEXTAREA', 'BUTTON', 'A', 'SUMMARY', 'OPTION', 'LABEL'].includes(tag) ||
        /^H[1-6]$/.test(tag)) {
      return textOf(el);
    }
    if (STRUCTURAL.has(role)) return '';   // a landmark is named by its label or not at all
    const hint = el.getAttribute('placeholder') || el.getAttribute('title') ||
                 el.getAttribute('name') || el.getAttribute('type') || '';
    if (hint) return clean(hint);
    return textOf(el, 80);
  };
  // Would showing what is typed into this field leak a credential? Decided here, on the
  // element, because the caller only ever sees a ref - and a ref carries no type.
  const secretOf = (el) => {
    if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') return false;
    if ((el.type || '').toLowerCase() === 'password') return true;
    const hay = [el.getAttribute('autocomplete'), el.name, el.id,
                 el.getAttribute('aria-label'), el.getAttribute('placeholder')]
                .filter(Boolean).join(' ');
    return SECRET.test(hay);
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === 'A') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'BUTTON') return 'button';
    if (tag === 'SELECT') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'FORM') return 'form';
    if (tag === 'IMG') return el.getAttribute('alt') ? 'img' : null;
    if (tag === 'NAV') return 'navigation';
    if (tag === 'MAIN') return 'main';
    if (tag === 'HEADER') return 'banner';
    if (tag === 'FOOTER') return 'contentinfo';
    if (tag === 'ASIDE') return 'complementary';
    if (tag === 'TABLE') return 'table';
    if (tag === 'DIALOG') return 'dialog';
    if (tag === 'SUMMARY') return 'button';
    if (tag === 'IFRAME') return 'iframe';
    if (/^H[1-6]$/.test(tag)) return 'heading';
    if (tag === 'INPUT') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'hidden') return null;
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'file') return 'file';
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'search') return 'searchbox';
      if (['number', 'range'].includes(type)) return 'slider';
      return 'textbox';
    }
    return null;
  };
  const INTERACTIVE = new Set(['button', 'link', 'textbox', 'searchbox', 'checkbox', 'radio',
                               'combobox', 'listbox', 'option', 'slider', 'file', 'switch',
                               'tab', 'menuitem', 'spinbutton']);
  const STRUCTURAL = new Set(['navigation', 'main', 'banner', 'contentinfo', 'complementary',
                              'form', 'dialog', 'table', 'heading', 'img', 'iframe',
                              'region', 'search']);
  const walk = (el, depth, parentRef, shadow) => {
    if (el.nodeType !== 1) return;
    if (budget <= 0) { elided++; return; }
    const tag = el.tagName;
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(tag)) return;
    const role = roleOf(el);
    const interactive = !!role && INTERACTIVE.has(role);
    const structural = !!role && STRUCTURAL.has(role);
    const shown = visible(el, el.getBoundingClientRect());
    const leafy = el.children.length === 0 && !el.shadowRoot;
    let here = parentRef;
    if (shown && (role || (opts.full && leafy && clean(el.textContent)))) {
      const ref = (interactive || structural) ? refOf(el) : null;
      const entry = { ref, role: role || 'text', name: '', depth };
      if (ref) {
        here = ref;                        // what the outline counts controls *inside*
        if (parentRef) entry.parent = parentRef;
      }
      entry.name = nameOf(el, role);
      if (role === 'heading') entry.level = Number(tag[1]) || 2;
      const secret = secretOf(el);
      if (secret) entry.secret = true;
      if (typeof el.value === 'string' && el.value && role !== 'button' &&
          !['checkbox', 'radio', 'file'].includes(role)) {
        // Never carry the value of a credential field out of the page: it would land in
        // the snapshot an agent reads, in find()'s matches and in the event stream.
        if (secret) entry.filled = el.value.length;
        else entry.value = clean(el.value, 60);
      }
      if (el.getAttribute('placeholder') && !entry.value) entry.placeholder = clean(el.placeholder, 60);
      if (el.disabled) entry.disabled = true;
      if (el.required) entry.required = true;
      if (el.checked) entry.checked = true;
      const expanded = el.getAttribute('aria-expanded');
      if (expanded) entry.expanded = expanded === 'true';
      if (ref) entry.sel = pathOf(el);
      if (shadow && ref) entry.shadow = true;
      if (entry.name || entry.ref) { out.push(entry); budget--; }
    }
    const deeper = (interactive || structural) ? depth + 1 : depth;
    // A custom element keeps its controls in a shadow root, and a walker that only reads
    // `children` sees an empty box where the site's whole UI is. Open roots are readable;
    // a closed one is genuinely not there for any script, ours included.
    if (el.shadowRoot) {
      for (const child of el.shadowRoot.children) walk(child, deeper, here, true);
    }
    for (const child of el.children) walk(child, deeper, here, shadow);
  };
  const root = opts.selector ? document.querySelector(opts.selector) : document.body;
  if (root) walk(root, 0, null, false);
  window[SEQ_KEY] = seq;
  const frames = [...document.querySelectorAll('iframe')].map(
    (f) => clean(f.src || f.getAttribute('src') || 'about:blank', 90));
  return { nodes: out, frames, elided };
}
"""

from .detect import short_url

MODES = ("interactive", "full", "text", "outline", "diff")
# What models call the modes. "mode must be one of interactive, full, text, outline, diff"
# is what `mode: "plain"` got on 10 September - a model that wanted the text and said so in
# a word the list did not have.
MODE_ALIASES = {
    "": "interactive", "default": "interactive", "controls": "interactive",
    "interactive_only": "interactive", "actionable": "interactive", "refs": "interactive",
    "a11y": "interactive", "aria": "interactive", "accessibility": "interactive",
    "tree": "interactive", "elements": "interactive", "compact": "interactive",
    "all": "full", "everything": "full", "complete": "full", "page": "full", "dom": "full",
    "plain": "text", "plaintext": "text", "plain_text": "text", "text_only": "text",
    "content": "text", "prose": "text", "readable": "text", "markdown": "text", "md": "text",
    "structure": "outline", "shape": "outline", "sections": "outline", "headings": "outline",
    "layout": "outline", "map": "outline",
    "changes": "diff", "changed": "diff", "delta": "diff", "since": "diff", "update": "diff",
    "updates": "diff", "new": "diff",
}
DEFAULT_MAX_CHARS = 6000
TRUNCATION_TAIL = 20          # outline lines kept when a snapshot is cut
# The walker stops naming things at this many nodes. A page with fifty thousand elements
# (a long table, an infinite feed) would otherwise ship all of them over CDP only for
# `render` to throw the tail away - the cost is paid in the transfer, not in the text.
MAX_NODES = 1500
# A match has to be more than a coincidence to be worth returning. Below this, `find`
# says there is nothing rather than handing back the best of a bad set - a model given
# a plausible-looking ref will act on it.
MATCH_FLOOR = 0.35


# Words that carry no search intent on their own. "sign in" is one content word and a
# preposition, and scoring the preposition is what let a logged-in GitHub answer it with
# "...special-casing in test-backend-ops.cpp". They are dropped whenever a content word
# survives, so "in" alone is still a search and "sign in" is a search for "sign".
STOPWORDS = frozenset({"a", "an", "the", "of", "to", "in", "on", "at", "for", "and", "or",
                       "is", "it", "be", "as", "by", "with", "from", "this", "that", "my",
                       "your", "me", "up", "out", "go", "click", "button", "link", "page"})
LONG_TEXT = 60      # beyond this, a partial match is a coincidence more often than not


def _has_word(text: str, word: str) -> bool:
    """Is `word` in `text` as a word, rather than as a run of letters inside another?"""
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


class RefError(RuntimeError):
    """A ref that cannot be honoured - saying what it was, and what to do instead."""


class Refs:
    """What the refs of this session's snapshots point at, so an action can name one.

    Per session, on the browser, because that is what a ref belongs to: a page in a tab.
    """

    KEEP = 4000        # refs remembered per session; the oldest are forgotten first

    def __init__(self, attr: str | None = None, seq_key: str | None = None) -> None:
        # Per session, and random. A fixed `data-abrowser-ref` on live elements was a
        # one-selector signature for anything looking for automation - which is a strange
        # thing to leave behind in a project that spends this much care on the fingerprint.
        token = secrets.token_hex(4)
        self.attr = attr or f"data-{token}"
        self.seq_key = seq_key or f"__s{token}"
        self.by_ref: dict[str, dict] = {}
        self.url = ""
        self.at = 0.0
        self.last_signature: dict[str, tuple] = {}

    def selector(self, ref: str) -> str:
        """The CSS that addresses a ref's element."""
        return f'[{self.attr}="{ref}"]'

    def ref_of(self, selector: str) -> str | None:
        """The ref behind one of our own selectors, or None for ordinary CSS."""
        found = re.fullmatch(rf'\[{re.escape(self.attr)}="([^"]+)"\]', (selector or "").strip())
        return found.group(1) if found else None

    def is_secret(self, ref: str) -> bool:
        """Does this ref name a field whose contents must never be reported?"""
        return bool((self.known(ref) or {}).get("secret"))

    def note(self, url: str, nodes: Iterable[dict]) -> None:
        """Remember what this snapshot's refs are, for resolution and for saying what they were."""
        self.url = url
        self.at = time.time()
        for node in nodes:
            ref = node.get("ref")
            if not ref:
                continue
            self.by_ref.pop(ref, None)      # re-insert, so recency is insertion order
            self.by_ref[ref] = {"role": node.get("role", ""), "name": node.get("name", ""),
                                "sel": node.get("sel") or "", "url": url,
                                "secret": bool(node.get("secret"))}
        # A long session on a re-rendering SPA mints refs forever; only the recent ones can
        # still be on a page, and a forgotten ref fails with the same message as a stale one.
        while len(self.by_ref) > self.KEEP:
            self.by_ref.pop(next(iter(self.by_ref)))

    def known(self, ref: str) -> dict | None:
        return self.by_ref.get(str(ref).strip())

    def fallback(self, ref: str) -> str:
        """The path this ref was taken at, for a page that rebuilt its DOM around it."""
        return (self.known(ref) or {}).get("sel", "")

    def describe(self, ref: str) -> str:
        found = self.known(ref)
        if not found:
            return f"ref {ref!r} is not from a snapshot in this session"
        what = " ".join(part for part in (found.get("role"), found.get("name")) if part)
        return f"ref {ref} was {what or 'an unnamed element'}"


def nodes(browser: Any, *, selector: str | None = None, viewport_only: bool = False,
          full: bool = False, max_nodes: int = MAX_NODES) -> dict:
    """The walker's raw answer: the elements worth naming, and the frames behind them."""
    refs = browser.refs
    raw = browser.run_js(SNAPSHOT_JS, {"selector": selector,
                                              "viewportOnly": bool(viewport_only),
                                              "full": bool(full),
                                              "attr": refs.attr, "seqKey": refs.seq_key,
                                              "maxNodes": int(max_nodes)})
    if not isinstance(raw, dict):
        raw = {}
    return {"nodes": raw.get("nodes") or [], "frames": raw.get("frames") or [],
            "elided": int(raw.get("elided") or 0)}


def remember(browser: Any, found: dict) -> dict:
    """Keep this reading's refs on the session, and say whether the page itself changed."""
    previous = browser.refs.last_signature
    moved = bool(browser.refs.url) and browser.refs.url != browser.page.url
    browser.refs.note(browser.page.url, found["nodes"])
    browser.refs.last_signature = {node["ref"]: signature(node)
                                   for node in found["nodes"] if node.get("ref")}
    return {"previous": previous, "moved": moved}


def normalise_mode(mode: Any) -> str:
    """A snapshot mode from whatever a caller wrote, or a one-line error listing the real ones."""
    word = str(mode if mode is not None else "").strip().lower().replace("-", "_").replace(" ", "_")
    if word in MODES:
        return word
    if word in MODE_ALIASES:
        return MODE_ALIASES[word]
    raise ValueError(f"snapshot mode {mode!r} is not one of: {', '.join(MODES)} "
                     f"(interactive = controls with refs, text = just the words, "
                     f"diff = what changed)")


def read(browser: Any, *, mode: str = "interactive", selector: str | None = None,
         viewport_only: bool = False, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Walk the page, remember the refs, and render the text of the snapshot."""
    mode = normalise_mode(mode)
    page = browser.page
    found = nodes(browser, selector=selector, viewport_only=viewport_only,
                  full=mode in ("full", "text"))
    kept = remember(browser, found)
    return render(found["nodes"], mode=mode, max_chars=max_chars, frames=found["frames"],
                  previous=kept["previous"], selector=selector, url=page.url,
                  page_changed=kept["moved"], elided=int(found.get("elided") or 0))


def find(browser: Any, query: str, limit: int = 8, *,
         selector: str | None = None) -> dict:
    """Find what is on the page that matches a description, best first.

    This is openbrowser's `browser_find`, and it is what an agent wants when it knows what it
    is looking for and not where it is - "sign in", "continue as", "accept cookies". Each
    match comes back with the ref that acts on it, so a find is normally followed by an act
    rather than by a snapshot: it is the cheap way to skip the reading.
    """
    found = nodes(browser, selector=selector, full=True)
    remember(browser, found)
    wanted = query.lower().strip()
    # One-letter words are not a search: "a" matches half a page and would let "purchase a
    # yacht" come back with three matches that are all wrong.
    words = [word for word in re.split(r"[^a-z0-9]+", wanted) if len(word) > 1]
    words = [word for word in words if word not in STOPWORDS] or words
    if not words:
        return {"query": query, "found": 0, "matches": [],
                "next_step": "give find something to match on - a word or two of what the "
                             "thing says, like \"sign in\" or \"accept cookies\""}
    scored: list[tuple[float, dict]] = []
    for node in found["nodes"]:
        # A credential's own text is never searchable and never returned, even if the
        # walker somehow handed one up: a match is a place to act, not a place to read.
        name = str(node.get("name") or "")
        role = str(node.get("role") or "")
        value = "" if node.get("secret") else str(node.get("value") or "")
        text = f"{name} {value}".lower().strip()
        # Whole words, not substrings, and the name counts for far more than the role.
        # Searching a logged-in GitHub for "sign in" used to return eight things, all of
        # them wrong, because "in" is inside "link", "main", "banner" and "contentinfo".
        named = sum(1 for word in words if _has_word(text, word))
        roled = sum(1 for word in words if _has_word(role.lower(), word))
        if not named and not roled:
            continue
        score = 0.75 * (named / len(words)) + 0.15 * (roled / len(words))
        if named < len(words) and len(text) > LONG_TEXT:
            # Part of the query, found somewhere inside a paragraph. That is a word the
            # page happens to contain, not the thing the agent is looking for.
            score *= 0.5
        if text == wanted:
            score += 0.40           # that is its name, not merely words it contains
        elif wanted in text:
            score += 0.20           # the whole phrase, in order
        if node.get("ref") and role not in ("heading", "text"):
            # An agent searching for a phrase wants the thing it can act on. "Sign in" on a
            # page with a heading and a "Sign in with Google" button is the button.
            score += 0.25
        if score >= MATCH_FLOOR:
            scored.append((score, node))
    scored.sort(key=lambda pair: (-pair[0], int(pair[1].get("depth") or 0)))
    matches = [{"ref": node.get("ref"), "role": node.get("role"),
                "name": (node.get("name") or "")[:120],
                **({"secret": True} if node.get("secret") else
                   {"value": node["value"]} if node.get("value") else {})}
               for _score, node in scored[:max(1, int(limit))]]
    out: dict = {"query": query, "found": len(matches), "matches": matches}
    if not matches:
        # Saying "nothing here" is worth more than eight near-misses: a model handed a
        # list of plausible refs will click one of them.
        out["next_step"] = ("nothing on this page matches that - not a ranking problem, "
                            "there is no such thing here. Try one word of it, read the "
                            "page with latchkey_snapshot, or check you are on the right "
                            "page (latchkey_open's reply says where you are).")
    return out


def signature(node: dict) -> tuple:
    """What makes a node "different" for diff mode: what it is and what it says, not where."""
    return (node.get("role"), node.get("name"), node.get("value"), node.get("checked"),
            node.get("disabled"), node.get("expanded"), node.get("filled"))


def render(nodes: list[dict], mode: str = "interactive", max_chars: int = DEFAULT_MAX_CHARS,
           frames: list[str] | None = None, previous: dict[str, tuple] | None = None,
           selector: str | None = None, url: str = "", page_changed: bool = False,
           elided: int = 0) -> str:
    """The snapshot as text: one line per thing worth naming, refs where they are usable."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    changed: set[str] = set()
    shown = nodes
    if mode == "diff":
        shown, changed = _changed(nodes, previous or {})

    if mode == "outline":
        lines = _outline(nodes, selector)
    elif mode == "text":
        lines = [node["name"] for node in shown if node.get("name") and not node.get("ref")]
    else:
        lines = [_line(node, bullet="+ " if node.get("ref") in changed else
                       ("  " if mode == "diff" else "- ")) for node in shown]

    body = "\n".join(line for line in lines if line.strip())
    header = _short(url)
    if selector:
        header += f" · {selector}"
    if mode == "diff":
        header += f" · {len(changed)} changed"
        if page_changed:
            header += " (on a new page since the last snapshot, so all of it is new)"
    truncated = False
    budget = max(200, max_chars - len(header) - 240)
    if len(body) > budget:
        truncated = True
        tail = _outline(nodes, selector)[:TRUNCATION_TAIL]
        room = max(0, budget - sum(len(line) + 1 for line in tail) - 160)
        body = (body[:room].rsplit("\n", 1)[0] + "\n"
                + f"[cut at {max_chars} characters: the rest of the page did not fit. That is "
                  f"its shape - read one part of it with selector, or raise max_chars]"
                + "\n" + "\n".join(tail))
    if frames:
        body += "\n" + "\n".join(
            f"[frame {index}] {src} - refs are not in frames: latchkey_frames lists them, "
            f"latchkey_frame_text reads one" for index, src in enumerate(frames, 1))
    if not body.strip():
        body = "(nothing to name: the page may still be loading, or everything in it is hidden)"
    if elided:
        body += (f"\n[the page has more than {MAX_NODES} things on it; {elided} were not "
                 f"walked. Pass a selector to read one part of it]")
    counted = sum(1 for node in nodes if node.get("ref"))
    note = f"\n({counted} refs{' · truncated' if truncated else ''})" if counted else ""
    return f"{header}\n{body}{note}"


def _short(url: str, limit: int = 96) -> str:
    """A URL short enough to open a snapshot with: a data: URL is not a header."""
    return short_url(url, limit) if url else "the page"


def _line(node: dict, bullet: str = "- ") -> str:
    role = node.get("role") or "text"
    if role == "heading" and node.get("level"):
        role = f"heading{node['level']}"
    name = node.get("name") or ""
    bits = [f'{role} "{name}"' if name else role]
    if node.get("secret"):
        bits.append(f'filled ({node["filled"]} chars, hidden)' if node.get("filled")
                    else "secret empty")
    elif node.get("value"):
        bits.append(f'value="{node["value"]}"')
    elif node.get("placeholder"):
        bits.append(f'placeholder="{node["placeholder"]}"')
    for flag in ("checked", "disabled", "required"):
        if node.get(flag):
            bits.append(flag)
    if "expanded" in node:
        bits.append("expanded" if node["expanded"] else "collapsed")
    if node.get("shadow"):
        bits.append("in-shadow-dom")
    tag = f' [ref={node["ref"]}]' if node.get("ref") else ""
    return f'{"  " * int(node.get("depth") or 0)}{bullet}{" ".join(bits)}{tag}'


def _changed(nodes: list[dict], previous: dict[str, tuple]) -> tuple[list[dict], set[str]]:
    """Only what is different since the last snapshot: the cheap read after an action."""
    changed: set[str] = set()
    keep: list[dict] = []
    for node in nodes:
        ref = node.get("ref")
        if not ref:
            continue
        if previous.get(ref) != signature(node):
            changed.add(ref)
            keep.append(node)
    return keep, changed


def _outline(nodes: list[dict], selector: str | None) -> list[str]:
    """The page's shape: the parts of it that can be read on their own, with a count.

    The count walks the parent chain the walker recorded rather than counting by depth,
    because depth alone puts a sibling's controls inside the part next to it.
    """
    by_ref = {node["ref"]: node for node in nodes if node.get("ref")}

    def inside(node: dict, ref: str) -> bool:
        parent = node.get("parent")
        for _ in range(16):
            if not parent:
                return False
            if parent == ref:
                return True
            parent = (by_ref.get(parent) or {}).get("parent")
        return False

    parts: list[str] = []
    for node in nodes:
        if node.get("role") not in ("main", "navigation", "form", "dialog", "banner",
                                    "contentinfo", "complementary", "region", "search", "table"):
            continue
        controls = sum(1 for other in nodes
                       if other.get("ref") and other is not node
                       and inside(other, node["ref"]))
        name = node.get("name") or ""
        hint = (f'{node.get("role")} "{name}"' if name else str(node.get("role"))) \
            + f' · {controls} controls'
        if node.get("sel"):
            hint += f' · selector:"{node["sel"]}"'
        parts.append("  " + hint)
    if not parts:
        parts = ["  (no landmarks on this page: read it whole, or pass a selector)"]
    if selector:
        parts.insert(0, f"  (inside {selector})")
    return parts
