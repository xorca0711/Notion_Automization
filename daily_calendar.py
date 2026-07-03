#!/usr/bin/env python3
"""
Daily calendar generator for the 일상 메모 / 기한 만료 workflow.

Flow, once per run (intended to fire at 03:00 KST):
  1. Compute today's date in Asia/Seoul (e.g. "5/30").
  2. Find the most recent prior daily page (yesterday) to carry from.
  3. Copy Must + Forward + Want + Reminder forward, each into its own
     section; flag Reminder items whose dates fall within
     DEADLINE_WINDOW_DAYS (기한 만료). Done and Daily 1-line start
     empty each day (Daily 1-line = a one-line reflection for later
     retrospectives; it never carries forward).
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
import time
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
# When a block claims to have children but the read returns none (sync lag or a
# throttled response), retry this many times, sleeping BACKOFF*attempt seconds.
CHILD_READ_RETRIES = 3
CHILD_READ_BACKOFF = 1.5
# Sections copied into the next day, each preserved under its own heading.
# Done is intentionally excluded, so the new day starts with an empty Done.
CARRY_SECTIONS = ("Must", "Forward", "Want", "Reminder")
# "Daily 1-line" is listed here only so the parser recognizes it as a section
# header when reading yesterday's page; it is deliberately NOT in CARRY_SECTIONS,
# so it always starts blank (a fresh one-line reflection each day).
SECTION_ORDER  = ("Must", "Forward", "Reminder", "Want", "Done", "Daily 1-line")

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


def _block_text(b: dict) -> str:
    t = b["type"]
    rich = b.get(t, {}).get("rich_text")
    if isinstance(rich, list):
        return "".join(s.get("plain_text", "") for s in rich).strip()
    return ""


def read_tree(block_id: str) -> list[dict]:
    """Recursively read a page/block's children into nodes:
    {type, text, children:[...]}. Preserves nesting depth.

    If a block reports has_children but the recursive read comes back empty
    (a sign of Notion sync lag or a throttled/partial API response), retry a
    few times with a short backoff before accepting the empty result — this
    guards against nested items silently going missing at read time."""
    nodes, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = requests.get(f"{API}/blocks/{block_id}/children", headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        for b in data["results"]:
            t = b["type"]
            if t == "child_page":               # don't descend into sub-pages
                continue
            node = {"type": t, "text": _block_text(b), "children": []}
            if b.get("has_children"):
                children = read_tree(b["id"])
                for attempt in range(CHILD_READ_RETRIES):
                    if children:
                        break
                    time.sleep(CHILD_READ_BACKOFF * (attempt + 1))
                    children = read_tree(b["id"])
                node["children"] = children
            nodes.append(node)
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return nodes


def _is_meaningful(node: dict) -> bool:
    """Drop empty placeholder items (e.g. a bare '2.' with no text/children)."""
    return bool(node["text"]) or any(_is_meaningful(c) for c in node["children"])


def section_label(text: str) -> str | None:
    """Return the section name if this line is a section header, else None.
    Tolerates **bold** markers and the '⚠ N일 이내 마감' suffix on Reminder."""
    t = text.strip().strip("*").strip()
    for lab in SECTION_ORDER:
        if t.lower() == lab.lower() or (
            t.lower().startswith(lab.lower()) and len(t) <= len(lab) + 25
        ):
            return lab
    return None


def sectionize_tree(nodes: list[dict]) -> dict[str, list[dict]]:
    """Group top-level nodes under their section header, keeping each
    item's nested subtree intact."""
    sections: dict[str, list[dict]] = {s: [] for s in SECTION_ORDER}
    current = None
    for n in nodes:
        if n["type"] == "divider":
            continue
        lab = section_label(n["text"]) if n["text"] else None
        if lab:
            current = lab
            continue
        if current and _is_meaningful(n):
            sections[current].append(n)
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
    - Carry-forward source: the single most recent page dated < today, searched
      across HOME, the Calendar archive, AND legacy parents. Searching Calendar
      matters because yesterday's page may already have been archived there.
    - home_stale_pages: daily pages still at HOME with date < today (to be moved).
    """
    home_children = list_child_pages(HOME_PAGE_ID)
    home_dated = []
    for c in home_children:
        d = parse_title_date(c["title"], today.year)
        if d and d < today:
            home_dated.append((d, c))
    home_dated.sort(key=lambda x: x[0])
    home_stale = [c for _, c in home_dated]

    # Gather carry-forward candidates from every location, not just HOME.
    candidates = list(home_dated)
    for parent in [ARCHIVE_PAGE_ID, *LEGACY_PARENTS]:
        for c in list_child_pages(parent):
            d = parse_title_date(c["title"], today.year)
            if d and d < today:
                candidates.append((d, c))
    candidates.sort(key=lambda x: x[0])
    prior_id = candidates[-1][1]["id"] if candidates else None
    return prior_id, home_stale


# ----------------------------------------------------------------------------
# Page builder  — native blocks, preserving nested sub-items at any depth
# ----------------------------------------------------------------------------
LIST_TYPES = ("numbered_list_item", "bulleted_list_item", "to_do", "toggle", "paragraph")

def _heading(text: str) -> dict:
    return {"type": "heading_3", "text": text, "children": []}

def _divider_node() -> dict:
    return {"type": "divider", "text": "", "children": []}


def _placeholder() -> dict:
    """An empty numbered-list item, so a cleared section shows a ready '1.'."""
    return {"type": "numbered_list_item", "text": "", "children": [], "placeholder": True}


# A previously-flagged reminder looks like "⚠ <text>  [D-3]". Strip the flag
# back off before re-evaluating, so markers don't pile up day after day.
_FLAG_RE = re.compile(r"^\s*⚠\s*")
_DSUFFIX_RE = re.compile(r"\s*\[D-\d+\]\s*$")

def _strip_flag(text: str) -> str:
    return _DSUFFIX_RE.sub("", _FLAG_RE.sub("", text)).strip()


def _subtree_text(node: dict) -> str:
    """All text in a node and its descendants (for deadline scanning)."""
    return node["text"] + " " + " ".join(_subtree_text(c) for c in node["children"])


def build_top_level(today: dt.date, prev: dict[str, list[dict]] | None) -> list[dict]:
    """Build the ordered list of top-level nodes (headings, dividers, item
    subtrees). Item nodes keep their nested children."""
    prev = prev or {s: [] for s in SECTION_ORDER}

    # Reminder: re-flag deadlines within the window (기한 만료 surfacing).
    reminders, flagged = [], 0
    for node in prev.get("Reminder", []):
        node["text"] = _strip_flag(node["text"])
        d = deadline_within_window(_subtree_text(node), today)
        if d is not None:
            node["text"] = f"⚠ {node['text']}  [D-{d}]"
            flagged += 1
        reminders.append(node)

    out: list[dict] = []

    def section(label: str, items: list[dict], suffix: str = ""):
        if out:                              # divider before every section but the first
            out.append(_divider_node())
        out.append(_heading(label + suffix))
        out.extend(items)

    # Carry-forward rules:
    #   Must (today) = leftover Must (yesterday) + Forward (yesterday)
    #       — Forward items are "forwarded" into tomorrow's must-dos.
    #   Forward (today) starts empty, ready for new items added during the day.
    #   Want / Reminder carry into their own sections; Done starts empty.
    must_items = prev.get("Must", []) + prev.get("Forward", [])

    section("Must", must_items)
    section("Forward", [_placeholder()])      # emptied; one ready '1.' to type into
    section("Reminder", reminders,
            suffix=(f"  ⚠ {DEADLINE_WINDOW_DAYS}일 이내 마감 {flagged}건" if flagged else ""))
    section("Want", prev.get("Want", []))
    section("Done", [_placeholder()])         # starts empty, with one ready '1.'
    section("Daily 1-line", [_placeholder()]) # bottom of page; blank each day for
                                              # a one-line reflection (retrospective fuel)
    return out


def _shallow_block(node: dict) -> dict:
    """Convert one node to a Notion block dict WITHOUT its children
    (children are appended in a later request)."""
    t = node["type"]
    if t == "divider":
        return {"object": "block", "type": "divider", "divider": {}}
    if t == "heading_3":
        return {"object": "block", "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": node["text"]}}]}}
    if t not in LIST_TYPES:
        t = "numbered_list_item"             # coerce anything unexpected
    rich = [{"type": "text", "text": {"content": node["text"]}}] if node["text"] else []
    payload = {"rich_text": rich}
    if t == "to_do":
        payload["checked"] = False
    return {"object": "block", "type": t, t: payload}


def _append_children(block_id: str, nodes: list[dict]) -> None:
    """Append nodes under block_id, then recurse for any node with children.
    Handles arbitrary nesting depth (one request per level)."""
    nodes = [n for n in nodes
             if n["type"] == "divider" or n["text"] or n["children"] or n.get("placeholder")]
    if not nodes:
        return
    specs = [_shallow_block(n) for n in nodes]
    created = []
    for i in range(0, len(specs), 100):      # API caps at 100 blocks per request
        r = requests.patch(f"{API}/blocks/{block_id}/children",
                            headers=HEADERS, json={"children": specs[i:i + 100]})
        r.raise_for_status()
        created.extend(r.json()["results"])
    for node, cb in zip(nodes, created):
        if node["children"]:
            _append_children(cb["id"], node["children"])


def create_page(parent_id: str, title: str, top_level: list[dict]) -> str:
    """Create an empty titled page, then append the full nested tree into it."""
    payload = {
        "parent": {"page_id": parent_id},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
    }
    r = requests.post(f"{API}/pages", headers=HEADERS, json=payload)
    r.raise_for_status()
    page = r.json()
    _append_children(page["id"], top_level)
    return page["url"]


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
        prev_sections = sectionize_tree(read_tree(prior_id)) if prior_id else None
        url = create_page(HOME_PAGE_ID, today_title, build_top_level(today, prev_sections))
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
