"""What fraction of real sites does this actually work on? Item 2's coverage run.

Read-only: one navigation per site, host-filtered session, no clicks. Writes a
markdown table to stdout (and `--json` for the raw rows).

    python3 examples/verify_sites.py                # the curated list
    python3 examples/verify_sites.py --top 20       # top 20 hosts by cookie count
    python3 examples/verify_sites.py --modes        # also compare inject vs clone
    python3 examples/verify_sites.py --json
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import json
import time

from latchkey import SessionRegistry, SessionSpec
from latchkey import cookies as ck

# Real sites a person is likely to be signed in to. The point is coverage of the
# *kind* of site an agent is asked to drive, not a cookie-count leaderboard.
CURATED = [
    "github.com", "gitlab.com", "amazon.com", "reddit.com", "youtube.com",
    "chatgpt.com", "claude.ai", "gemini.google.com", "x.com", "linkedin.com",
    "instagram.com", "netflix.com", "spotify.com", "discord.com", "notion.so",
    "figma.com", "dropbox.com", "steamcommunity.com", "coursera.org",
    "canvas.uw.edu", "my.uw.edu", "stackoverflow.com", "news.ycombinator.com",
]

# Hosts that are plumbing rather than destinations: SSO, CDNs, ad networks.
NOISE = ("doubleclick", "googleadservices", "google-analytics", "googlesyndication",
         "gstatic", "googleapis", "cloudflare", "akamai", "sentry", "segment",
         "scorecardresearch", "criteo", "taboola", "amazon-adsystem", "bing.com")


def top_hosts(limit: int) -> list[str]:
    counts: dict[str, int] = {}
    for cookie in ck.load(None):
        host = cookie.host.lstrip(".")
        if any(word in host for word in NOISE):
            continue
        counts[host] = counts.get(host, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return [host for host, _ in ordered[:limit]]


def probe(registry, host: str, mode: str = "inject") -> dict:
    name = f"{host}-{mode}"
    row = {"host": host, "mode": mode, "url": f"https://{host}/"}
    started = time.time()
    try:
        registry.get(name, spec=SessionSpec(mode=mode, host=host, label=name))
        state = registry.require(name).submit(lambda b: b.goto(f"https://{host}/"))
        report = registry.require(name).submit(lambda b: b.report.as_dict())
        row.update(verdict=state.verdict, title=state.title, tab=state.tab,
                   wall=(state.wall or {}).get("sentence", ""),
                   cookies=report["accepted"], loaded=report["loaded"],
                   jar=state.host_cookies,
                   profiles=report["profiles"], blocked=state.blocked,
                   login_prompt=state.login_prompt, text=len(state.text),
                   head=" ".join(state.text.split())[:60])
    except Exception as exc:  # noqa: BLE001
        row.update(verdict="ERROR", error=f"{type(exc).__name__}: {exc}"[:120])
    finally:
        registry.close(name)
    row["seconds"] = round(time.time() - started, 1)
    return row


def jar_of(value) -> str:
    """The cookie count the verdict actually used; '?' when the session could not know.

    The `cookies` column is what the mode *loaded* - for a clone, the whole profile's jar,
    which is not the question the verdict asks. This is that question: does this session
    hold anything for this host (see `Driver.host_cookies`).
    Under `clone` it is the count that now comes back from Chrome, one query per page.
    """
    return "?" if value is None else str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=0,
                        help="use the N hosts holding the most cookies instead")
    parser.add_argument("hosts", nargs="*",
                        help="specific hosts to check (default: the curated list)")
    parser.add_argument("--modes", action="store_true",
                        help="also run each site in clone mode and compare")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(hosts=[])
    args = parser.parse_args()

    hosts = args.hosts or (top_hosts(args.top) if args.top else CURATED)
    registry = SessionRegistry()
    rows = []
    try:
        for host in hosts:
            row = probe(registry, host)
            rows.append(row)
            print(f"{host:24} {row['verdict']:11} {row.get('cookies', 0):>4} cookies  "
                  f"jar {jar_of(row.get('jar')):>4}  "
                  f"{row.get('title', row.get('error', ''))[:38]!r}", flush=True)
            if args.modes and row["verdict"] != "ERROR":
                clone = probe(registry, host, mode="clone")
                row["clone_verdict"] = clone["verdict"]
                row["clone_cookies"] = clone.get("cookies", 0)
                row["clone_jar"] = clone.get("jar")
                row["clone_error"] = clone.get("error", "")
                same = "same" if clone["verdict"] == row["verdict"] else "DIFFERS"
                note = f"  {clone['error']}" if clone.get("error") else ""
                print(f"{'':24} clone: {clone['verdict']:11} "
                      f"{clone.get('cookies', 0):>4} cookies  "
                      f"jar {jar_of(clone.get('jar')):>4}  ({same}){note}", flush=True)
    finally:
        registry.close_all()

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("\n| site | inject | clone | cookies | jar | title | wall |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for row in rows:
            clone = row.get("clone_verdict", "-")
            if row.get("clone_error"):
                clone = f"ERROR: {row['clone_error'][:60]}"
            jar = jar_of(row.get("jar"))
            if "clone_verdict" in row:
                jar = f"{jar}/{jar_of(row.get('clone_jar'))}"
            print(f"| {row['host']} | {row['verdict']} | {clone} | "
                  f"{row.get('cookies', 0)} | {jar} | {row.get('title', '')[:30]} | "
                  f"{row.get('wall', '')[:40]} |")
        signed_in = sum(1 for r in rows if r["verdict"] == "logged-in")
        logged_out = sum(1 for r in rows if r["verdict"] == "logged-out")
        unclear = sum(1 for r in rows if r["verdict"] == "unclear")
        blocked = sum(1 for r in rows if r["verdict"] == "blocked")
        challenged = sum(1 for r in rows if r["verdict"] == "challenged")
        errors = sum(1 for r in rows if r["verdict"] == "ERROR")
        print(f"\n{len(rows)} sites: {signed_in} logged-in, {logged_out} logged-out, "
              f"{unclear} unclear, {blocked} blocked, {challenged} challenged, {errors} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
