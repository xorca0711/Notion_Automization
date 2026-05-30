#!/usr/bin/env python3
"""
Daily calendar generator for the 일상 메모 / 기한 만료 workflow.

Flow, once per run (intended to fire at 03:00 KST):
  1. Compute today's date in Asia/Seoul (e.g. "5/30").
  2. Find the most recent prior daily page (yesterday) to carry from.
  3. Copy Must + Forward + Want + Reminder forward, each into its own
     section; flag Reminder items whose dates fall within
     DEADLINE_WINDOW_DAYS (기한 만료). Done starts empty each day.
  4. CREATE today's page at the 일상 메모 top level (HOME_PAGE_ID).
  5. MOVE every now-passed daily page sitting at the 일상 메모 level down
     into 기한 만료 → Calendar (ARCHIVE_PAGE_ID), filing the day away.

This is the page+sections (Option A) design: carry-forward and deadline
detection are best-effort text parsing. Tune the regexes / section names
below to match how you write.

Setup:
  pip install requests
  export NOTION_TOKEN="secret_xxx"   # an internal integration token
  # Share BOTH 일상 메모 and 기한 만료 with that integration in Notion
  # (••• > Connections). The Calendar page inherits access from 기한 만료.
"""

import os
import re
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]

HOME_PAGE_ID    = "a9788871-eddc-40f9-8c6c-f3c1edd18c9a"   # 일상 메모  (new pages created here)
ARCHIVE_PAGE_ID = "35d15161-6b44-80b0-9fe3-c8224955c573"   # 기한 만료 → Calendar (passed days filed here)
# Extra places to look for a prior daily page on the first run, before the
# steady state where yesterday's page lives at HOME_PAGE_ID:
LEGACY_PARENTS  = ["1bb15161-6b44-80bc-841d-dda26d2ace2e"]  # 지나간 일들, 기한 만료

TZ = ZoneInfo("Asia/Seoul")
DEADLINE_WINDOW_DAYS = 7
# Sections copied into the next day, each preserved under its own heading.
# Done is intentionally excluded, so the new day starts with an empty Done.
CARRY_SECTIONS = ("Must", "Forward", "Want", "Reminder")
SECTION_ORDER  = ("Must", "Forward", "Want", "Reminder", "Done")

API = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
# The move endpoint requires a newer API version than the rest of the calls.
MOVE_HEADERS = {**HEADERS, "Notion-Version": "2025-09-03"}


# ----------------------------------------------------------------------------
# Date helpers
# ----------------------------------------------------------------------------
def title_for(date: dt.date) -> str:
    """Match your no-leading-zero titles: 5/9, 5/30."""
    return f"{date.month}/{date.day}"


