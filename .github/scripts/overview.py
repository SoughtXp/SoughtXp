#!/usr/bin/env python3
"""Generate the Overview section of the profile README.

Two independent modes (selected by the MODE env var):

  MODE=activity  -> refresh the "recent activity" card. Runs automatically on a
                    cron. Picks up *any* public GitHub activity (commits, PRs,
                    issues, repo create/delete, stars, forks, releases,
                    comments, activity on other people's repos, ...) plus
                    profile/bio edits (detected via the user's updated_at). The
                    card lists the most recent activities with a relative time
                    and stamps when it was refreshed.

  MODE=mark      -> mark TODAY on the manual calendar. Runs on manual dispatch
                    only. Appends today's date to assets/overview/days.json and
                    redraws the calendar. Calendar colours: a marked day is
                    green, a past unmarked day is red, today and future days use
                    a neutral colour.

Both modes write light + dark SVGs. Designed to run inside a GitHub Action with
GITHUB_TOKEN, but the activity feed also works unauthenticated (lower rate
limit).
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

# Palette. Calendar states: marked / past / future, plus neutral text accents.
# Marked-day colour matches the mascot's eye (assets/hello.png).
MARKED_LIGHT = "#2ea043"
MARKED_DARK = "#39d353"
RED_LIGHT = "#d0d7de"
RED_DARK = "#21262d"
FUTURE_LIGHT = "#ebedf0"
FUTURE_DARK = "#30363d"
NEUTRAL_LIGHT = "#121212"
NEUTRAL_DARK = "#ffffff"
BG_LIGHT = "#f4f5f7"
BG_DARK = "#1a1a1a"

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
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-profile-overview",
    }
    if TOKEN:
        hdr["Authorization"] = f"Bearer {TOKEN}"
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
# Recent activity (automatic)
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
        size = payload.get("size") or len(commits)
        if commits:
            detail = commits[-1]["message"].splitlines()[0]
            sha = commits[-1]["sha"]
            url = f"{repo_url}/commit/{sha}"
        else:
            count = size or ""
            detail = f"{count} commit{'s' if count != 1 else ''}" if count else ""
            ref = payload.get("ref", "")
            branch = ref.split("/")[-1] if ref else ""
            url = f"{repo_url}/tree/{branch}" if branch else repo_url
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


def fetch_recent_activities(limit=10):
    """Most recent activities of any kind, plus a profile-edit check.

    Returns a list of (label, detail, url, when_iso), newest first.
    """
    cands = []  # (datetime, (label, detail, url, when))

    # 1) Any event (commits, PRs, issues, stars, forks, releases, ...).
    #    /user/events requires authentication and includes private-repo events.
    #    Falls back to the public feed when no token is available.
    try:
        if TOKEN:
            try:
                events = _req(f"{API}/user/events?per_page=100")
            except urllib.error.HTTPError as e:
                print(f"WARNING: /user/events failed ({e.code}), falling back to public feed", flush=True)
                events = _req(f"{API}/users/{USER}/events?per_page=100")
        else:
            events = _req(f"{API}/users/{USER}/events?per_page=100")
    except urllib.error.HTTPError as e:
        print(f"WARNING: events API failed ({e.code})", flush=True)
        events = []
    for ev in events:
        when = ev.get("created_at", "")
        label, detail, url = _describe(ev)
        cands.append((_parse(when), (label, detail, url, when)))

    # 2) Profile / bio edits: only add if the user explicitly has a bio set
    #    and updated_at is recent (within 24h), to avoid noise from API calls
    #    bumping updated_at automatically.
    try:
        user = _req(f"{API}/users/{USER}")
        updated = user.get("updated_at", "")
        bio = user.get("bio") or ""
        if updated and bio:
            dt = _parse(updated)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age < 86400:
                cands.append((_parse(updated),
                              ("Updated profile", bio,
                               f"https://github.com/{USER}", updated)))
    except urllib.error.HTTPError:
        pass

    cands.sort(key=lambda c: c[0], reverse=True)
    out, seen = [], set()
    for _, payload in cands:
        key = (payload[0], payload[2])
        if key in seen:
            continue
        seen.add(key)
        out.append(payload)
        if len(out) >= limit:
            break
    return out


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


def render_recent(events, dark):
    """Card listing the most recent activities, newest first."""
    width = 420
    header_h, line_h = 30, 24
    rows = events[:10]
    n = max(1, len(rows))
    height = header_h + n * line_h + 12

    bg = BG_DARK if dark else BG_LIGHT
    muted = "#c9c9c9" if dark else "#3a3a3a"
    strong = NEUTRAL_DARK if dark else NEUTRAL_LIGHT
    stamp = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Recent activity">',
        '<style>.t{font-family:Inter,Segoe UI,Helvetica,Arial,sans-serif}</style>',
        f'<rect x="0.5" y="0.5" width="{width-1}" height="{height-1}" rx="10" '
        f'fill="{bg}" stroke="{strong}" stroke-opacity="0.35"/>',
        f'<text class="t" x="16" y="20" font-size="13" font-weight="700" '
        f'fill="{strong}">Recent activity</text>',
        f'<text class="t" x="{width-16}" y="20" font-size="10" fill="{muted}" '
        f'text-anchor="end">updated {stamp}</text>',
    ]
    if not rows:
        parts.append(
            f'<text class="t" x="32" y="{header_h + 18}" font-size="12.5" '
            f'fill="{muted}">No recent activity</text>')
    else:
        for i, (label, detail, _url, when) in enumerate(rows):
            y = header_h + 16 + i * line_h
            parts.append(
                f'<circle cx="20" cy="{y-4}" r="3" fill="{strong}"/>')
            parts.append(
                f'<text class="t" x="32" y="{y}" font-size="12.5" '
                f'font-weight="600" fill="{strong}">{esc(label[:38])}</text>')
            parts.append(
                f'<text class="t" x="{width-16}" y="{y}" font-size="11" '
                f'fill="{muted}" text-anchor="end">{esc(human_when(when))}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def render_calendar(days, dark):
    """GitHub-style grid for the whole current year.

    Colours: a marked day is green, a past unmarked day is red, today and every
    future day use a neutral colour.
    """
    cell, gap, pad, top = 11, 3, 16, 50
    now = datetime.now(timezone.utc).date()
    year = now.year
    jan1 = date(year, 1, 1)
    dec31 = date(year, 12, 31)
    # Grid starts on the Sunday on/just before Jan 1 (GitHub layout)
    start = jan1 - timedelta(days=(jan1.weekday() + 1) % 7)
    total_days = (dec31 - start).days + 1
    weeks = (total_days + 6) // 7

    width = pad * 2 + weeks * (cell + gap)
    height = top + 7 * (cell + gap) + pad
    marked = MARKED_DARK if dark else MARKED_LIGHT
    red = RED_DARK if dark else RED_LIGHT
    future = FUTURE_DARK if dark else FUTURE_LIGHT
    text = "#c9c9c9" if dark else "#3a3a3a"
    strong = NEUTRAL_DARK if dark else NEUTRAL_LIGHT

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
            if d < jan1 or d > dec31:
                continue
            y = top + dow * (cell + gap)
            iso = d.isoformat()
            if iso in days:
                fill = marked
            elif d < now:
                fill = red
            else:
                fill = future
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
    events = fetch_recent_activities(10)
    write("assets/overview/last_light.svg", render_recent(events, dark=False))
    write("assets/overview/last_dark.svg", render_recent(events, dark=True))
    link = events[0][2] if events else f"https://github.com/{USER}"
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
    if MODE == "mark":
        run_mark()
    else:
        run_activity()


if __name__ == "__main__":
    main()
