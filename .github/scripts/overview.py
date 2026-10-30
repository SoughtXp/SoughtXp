#!/usr/bin/env python3
"""Generate the Overview section of the profile README.

MODE=activity  → refresh the activity card (auto, cron + push trigger).
                 The card shows recent events and a PRESENCE counter (N/365).
MODE=mark      → mark today in days.json and refresh the card.
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, date, timedelta

USER  = os.environ.get("PROFILE_USER", "SoughtXp")
MODE  = os.environ.get("MODE", "activity").strip().lower()
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
API   = "https://api.github.com"

DAYS_FILE = "assets/overview/days.json"

# ── Palette ───────────────────────────────────────────────────────────────────
BG_L    = "#ffffff"
BG_D    = "#0d0d0d"
TEXT_L  = "#1f2328"
TEXT_D  = "#e6edf3"
MUTED_L = "#57606a"
MUTED_D = "#8b949e"
LINE_L  = "#d0d7de"
LINE_D  = "#30363d"

FONT = "font-family: 'SF Mono', 'Fira Code', 'Roboto Mono', monospace"


# ── GitHub API ────────────────────────────────────────────────────────────────
def _req(url):
    hdr = {"Accept": "application/vnd.github+json",
           "User-Agent": f"{USER}-overview"}
    if TOKEN:
        hdr["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _parse(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


# ── Event → display ───────────────────────────────────────────────────────────
def _short(full):
    return full.split("/")[-1] if "/" in full else full


def _describe(ev):
    t    = ev.get("type", "")
    full = ev.get("repo", {}).get("name", "")
    repo = _short(full)
    pl   = ev.get("payload", {})
    rurl = f"https://github.com/{full}"

    if t == "PushEvent":
        commits = pl.get("commits") or []
        size    = pl.get("size") or len(commits)
        ref     = pl.get("ref", "")
        branch  = ref.split("/")[-1] if ref else ""
        if commits:
            msg = commits[-1]["message"].splitlines()[0]
            url = f"{rurl}/commit/{commits[-1]['sha']}"
        else:
            msg = f"{size or 1} commit(s)"
            url = f"{rurl}/tree/{branch}" if branch else rurl
        return (repo, msg, url)

    if t == "PullRequestEvent":
        pr     = pl.get("pull_request", {})
        merged = pr.get("merged", False)
        action = pl.get("action", "")
        verb   = "merged PR" if merged else f"{action} PR"
        return (f"{repo} — {verb}", pr.get("title", ""), pr.get("html_url", rurl))

    if t == "IssuesEvent":
        issue  = pl.get("issue", {})
        action = pl.get("action", "")
        return (f"{repo} — issue {action}", issue.get("title", ""),
                issue.get("html_url", rurl))

    if t == "IssueCommentEvent":
        issue = pl.get("issue", {})
        return (f"{repo} — commented", issue.get("title", ""),
                pl.get("comment", {}).get("html_url", rurl))

    if t == "CreateEvent":
        return (f"{repo} — created {pl.get('ref_type','')}", pl.get("ref") or "", rurl)

    if t == "DeleteEvent":
        return (f"{repo} — deleted {pl.get('ref_type','')}", pl.get("ref", ""), rurl)

    if t == "WatchEvent":
        return (f"starred {full}", "", rurl)

    if t == "ForkEvent":
        fk = pl.get("forkee", {})
        return (f"forked {full}", "", fk.get("html_url", rurl))

    if t == "ReleaseEvent":
        rel = pl.get("release", {})
        return (f"{repo} — release", rel.get("name") or rel.get("tag_name", ""),
                rel.get("html_url", rurl))

    return (f"{repo} — {t.replace('Event','').lower()}", "", rurl)


# ── Fetch events ──────────────────────────────────────────────────────────────
def fetch_recent(limit=10):
    raw = []
    try:
        if TOKEN:
            try:
                raw = _req(f"{API}/user/events?per_page=100")
                print(f"fetched /user/events → {len(raw)} events", flush=True)
            except urllib.error.HTTPError as e:
                print(f"WARNING: /user/events → {e.code}, falling back", flush=True)
                raw = _req(f"{API}/users/{USER}/events?per_page=100")
        else:
            raw = _req(f"{API}/users/{USER}/events?per_page=100")
    except urllib.error.HTTPError as e:
        print(f"WARNING: events API → {e.code}", flush=True)

    print(f"processing {len(raw)} raw events", flush=True)

    out, seen = [], set()
    for ev in raw:
        when               = ev.get("created_at", "")
        label, detail, url = _describe(ev)
        key                = (label, url)
        if key in seen:
            continue
        seen.add(key)
        out.append((label, detail, url, when))
        if len(out) >= limit:
            break
    return out


# ── Days helpers ──────────────────────────────────────────────────────────────
def load_days():
    try:
        with open(DAYS_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_days(days):
    os.makedirs(os.path.dirname(DAYS_FILE), exist_ok=True)
    with open(DAYS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(days), f, indent=0)
        f.write("\n")


def mark_today(days):
    today = datetime.now(timezone.utc).date().isoformat()
    days.add(today)
    return days, today


def presence_counter(days):
    """Return (marked, total_days_in_year) for the current year."""
    year  = datetime.now(timezone.utc).year
    total = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    n     = sum(1 for d in days if d.startswith(str(year)))
    return n, total, year


# ── SVG helpers ───────────────────────────────────────────────────────────────
def esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _cut(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def age(iso):
    s = int((datetime.now(timezone.utc) - _parse(iso)).total_seconds())
    if s < 60:    return "just now"
    if s < 3600:  return f"{s // 60}m"
    if s < 86400: return f"{s // 3600}h"
    return f"{s // 86400}d"


# ── Activity card ─────────────────────────────────────────────────────────────
def render_activity(events, days, dark):
    W      = 480
    PAD    = 20
    HDR    = 32
    ROW    = 38
    FOOT   = 28   # footer row for presence counter
    rows   = events[:10]
    n      = max(1, len(rows))
    H      = HDR + n * ROW + FOOT + 8

    bg     = BG_D    if dark else BG_L
    tx     = TEXT_D  if dark else TEXT_L
    mu     = MUTED_D if dark else MUTED_L
    ln     = LINE_D  if dark else LINE_L
    stamp  = datetime.now(timezone.utc).strftime("%b %d · %H:%M UTC")

    marked, total, year = presence_counter(days)

    o = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-label="Recent activity">',
        f'<style>text {{ {FONT}; }}</style>',
        f'<rect width="{W}" height="{H}" fill="{bg}"/>',
        f'<line x1="{PAD}" y1="{HDR}" x2="{W-PAD}" y2="{HDR}" stroke="{ln}" stroke-width="1"/>',
        f'<text x="{PAD}" y="20" font-size="11" fill="{mu}" letter-spacing="2">RECENT ACTIVITY</text>',
        f'<text x="{W-PAD}" y="20" font-size="10" fill="{mu}" text-anchor="end">{esc(stamp)}</text>',
    ]

    if not rows:
        o.append(f'<text x="{PAD}" y="{HDR+22}" font-size="12" fill="{mu}">no recent activity</text>')
    else:
        for i, (label, detail, _url, when) in enumerate(rows):
            base = HDR + i * ROW
            mid  = base + ROW // 2

            o.append(
                f'<text x="{PAD}" y="{mid - 4}" font-size="12" font-weight="700" '
                f'fill="{tx}">{esc(_cut(label, 44))}</text>')

            if detail:
                o.append(
                    f'<text x="{PAD}" y="{mid + 10}" font-size="11" '
                    f'fill="{mu}">{esc(_cut(detail, 56))}</text>')

            o.append(
                f'<text x="{W-PAD}" y="{mid + 4}" font-size="10" '
                f'fill="{mu}" text-anchor="end">{esc(age(when))}</text>')

            if i < n - 1:
                sep = base + ROW
                o.append(
                    f'<line x1="{PAD}" y1="{sep}" x2="{W-PAD}" y2="{sep}" '
                    f'stroke="{ln}" stroke-width="0.5"/>')

    # footer: presence counter
    foot_y = HDR + n * ROW
    o.append(f'<line x1="{PAD}" y1="{foot_y}" x2="{W-PAD}" y2="{foot_y}" stroke="{ln}" stroke-width="0.5"/>')
    o.append(
        f'<text x="{PAD}" y="{foot_y + 18}" font-size="10" fill="{mu}" letter-spacing="1">'
        f'PRESENCE {year}</text>')
    o.append(
        f'<text x="{W-PAD}" y="{foot_y + 18}" font-size="11" font-weight="700" '
        f'fill="{tx}" text-anchor="end">{marked} / {total}</text>')

    o.append("</svg>")
    return "\n".join(o)


# ── I/O ───────────────────────────────────────────────────────────────────────
def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    print(f"wrote {path}", flush=True)


# ── Entry points ──────────────────────────────────────────────────────────────
def run_activity():
    days   = load_days()
    events = fetch_recent(10)
    write("assets/overview/last_light.svg", render_activity(events, days, dark=False))
    write("assets/overview/last_dark.svg",  render_activity(events, days, dark=True))
    link = events[0][2] if events else f"https://github.com/{USER}"
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"last_url={link}\n")
    print(f"last_url={link}", flush=True)


def run_mark():
    days = load_days()
    days, today = mark_today(days)
    save_days(days)
    events = fetch_recent(10)
    write("assets/overview/last_light.svg", render_activity(events, days, dark=False))
    write("assets/overview/last_dark.svg",  render_activity(events, days, dark=True))
    print(f"marked {today} ({len(days)} total)", flush=True)


def main():
    if MODE == "mark":
        run_mark()
    else:
        run_activity()


if __name__ == "__main__":
    main()