def parse_title_date(title: str, year: int) -> dt.date | None:
    """Parse titles like '5/29', '5/29 할 것', '5/23-4' (takes first date)."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})", title)
    if not m:
        return None
    try:
        return dt.date(year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Notion API helpers
# ----------------------------------------------------------------------------
def list_child_pages(page_id: str) -> list[dict]:
    """Return child_page blocks [{id, title}] of a page."""
    out, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = requests.get(f"{API}/blocks/{page_id}/children", headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        for b in data["results"]:
            if b["type"] == "child_page":
                out.append({"id": b["id"], "title": b["child_page"]["title"]})
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return out


def read_blocks_text(page_id: str) -> list[str]:
    """Flatten a page's top-level blocks into plain text lines."""
    lines, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = requests.get(f"{API}/blocks/{page_id}/children", headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        for b in data["results"]:
            t = b["type"]
            rich = b.get(t, {}).get("rich_text")
            if isinstance(rich, list):
                txt = "".join(s.get("plain_text", "") for s in rich).strip()
                if t in ("numbered_list_item", "bulleted_list_item"):
                    txt = "• " + txt
                lines.append(txt)
            elif t == "divider":
                lines.append("---")
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return lines


def sectionize(lines: list[str]) -> dict[str, list[str]]:
    """Group lines under their section label (Must/Forward/Want/Reminder/Done)."""
    sections: dict[str, list[str]] = {s: [] for s in SECTION_ORDER}
    current = None
    label_re = re.compile(r"^\**\s*(Must|Forward|Want|Reminder|Done)\b", re.I)
    for ln in lines:
        m = label_re.match(ln)
        if m:
            current = m.group(1).capitalize()
            continue
        if current and ln and ln != "---":
            item = ln.lstrip("• ").strip()
            if item:
                sections[current].append(item)
    return sections


def deadline_within_window(text: str, today: dt.date) -> int | None:
    """If the line mentions a date within the window, return days-until; else None."""
    best = None
    for mm, dd in re.findall(r"(\d{1,2})\s*[./]\s*(\d{1,2})", text):
        try:
            d = dt.date(today.year, int(mm), int(dd))
        except ValueError:
            continue
        if d < today:                       # past dates: guess next year
            try:
                d = dt.date(today.year + 1, int(mm), int(dd))
            except ValueError:
                continue
        delta = (d - today).days
        if 0 <= delta <= DEADLINE_WINDOW_DAYS:
            best = delta if best is None else min(best, delta)
    return best


def find_prior_daily(today: dt.date) -> tuple[str | None, list[dict]]:
    """
    Return (id_of_most_recent_prior_daily_page, home_stale_pages).
    - Searches HOME first, then LEGACY_PARENTS, for the latest page dated < today.
    - home_stale_pages: daily pages at HOME with date < today (these get moved).
    """
    home_children = list_child_pages(HOME_PAGE_ID)
    home_dated = []
    for c in home_children:
        d = parse_title_date(c["title"], today.year)
        if d and d < today:
            home_dated.append((d, c))
    home_dated.sort(key=lambda x: x[0])
    home_stale = [c for _, c in home_dated]

    candidates = list(home_dated)
    if not candidates:                       # first-run fallback to legacy locations
        for parent in LEGACY_PARENTS:
            for c in list_child_pages(parent):
                d = parse_title_date(c["title"], today.year)
                if d and d < today:
                    candidates.append((d, c))
    candidates.sort(key=lambda x: x[0])
    prior_id = candidates[-1][1]["id"] if candidates else None
    return prior_id, home_stale


# ----------------------------------------------------------------------------
# Page builder
# ----------------------------------------------------------------------------
def build_markdown(today: dt.date, prev: dict[str, list[str]] | None) -> str:
    prev = prev or {s: [] for s in SECTION_ORDER}

    # Must / Forward / Want copied forward verbatim under their own headings.
    must    = prev.get("Must", [])
    forward = prev.get("Forward", [])
    want    = prev.get("Want", [])

    # Reminder copied forward, with deadline flagging (기한 만료 surfacing).
    reminders, flagged = [], 0
    for item in prev.get("Reminder", []):
        d = deadline_within_window(item, today)
        if d is not None:
            reminders.append(f"⚠ {item}  [D-{d}]")
            flagged += 1
        else:
            reminders.append(item)

    def numbered(items):
        return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)) if items else ""

    hdr = "**Reminder**" + (f"  ⚠ {DEADLINE_WINDOW_DAYS}일 이내 마감 {flagged}건" if flagged else "")
    blocks = [
        "**Must**\n" + numbered(must),
        "\n\n---\n\n**Forward**\n" + numbered(forward),
        "\n\n---\n\n**Want**\n" + numbered(want),
        "\n\n---\n\n" + hdr + "\n" + numbered(reminders),
        "\n\n---\n\n**Done**\n",          # Done always starts empty
    ]
    return "".join(blocks)


def create_page(parent_id: str, title: str, markdown: str) -> str:
    """Create a child page (one block per line, dividers honored)."""
    children = []
    for raw in markdown.split("\n"):
        if raw.strip() == "---":
            children.append({"object": "block", "type": "divider", "divider": {}})
            continue
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": ([{"type": "text", "text": {"content": raw}}] if raw else [])},
        })
    payload = {
        "parent": {"page_id": parent_id},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": children,
    }
    r = requests.post(f"{API}/pages", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()["url"]


def move_page(page_id: str, new_parent_id: str) -> None:
    """Move a page under a new parent page via the dedicated move endpoint."""
    payload = {"parent": {"type": "page_id", "page_id": new_parent_id}}
    r = requests.post(f"{API}/pages/{page_id}/move", headers=MOVE_HEADERS, json=payload)
    r.raise_for_status()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    today = dt.datetime.now(TZ).date()
    today_title = title_for(today)

    prior_id, home_stale = find_prior_daily(today)

    # 1) Create today's page at the 일상 메모 level (idempotent).
    home_today = [c for c in list_child_pages(HOME_PAGE_ID)
                  if parse_title_date(c["title"], today.year) == today]
    if home_today:
        print(f"Page for {today_title} already exists at 일상 메모. Skipping create.")
    else:
        prev_sections = sectionize(read_blocks_text(prior_id)) if prior_id else None
        url = create_page(HOME_PAGE_ID, today_title, build_markdown(today, prev_sections))
        print(f"Created {today_title} at 일상 메모: {url}")

    # 2) File every now-passed daily page at 일상 메모 into 기한 만료 → Calendar.
    for c in home_stale:
        try:
            move_page(c["id"], ARCHIVE_PAGE_ID)
            print(f"Moved {c['title']} → Calendar")
        except requests.HTTPError as e:
            print(f"Could not move {c['title']}: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())