#!/usr/bin/env python3
"""Generate the Overview section of the profile README.

Two independent modes (selected by the MODE env var):

  MODE=activity  -> refresh the "last activity" card. Runs automatically on a
                    cron. Picks up *any* public GitHub activity (commits, PRs,
                    issues, repo create/delete, stars, forks, releases,
                    comments, activity on other people's repos, ...) plus
                    profile/bio edits (detected via the user's updated_at).

  MODE=mark      -> mark TODAY on the manual calendar. Runs on manual dispatch
                    only. Appends today's date to assets/overview/days.json and
                    redraws the calendar. The calendar is a plain day tracker:
                    a day you marked is green, everything else is empty.

Both modes write light + dark SVGs. Designed to run inside a GitHub Action with
GITHUB_TOKEN.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, date, timedelta

USER = os.environ.get("PROFILE_USER", "SoughtXp")
MODE = os.environ.get("MODE", "activity").strip().lower()
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
API = "https://api.github.com"

DAYS_FILE = "assets/overview/days.json"

# Palette (matches the rest of the profile)
GREEN = "#25D366"
BG_DARK = "#1a1a1a"
EMPTY_LIGHT = "#ebedf0"
EMPTY_DARK = "#20242b"

# Human-readable labels for the GitHub event types
EVENT_LABELS = {
    "PushEvent": "Pushed commits",
    "PullRequestEvent": "Pull request",
    "PullRequestReviewEvent": "Reviewed a pull request",
    "PullRequestReviewCommentEvent": "Commented on a pull request",
    "IssuesEvent": "Issue",
    "IssueCommentEvent": "Commented on an issue",
    "CreateEvent": "Created",
    "DeleteEvent": "Deleted",
    "ForkEvent": "Forked a repo",
    "WatchEvent": "Starred a repo",
    "ReleaseEvent": "Published a release",
    "CommitCommentEvent": "Commented on a commit",
    "PublicEvent": "Made a repo public",
    "MemberEvent": "Updated collaborators",
    "GollumEvent": "Edited the wiki",
}


def _req(url, data=None, headers=None):
    hdr = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-profile-overview",
    }
    if headers:
        hdr.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=hdr)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _parse(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Last activity (automatic)
# --------------------------------------------------------------------------- #

def _describe(ev):
    """Turn a raw event into (label, short_detail, url)."""
    t = ev.get("type", "")
    repo = ev.get("repo", {}).get("name", "")
    payload = ev.get("payload", {})
    repo_url = f"https://github.com/{repo}"
    label = EVENT_LABELS.get(t, t.replace("Event", ""))

    if t == "PushEvent":
        commits = payload.get("commits") or []
        detail = commits[-1]["message"].splitlines()[0] if commits else repo
        sha = commits[-1]["sha"] if commits else None
        url = f"{repo_url}/commit/{sha}" if sha else repo_url
        return (f"Pushed to {repo}", detail, url)
    if t == "PullRequestEvent":
        pr = payload.get("pull_request", {})
        action = payload.get("action", "")
        return (f"PR {action} in {repo}", pr.get("title", ""),
                pr.get("html_url", repo_url))
    if t in ("IssuesEvent",):
        issue = payload.get("issue", {})
        action = payload.get("action", "")
        return (f"Issue {action} in {repo}", issue.get("title", ""),
                issue.get("html_url", repo_url))
    if t == "IssueCommentEvent":
        issue = payload.get("issue", {})
        return (f"Commented in {repo}", issue.get("title", ""),
                payload.get("comment", {}).get("html_url", repo_url))
    if t == "WatchEvent":
        return (f"Starred {repo}", "", repo_url)
    if t == "ForkEvent":
        forkee = payload.get("forkee", {})
        return (f"Forked {repo}", "", forkee.get("html_url", repo_url))
    if t == "CreateEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref") or repo
        return (f"Created {ref_type} in {repo}", ref, repo_url)
    if t == "DeleteEvent":
        return (f"Deleted {payload.get('ref_type','')} in {repo}",
                payload.get("ref", ""), repo_url)
    if t == "ReleaseEvent":
        rel = payload.get("release", {})
        return (f"Released in {repo}", rel.get("name") or rel.get("tag_name", ""),
                rel.get("html_url", repo_url))
    if t == "PublicEvent":
        return (f"Made {repo} public", "", repo_url)
    # Generic fallback for any other event type
    return (f"{label} in {repo}" if repo else label, "", repo_url)


def fetch_last_activity():
    """Most recent activity of any kind, plus a profile-edit check.

    Returns (label, detail, url, when_iso) or None.
    """
    best = None  # (datetime, (label, detail, url, when))

    def consider(dt, payload):
        nonlocal best
        if best is None or dt > best[0]:
            best = (dt, payload)

    # 1) Any public event (commits, PRs, issues, stars, forks, releases,
    #    activity on other people's repos, ...). Private-repo events are not
    #    exposed by this feed.
    try:
        events = _req(f"{API}/users/{USER}/events?per_page=100")
    except urllib.error.HTTPError:
        events = []
    for ev in events:
        when = ev.get("created_at", "")
        label, detail, url = _describe(ev)
        consider(_parse(when), (label, detail, url, when))

    # 2) Profile / bio edits: the user object carries an updated_at that bumps
    #    when the profile changes.
    try:
        user = _req(f"{API}/users/{USER}")
        updated = user.get("updated_at", "")
        if updated:
            consider(_parse(updated),
                     ("Updated profile", user.get("bio") or "",
                      f"https://github.com/{USER}", updated))
    except urllib.error.HTTPError:
        pass

    return best[1] if best else None


# --------------------------------------------------------------------------- #
# Manual calendar (day tracker)
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def human_when(iso):
    dt = _parse(iso)
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def render_last(ev, dark):
    width, height = 420, 70
    bg = BG_DARK if dark else "#f4f5f7"
    text = "#c9c9c9" if dark else "#3a3a3a"
    strong = "#ffffff" if dark else "#121212"
    if ev:
        label, detail, url, when = ev
        when_h = human_when(when)
        detail = esc(detail[:48] + ("…" if len(detail) > 48 else "")) if detail else when_h
        line2 = when_h if detail != when_h else ""
        label = esc(label[:52])
    else:
        label, detail, line2 = "No recent activity", "", ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{label}">'
        '<style>.t{font-family:Inter,Segoe UI,Helvetica,Arial,sans-serif}</style>'
        f'<rect x="0.5" y="0.5" width="{width-1}" height="{height-1}" rx="10" '
        f'fill="{bg}" stroke="{GREEN}" stroke-opacity="0.5"/>'
        f'<circle cx="20" cy="22" r="4" fill="{GREEN}"/>'
        f'<text class="t" x="34" y="26" font-size="13" font-weight="700" '
        f'fill="{GREEN}">{label}</text>'
        f'<text class="t" x="20" y="46" font-size="13" fill="{strong}">{detail}</text>'
        f'<text class="t" x="20" y="62" font-size="11" fill="{text}">{line2}</text>'
        '</svg>')


def render_calendar(days, dark):
    """GitHub-style grid for the current year; a marked day is green."""
    cell, gap, pad, top = 11, 3, 16, 50
    year = datetime.now(timezone.utc).year
    jan1 = date(year, 1, 1)
    # Grid starts on the Sunday on/just before Jan 1 (GitHub layout)
    start = jan1 - timedelta(days=(jan1.weekday() + 1) % 7)
    last = datetime.now(timezone.utc).date()
    total_days = (last - start).days + 1
    weeks = (total_days + 6) // 7

    width = pad * 2 + weeks * (cell + gap)
    height = top + 7 * (cell + gap) + pad
    empty = EMPTY_DARK if dark else EMPTY_LIGHT
    text = "#c9c9c9" if dark else "#3a3a3a"
    strong = "#ffffff" if dark else "#121212"

    marked_this_year = sum(1 for d in days if d.startswith(str(year)))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Days I showed up in {year}">',
        '<style>.t{font-family:Inter,Segoe UI,Helvetica,Arial,sans-serif}</style>',
        f'<text class="t" x="{pad}" y="22" font-size="14" font-weight="700" '
        f'fill="{strong}">Days I showed up in {year}</text>',
        f'<text class="t" x="{pad}" y="40" font-size="12" fill="{text}">'
        f'{marked_this_year} day{"s" if marked_this_year != 1 else ""} marked</text>',
    ]
    for wi in range(weeks):
        x = pad + wi * (cell + gap)
        for dow in range(7):
            d = start + timedelta(days=wi * 7 + dow)
            if d < jan1 or d > last:
                continue
            y = top + dow * (cell + gap)
            iso = d.isoformat()
            fill = GREEN if iso in days else empty
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" '
                f'ry="2" fill="{fill}"><title>{iso}</title></rect>')
    parts.append("</svg>")
    return "\n".join(parts)


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    print(f"wrote {path}")


# --------------------------------------------------------------------------- #

def run_activity():
    ev = fetch_last_activity()
    write("assets/overview/last_light.svg", render_last(ev, dark=False))
    write("assets/overview/last_dark.svg", render_last(ev, dark=True))
    link = ev[2] if ev else f"https://github.com/{USER}"
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"last_url={link}\n")
    print(f"last_url={link}")


def run_mark():
    days = load_days()
    days, today = mark_today(days)
    save_days(days)
    write("assets/overview/calendar_light.svg", render_calendar(days, dark=False))
    write("assets/overview/calendar_dark.svg", render_calendar(days, dark=True))
    print(f"marked {today} ({len(days)} day(s) total)")


def main():
    if not TOKEN and MODE == "activity":
        sys.exit("GITHUB_TOKEN / GH_TOKEN not set")
    if MODE == "mark":
        run_mark()
    else:
        run_activity()


if __name__ == "__main__":
    main()
