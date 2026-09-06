"""Opt-in tracker reminders. No Notion writes occur during --dry-run."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import uuid
from zoneinfo import ZoneInfo

import requests

API = "https://api.notion.com/v1"
TZ = ZoneInfo("Asia/Seoul")
MARKER = "https://github.com/xorca0711/Notion_Automization#managed-reminders-v1"
KINDS = {"Deadline": 7, "Event": 7, "Period": 7, "Exam": 14}
COMPLETE = {"완료", "Done", "Complete", "Completed", "취소", "Cancelled", "Canceled"}


def plain(rich: list[dict]) -> str:
    return "".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in rich)


def text(value: str, url: str | None = None, bold: bool = False) -> dict:
    result = {"type": "text", "text": {"content": value}, "annotations": {"bold": bold}}
    if url:
        result["text"]["link"] = {"url": url}
    return result


def is_managed_block(block: dict) -> bool:
    return block.get("type") == "callout" and any(
        x.get("text", {}).get("link", {}).get("url", "").split("?")[0] == MARKER
        for x in block.get("callout", {}).get("rich_text", [])
        if x.get("text", {}).get("link")
    )


def sources_from_env() -> list[dict]:
    sources = json.loads(os.environ.get("REMINDER_SOURCES_JSON", "[]"))
    if not isinstance(sources, list):
        raise ValueError("REMINDER_SOURCES_JSON must be a JSON array")
    for source in sources:
        uuid.UUID(source["data_source_id"])
        if not isinstance(source.get("label"), str):
            raise ValueError("Each source needs a label")
    return sources


class Notion:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}",
                        "Notion-Version": "2025-09-03", "Content-Type": "application/json"}

    def request(self, method: str, path: str, *, read_only: bool = False, **kwargs) -> dict:
        # Reads and query POSTs can be retried. An uncertain append must not be
        # blindly retried: the next run reconciles it using the block marker.
        for attempt in range(4):
            try:
                response = requests.request(method, API + path, headers=self.headers,
                                            timeout=30, **kwargs)
            except (requests.Timeout, requests.ConnectionError):
                if not read_only or attempt == 3:
                    raise
                time.sleep(2 ** attempt)
                continue
            if (response.status_code == 429 or
                    (read_only and response.status_code >= 500)) and attempt < 3:
                time.sleep(min(float(response.headers.get("Retry-After", 2 ** attempt)), 30))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Notion request exhausted retries")

    def children(self, block_id: str) -> list[dict]:
        result, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.request("GET", f"/blocks/{block_id}/children", read_only=True, params=params)
            result.extend(data["results"])
            if not data.get("has_more"):
                return result
            cursor = data["next_cursor"]

    def query(self, source: dict) -> list[dict]:
        result, cursor = [], None
        while True:
            payload = {"page_size": 100,
                       "filter": {"property": "Daily Reminder", "checkbox": {"equals": True}}}
            if cursor:
                payload["start_cursor"] = cursor
            data = self.request("POST", f"/data_sources/{source['data_source_id']}/query",
                                read_only=True, json=payload)
            result.extend(data["results"])
            if not data.get("has_more"):
                return result
            cursor = data["next_cursor"]


def _date(value: str | None) -> dt.datetime | dt.date | None:
    if not value:
        return None
    if len(value) == 10:
        return dt.date.fromisoformat(value)
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TZ)
    return stamp.astimezone(TZ)


def day(value: dt.datetime | dt.date) -> dt.date:
    return value.date() if isinstance(value, dt.datetime) else value


def format_date(value: dt.datetime | dt.date) -> str:
    return value.strftime("%Y-%m-%d %H:%M KST" if isinstance(value, dt.datetime) else "%Y-%m-%d")


def _option(prop: dict) -> str:
    return (prop.get(prop.get("type", "select")) or {}).get("name", "")


def item_for(page: dict, label: str, today: dt.date) -> dict | None:
    p = page.get("properties", {})
    if page.get("archived") or page.get("in_trash"):
        return None
    if not p.get("Daily Reminder", {}).get("checkbox") or _option(p.get("Status", {})) in COMPLETE:
        return None
    kind = _option(p.get("Reminder Type", {}))
    if kind not in KINDS:
        raise ValueError("An enabled reminder has no valid Reminder Type")
    start_prop = p.get("Start", {}).get("date") or {}
    end_prop = p.get("End", {}).get("date") or {}
    start = _date(start_prop.get("start"))
    end = _date(end_prop.get("start") or start_prop.get("end")) or start
    if start is None:
        # An interview still awaiting a date must not turn into a made-up alert.
        return None
    if end is not None and day(end) < day(start):
        raise ValueError("Reminder End must not precede Start")
    advance = p.get("Advance Days", {}).get("number")
    if advance is None:
        advance = KINDS[kind]
    if isinstance(advance, bool) or not isinstance(advance, (int, float)) or advance < 0 or int(advance) != advance:
        raise ValueError("Advance Days must be a nonnegative whole number")
    anchor = end if kind == "Deadline" else start
    delta = (day(anchor) - today).days
    if delta > advance:
        return None
    if kind != "Deadline" and today > day(end):
        return None
    if kind == "Deadline" and delta < 0:
        status, priority = f"기한 경과 {abs(delta)}일", 0
    elif delta == 0:
        status, priority = "오늘 마감" if kind == "Deadline" else "오늘 시작", 1
    elif delta > 0:
        status, priority = f"D-{delta}", 3
    else:
        status, priority = f"진행 중 · 종료 D-{(day(end) - today).days}", 2
    title = next((plain(v.get("title", [])) for v in p.values() if v.get("type") == "title"), "")
    notes = plain(p.get("Notes", {}).get("rich_text", []))
    date_label = format_date(anchor)
    if kind != "Deadline" and start != end:
        date_label = f"{format_date(start)} – {format_date(end)}"
    return {"id": page["id"], "title": title, "url": page["url"], "source": label,
            "kind": kind, "status": status, "date": date_label, "notes": notes,
            "sort": (priority, day(anchor).isoformat(), title)}


def collect(client: Notion, sources: list[dict], today: dt.date) -> list[dict]:
    # Finish every source read before writing anything; a failed source must
    # never masquerade as an empty/complete tracker.
    items = {}
    for source in sources:
        for page in client.query(source):
            item = item_for(page, source["label"], today)
            if item:
                items[item["id"]] = item
    return sorted(items.values(), key=lambda x: x["sort"])


def render(items: list[dict], today: dt.date) -> dict:
    children = []
    for item in items:
        rich = [text(item["status"] + " · ", bold=True), text(item["source"] + ": "),
                text(item["title"], item["url"]), text(" — " + item["date"])]
        if item["notes"]:
            rich.append(text("\n" + item["notes"][:240] + ("…" if len(item["notes"]) > 240 else "")))
        children.append({"object": "block", "type": "bulleted_list_item",
                         "bulleted_list_item": {"rich_text": rich}})
    if not children:
        children.append({"object": "block", "type": "paragraph",
                         "paragraph": {"rich_text": [text("표시할 자동 일정이 없습니다.")]}})
    children.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        text("원본에서 날짜·완료 상태를 수정하면 다음 실행에 반영됩니다. 개인 메모는 이 상자 밖에 작성해 주세요.")]}})
    digest = hashlib.sha256(json.dumps(children, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "📅"},
        "rich_text": [text(f"자동 일정 · {today.isoformat()} KST · 원본에서 관리", MARKER + "?v=" + digest)],
        "children": children}}


def _identity(block: dict) -> list[tuple[str, str | None]]:
    return [(x.get("text", {}).get("content", x.get("plain_text", "")),
             (x.get("text", {}).get("link") or {}).get("url"))
            for x in block.get("callout", {}).get("rich_text", [])]


def sync(client: Notion, page_id: str, replacement: dict) -> None:
    blocks = client.children(page_id)
    reminder_heading = None
    owned = []
    inside = False
    for block in blocks:
        kind = block["type"]
        if kind.startswith("heading_"):
            title = plain(block[kind].get("rich_text", []))
            inside = title.strip() == "Reminder" or title.startswith("Reminder  ⚠")
            if inside:
                if reminder_heading is not None:
                    raise ValueError("Multiple Reminder sections; refusing an ambiguous update")
                reminder_heading = block["id"]
        if is_managed_block(block):
            if not inside:
                raise ValueError("Automatic block moved outside Reminder; restore it before syncing")
            owned.append(block)
    if reminder_heading is None:
        raise ValueError("Reminder heading not found; personal page left untouched")
    matching = [b for b in owned if _identity(b) == _identity(replacement)]
    keep_id = matching[0]["id"] if matching else None
    if keep_id is None:
        # One append creates the replacement and its contents together. Delete
        # the previous snapshot only after this succeeds. Future runs also
        # clean duplicates left by an interrupted previous attempt.
        if len(replacement["callout"]["children"]) > 100:
            raise ValueError("Too many daily reminders; reduce enabled items before syncing")
        created = client.request("PATCH", f"/blocks/{page_id}/children",
                                 json={"children": [replacement], "after": reminder_heading})
        keep_id = created["results"][0]["id"]
    for block in owned:
        if block["id"] != keep_id:
            client.request("DELETE", f"/blocks/{block['id']}")
