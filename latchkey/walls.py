"""Who stopped us, and on what evidence.

`detect.py` answers *whether* a page is a wall. That is enough to keep an agent
from reading a challenge page as a signed-out account, and not enough to do
anything about it: "blocked" on Reddit, ChatGPT and a bank look identical, while
the three need different responses. A Cloudflare managed challenge usually clears
itself if the client waits and looks like a real browser; a DataDome hard block
does not; an IP-reputation block cannot be fixed from inside the browser at all.

So attribution is deliberately *evidence-based and cheap*: it reads only things
the session already has - the document response's status and headers, the cookie
names Chrome holds for the host, the page's title and text, and the srcs of the
iframes the probe already walks. No extra round trip, no probing.

Evidence is graded, because "there is a `cf-ray` header" is true of most of the
web:

* **strong** - a vendor cookie, a challenge marker in the DOM, a distinctive phrase.
  These can name a vendor and decide what kind of wall it is.
* **weak** - a response header, a vendor URL. Useful for naming the vendor in a
  log, never enough on its own to call a page blocked: half the internet answers
  `server: cloudflare`, and a 403 from a normal app behind Cloudflare is a
  permission error, not a bot wall.

Nothing here is authoritative. These signatures come from the vendors' own docs
and from the wild, they drift, and a site can front one vendor with another. A
`Wall` with `confidence="low"` is a hint, not a verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# CSS selectors worth asking about on every page read. Kept in one place because
# detect.py injects them into the probe, and the probe is the only thing that can
# see inside a challenge: the iframe's *contents* are cross-origin and unreachable,
# but its `src` is not.
MARKERS: tuple[str, ...] = (
    "iframe[src*='challenges.cloudflare.com']",       # Turnstile / managed challenge
    "iframe[src*='recaptcha']",                       # reCAPTCHA v2, v3, Enterprise
    "iframe[src*='hcaptcha.com']",
    "iframe[src*='arkoselabs.com']",                  # FunCaptcha
    "iframe[src*='captcha.px-cdn.net']",              # HUMAN / PerimeterX
    "iframe[src*='captcha-delivery.com']",            # DataDome
    "iframe[src*='bm-verify']",                       # Akamai
    "#px-captcha",                                    # HUMAN "press and hold"
    "#funcaptcha",
    "#challenge-form",                                # Cloudflare interstitial
    "#cf-challenge-running",
    "#challenge-stage",
    "#challenge-container",                           # AWS WAF
    "#g-recaptcha-response",
    "[data-sitekey].cf-turnstile",
    "script[src*='/cdn-cgi/challenge-platform/']",
    "script[src*='challenge.js']",                    # AWS WAF
    "script[src*='ips.js']",                          # Kasada
    "script[src*='/akam/']",                          # Akamai sensor
    "script[src*='perimeterx']",
    "input[name='g-recaptcha-response']",
)

# Text a wall shows. Lowercased, substring-matched against title + body. These are
# the vendors' own words on their own error pages, which is why they read oddly
# ("Request unsuccessful. Incapsula incident ID") - that phrase is the signature.
TEXT: tuple[tuple[str, str, str], ...] = (
    # (vendor, phrase, kind)
    ("cloudflare", "just a moment", "challenge"),
    ("cloudflare", "checking your browser before accessing", "challenge"),
    ("cloudflare", "verifying you are human", "challenge"),
    ("cloudflare", "verify you are human", "challenge"),
    ("cloudflare", "checking if the site connection is secure", "challenge"),
    ("cloudflare", "enable javascript and cookies to continue", "challenge"),
    ("cloudflare", "needs to review the security of your connection", "challenge"),
    ("cloudflare", "attention required! | cloudflare", "block"),
    ("cloudflare", "sorry, you have been blocked", "block"),
    ("cloudflare", "you are unable to access", "block"),
    ("cloudflare", "error 1020", "block"),
    ("recaptcha", "protected by recaptcha", "challenge"),
    ("recaptcha", "unusual traffic from your computer network", "challenge"),
    ("hcaptcha", "hcaptcha", "challenge"),
    ("datadome", "please enable js and disable any ad blocker", "block"),
    ("akamai", "reference #18", "block"),
    ("imperva", "request unsuccessful. incapsula", "block"),
    ("imperva", "incapsula incident id", "block"),
    ("human", "please verify you are a human", "challenge"),
    ("human", "press & hold", "challenge"),
    ("aws-waf", "human verification", "challenge"),
    ("sucuri", "sucuri website firewall - access denied", "block"),
    ("f5", "the requested url was rejected", "block"),
    ("vercel", "vercel security checkpoint", "challenge"),
    ("kasada", "kasada", "challenge"),
    ("queue-it", "you are now in line", "queue"),
    ("queue-it", "you are in the queue", "queue"),
)

# Cookie names Chrome is holding for the host. A tenant cookie from a bot vendor is
# the strongest cheap signal there is: it says the vendor has already talked to this
# client, whatever page it is showing now.
COOKIES: tuple[tuple[str, str], ...] = (
    ("cloudflare", "cf_clearance"),
    ("cloudflare", "__cf_bm"),
    ("cloudflare", "__cfruid"),
    ("cloudflare", "cf_chl_"),
    ("cloudflare", "_cfuvid"),
    ("recaptcha", "_grecaptcha"),
    ("datadome", "datadome"),
    ("datadome", "dd_cookie_test_"),
    ("akamai", "_abck"),
    ("akamai", "bm_sz"),
    ("akamai", "ak_bmsc"),
    ("akamai", "bm_sv"),
    ("akamai", "ak_bmsec"),
    ("imperva", "visid_incap_"),
    ("imperva", "incap_ses_"),
    ("imperva", "reese84"),
    ("human", "_px"),
    ("human", "_pxhd"),
    ("human", "pxvid"),
    ("human", "_pxde"),
    ("aws-waf", "aws-waf-token"),
    ("kasada", "x-kpsdk-ct"),
    ("kasada", "x-kpsdk-cd"),
)

# Response headers, graded. `server` alone never decides anything.
HEADERS: tuple[tuple[str, str, str], ...] = (
    ("cloudflare", "cf-mitigated", "strong"),
    ("cloudflare", "cf-chl-", "strong"),
    ("cloudflare", "cf-ray", "weak"),
    ("cloudflare", "cf-cache-status", "weak"),
    ("datadome", "x-datadome", "strong"),
    ("datadome", "x-dd-b", "strong"),
    ("akamai", "x-akamai-", "weak"),
    ("imperva", "x-iinfo", "strong"),
    ("aws-waf", "x-amzn-waf-action", "strong"),
    ("sucuri", "x-sucuri-id", "strong"),
    ("f5", "x-wa-info", "strong"),
    ("kasada", "x-kpsdk-", "strong"),
    ("fastly", "x-fastly-", "weak"),
)

# Hosts whose presence in a request URL means a widget is being served even before
# any element is inspected. Weak: these are also seen on ordinary pages that merely
# embed a form.
URL_HOSTS: tuple[tuple[str, str], ...] = (
    ("cloudflare", "challenges.cloudflare.com"),
    ("cloudflare", "/cdn-cgi/challenge-platform/"),
    ("recaptcha", "google.com/recaptcha"),
    ("hcaptcha", "hcaptcha.com"),
    ("arkose", "arkoselabs.com"),
    ("human", "px-cdn.net"),
    ("datadome", "captcha-delivery.com"),
    ("akamai", "bm-verify"),
    ("kasada", "ips.js"),
    ("queue-it", "queue-it.net"),
)

KINDS = ("block", "challenge", "rate_limit", "queue")

LABELS = {"cloudflare": "Cloudflare", "recaptcha": "Google reCAPTCHA",
          "hcaptcha": "hCaptcha", "datadome": "DataDome", "akamai": "Akamai Bot Manager",
          "imperva": "Imperva/Incapsula", "human": "HUMAN/PerimeterX",
          "aws-waf": "AWS WAF", "sucuri": "Sucuri", "f5": "F5 BIG-IP ASM",
          "fastly": "Fastly", "kasada": "Kasada", "arkose": "Arkose Labs",
          "vercel": "Vercel", "queue-it": "Queue-it"}

DENIAL_STATUSES = (401, 403, 429, 503)
BARE_BODY = 200          # a denial with less than this much text is a bare denial


@dataclass
class Wall:
    """What we think stopped us, and the evidence that says so."""
    vendor: str = ""                 # cloudflare, datadome, ...; "" when unknown
    label: str = ""                  # Cloudflare, DataDome, ...
    kind: str = ""                   # block | challenge | rate_limit | queue | ""
    confidence: str = ""             # high | medium | low | ""
    reasons: list[str] = field(default_factory=list)
    status: int | None = None
    strong: bool = False             # did anything certain name this vendor?

    @property
    def known(self) -> bool:
        return bool(self.vendor)

    def sentence(self) -> str:
        """One line for a log or an agent: who, how sure, why."""
        if not self.known:
            return ""
        bits = [self.label or self.vendor]
        if self.kind:
            bits.append(self.kind.replace("_", " "))
        if self.status:
            bits.append(f"HTTP {self.status}")
        if self.confidence:
            bits.append(self.confidence)
        head = " ".join(bits)
        why = f" - {self.reasons[0]}" if self.reasons else ""
        return f"{head}{why}"

    def as_dict(self) -> dict:
        return {"vendor": self.vendor, "label": self.label, "kind": self.kind,
                "confidence": self.confidence, "status": self.status,
                "reasons": list(self.reasons), "strong": self.strong,
                "sentence": self.sentence()}


def _hits(hay: str, needle: str) -> bool:
    return bool(hay) and bool(needle) and needle in hay


def attribute(*, headers: dict | None = None, cookie_names: list | tuple | None = None,
              title: str = "", body: str = "", urls: list | tuple | None = None,
              selectors: list | tuple | None = None, status: int | None = None) -> Wall:
    """Name the wall from evidence the session already has, or return an empty Wall."""
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    cookies = [str(c).lower() for c in (cookie_names or [])]
    urls = [str(u).lower() for u in (urls or [])]
    selectors = [str(s).lower() for s in (selectors or [])]
    hay = f"{title or ''} {body or ''}".lower()
    text = (body or "").strip()

    reasons: dict[str, list[str]] = {}
    strength: dict[str, bool] = {}
    kind: dict[str, str] = {}

    def note(vendor: str, why: str, *, strong: bool, wall_kind: str = "") -> None:
        bucket = reasons.setdefault(vendor, [])
        if why not in bucket:
            bucket.append(why)
        strength[vendor] = strength.get(vendor, False) or strong
        # A refusal outranks a widget: a page that says "you have been blocked" is a
        # block even if a challenge element sits somewhere in its markup. A cookie,
        # though, only ever names the vendor - what kind of wall this is gets decided
        # after every piece of evidence is in (see below), because a `datadome` cookie
        # arrives on ordinary visits and a 403 with one is not automatically a refusal.
        if wall_kind in ("block", "rate_limit"):
            kind[vendor] = wall_kind
        elif wall_kind and not kind.get(vendor):
            kind[vendor] = wall_kind

    for name in cookies:
        for vendor, needle in COOKIES:
            if needle in name:
                # A vendor cookie names the company, not its mood: `datadome` and `_abck`
                # are set on ordinary visits to sites that use them. The kind of wall has
                # to come from the page or from the status.
                note(vendor, f"cookie {needle}", strong=True)
    for selector in selectors:
        for vendor, marker in (("cloudflare", "cloudflare.com"), ("cloudflare", "cdn-cgi"),
                              ("cloudflare", "challenge-form"),
                              ("cloudflare", "cf-challenge-running"),
                              ("cloudflare", "challenge-stage"),
                              ("cloudflare", "cf-turnstile"),
                              ("recaptcha", "recaptcha"), ("hcaptcha", "hcaptcha"),
                              ("arkose", "arkoselabs"), ("arkose", "funcaptcha"), ("human", "px-captcha"),
                              ("human", "px-cdn"), ("human", "perimeterx"),
                              ("datadome", "captcha-delivery"),
                              ("akamai", "bm-verify"), ("akamai", "/akam/"),
                              ("aws-waf", "challenge.js"),
                              ("aws-waf", "challenge-container"),
                              ("kasada", "ips.js")):
            if _hits(selector, marker):
                note(vendor, f"marker {marker}", strong=True, wall_kind="challenge")
    for vendor, phrase, wall_kind in TEXT:
        if _hits(hay, phrase):
            # A long phrase is a sentence only this vendor prints; a short one
            # ("hcaptcha", "kasada") is a hint wherever it turns up.
            note(vendor, f'page says "{phrase}"', strong=len(phrase) > 12,
                 wall_kind=wall_kind)
    for vendor, host in URL_HOSTS:
        if any(host in url for url in urls):
            note(vendor, f"host {host}", strong=False)
    for vendor, header, weight in HEADERS:
        for key in headers:
            matched = key.startswith(header) if header.endswith("-") else (
                key == header or header in key)
            if matched:
                note(vendor, f"header {header}", strong=(weight == "strong"))
    for key, value in headers.items():
        if key == "server":
            low = value.lower()
            if "cloudflare" in low:
                note("cloudflare", "server: cloudflare", strong=False)
            elif "sucuri" in low:
                note("sucuri", "server: sucuri", strong=False)
            elif "varnish" in low or "fastly" in low:
                note("fastly", f"server: {value}", strong=False)

    if not reasons:
        return Wall(status=status)

    def rank(vendor: str) -> tuple:
        return (1 if strength.get(vendor) else 0, len(reasons[vendor]),)

    best = max(reasons, key=rank)
    is_strong = strength.get(best, False)
    wall_kind = kind.get(best, "")
    if not wall_kind and is_strong and status in DENIAL_STATUSES:
        # Named by a cookie or a marker and the site answered with a denial: a refusal,
        # unless something said otherwise first.
        wall_kind = "rate_limit" if status == 429 else "block"
    confidence = ""
    if is_strong:
        # A challenge widget or a vendor cookie is a fact, not an inference; the
        # denial status says the site meant it.
        confidence = "high" if status in DENIAL_STATUSES or len(reasons[best]) > 1 else "medium"
    elif status in DENIAL_STATUSES and len(text) < BARE_BODY:
        # A denial with nothing on it: no text, no widget, just a header and a
        # status. Not certain enough to name the vendor's mood, certain enough to
        # call it a wall.
        wall_kind = "rate_limit" if status == 429 else "block"
        confidence = "low"
    else:
        confidence = "low"
        wall_kind = ""
    return Wall(vendor=best, label=LABELS.get(best, best), kind=wall_kind,
                confidence=confidence, reasons=reasons[best][:4], status=status,
                strong=is_strong)


def from_page(probe: dict, *, headers: dict | None = None, cookie_names=None,
              status: int | None = None) -> Wall:
    """`attribute` over the shape `detect.py`'s probe returns."""
    return attribute(headers=headers, cookie_names=cookie_names, status=status,
                     title=probe.get("title") or "", body=probe.get("body") or "",
                     urls=probe.get("urls") or (), selectors=probe.get("selectors") or ())
