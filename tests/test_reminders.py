import copy
import datetime as dt
import unittest
from unittest.mock import patch

import daily_calendar as daily
import timeline_reminders as r


def page(kind="Deadline", start="2026-09-14", end=None, advance=None, enabled=True, status="시작 전"):
    return {"id": "task-1", "url": "https://notion.so/task-1", "properties": {
        "이름": {"type": "title", "title": [{"plain_text": "Example task"}]},
        "Status": {"type": "select", "select": {"name": status}},
        "Start": {"date": {"start": start} if start else None},
        "End": {"date": {"start": end} if end else None},
        "Daily Reminder": {"checkbox": enabled}, "Advance Days": {"number": advance},
        "Reminder Type": {"type": "select", "select": {"name": kind}},
        "Notes": {"rich_text": [{"plain_text": "Example notes"}]}}}


class SelectionTests(unittest.TestCase):
    def item(self, p, date):
        return r.item_for(p, "Tracker", dt.date.fromisoformat(date))

    def test_seven_day_boundary(self):
        self.assertIsNone(self.item(page(), "2026-09-06"))
        self.assertEqual(self.item(page(), "2026-09-07")["status"], "D-7")

    def test_deadline_uses_end_and_stays_overdue(self):
        p = page(start="2026-09-01", end="2026-09-14")
        self.assertIsNone(self.item(p, "2026-09-06"))
        self.assertEqual(self.item(p, "2026-09-14")["status"], "오늘 마감")
        self.assertEqual(self.item(p, "2026-09-15")["status"], "기한 경과 1일")

    def test_period_starts_seven_days_early_and_expires(self):
        p = page("Period", "2026-11-04", "2026-11-05")
        self.assertIsNone(self.item(p, "2026-10-27"))
        self.assertEqual(self.item(p, "2026-10-28")["status"], "D-7")
        self.assertIn("진행 중", self.item(p, "2026-11-05")["status"])
        self.assertIsNone(self.item(p, "2026-11-06"))

    def test_exams_fourteen_days(self):
        p = page("Exam", "2026-10-19", "2026-10-23")
        self.assertIsNone(self.item(p, "2026-10-04"))
        self.assertEqual(self.item(p, "2026-10-05")["status"], "D-14")
        self.assertIsNotNone(self.item(p, "2026-10-23"))
        self.assertIsNone(self.item(p, "2026-10-24"))

    def test_custom_three_days(self):
        p = page("Event", advance=3)
        self.assertIsNone(self.item(p, "2026-09-10"))
        self.assertEqual(self.item(p, "2026-09-11")["status"], "D-3")

    def test_separate_interview_action_zero_advance(self):
        # Confirmed interview 9/17 -> contact action dated 9/14.
        p = page(advance=0)
        self.assertIsNone(self.item(p, "2026-09-13"))
        self.assertEqual(self.item(p, "2026-09-14")["status"], "오늘 마감")

    def test_undated_disabled_completed_and_archived(self):
        for p in [page(start=None), page(enabled=False), page(status="완료"),
                  {**page(), "archived": True}, {**page(), "in_trash": True}]:
            self.assertIsNone(self.item(p, "2026-09-14"))
        p = page()
        p["properties"]["Status"] = {"type": "status", "status": {"name": "Done"}}
        self.assertIsNone(self.item(p, "2026-09-14"))

    def test_explicit_year_crosses_new_year(self):
        p = page(start="2027-01-03")
        self.assertEqual(self.item(p, "2026-12-27")["status"], "D-7")
        self.assertEqual(self.item(page(), "2027-01-01")["status"], "기한 경과 109일")

    def test_event_times_use_kst(self):
        p = page("Event", "2026-11-11T09:00:00Z", "2026-11-11T10:00:00Z")
        item = self.item(p, "2026-11-04")
        self.assertIn("18:00 KST", item["date"])
        self.assertIn("19:00 KST", item["date"])
        self.assertIsNone(self.item(p, "2026-11-12"))

    def test_timezone_crossing_midnight(self):
        p = page("Event", "2026-11-10T23:00:00Z")
        self.assertEqual(self.item(p, "2026-11-04")["status"], "D-7")

    def test_estimated_label_and_link_are_preserved(self):
        p = page()
        p["properties"]["이름"]["title"][0]["plain_text"] = "Program (2027 추정)"
        block = r.render([self.item(p, "2026-09-07")], dt.date(2026, 9, 7))
        rich = block["callout"]["children"][0]["bulleted_list_item"]["rich_text"]
        self.assertIn("추정", r.plain(rich))
        self.assertEqual(rich[2]["text"]["link"]["url"], p["url"])

    def test_bad_configuration_fails(self):
        for p in [page(advance=-1), page(advance=1.5), page(kind=""),
                  page(end="2026-09-01")]:
            with self.assertRaises(ValueError):
                self.item(p, "2026-09-07")


class FakeNotion:
    def __init__(self):
        self.blocks = [
            {"id": "reminder-heading", "type": "heading_3", "heading_3": {"rich_text": [r.text("Reminder")]}},
            {"id": "personal", "type": "paragraph", "paragraph": {"rich_text": [r.text("My note")]}},
            {"id": "want-heading", "type": "heading_3", "heading_3": {"rich_text": [r.text("Want")]}}]
        self.calls = []
        self.fail_append = False

    def children(self, _):
        return copy.deepcopy(self.blocks)

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "PATCH":
            if self.fail_append:
                raise RuntimeError("Simulated write failure")
            block = copy.deepcopy(kwargs["json"]["children"][0])
            block["id"] = "generated-" + str(len(self.calls))
            after = kwargs["json"]["after"]
            index = next(i for i, b in enumerate(self.blocks) if b["id"] == after)
            self.blocks.insert(index + 1, block)
            return {"results": [block]}
        if method == "DELETE":
            self.blocks = [b for b in self.blocks if b["id"] != path.split("/")[-1]]
            return {}
        raise AssertionError(method)


class SyncTests(unittest.TestCase):
    def test_rerun_and_changed_date_preserve_personal_content(self):
        client = FakeNotion()
        original = copy.deepcopy(client.blocks)
        block = r.render([], dt.date(2026, 9, 6))
        r.sync(client, "today", block)
        r.sync(client, "today", block)
        self.assertEqual(len(client.calls), 1)
        r.sync(client, "today", r.render([], dt.date(2026, 9, 7)))
        self.assertEqual([b for b in client.blocks if not r.is_managed_block(b)], original)
        self.assertEqual(sum(r.is_managed_block(b) for b in client.blocks), 1)

    def test_replace_failure_keeps_previous_snapshot(self):
        client = FakeNotion()
        r.sync(client, "today", r.render([], dt.date(2026, 9, 6)))
        before = copy.deepcopy(client.blocks)
        client.fail_append = True
        with self.assertRaises(RuntimeError):
            r.sync(client, "today", r.render([], dt.date(2026, 9, 7)))
        self.assertEqual(client.blocks, before)

    def test_retry_cleans_duplicate_after_interruption(self):
        client = FakeNotion()
        block = r.render([], dt.date(2026, 9, 6))
        r.sync(client, "today", block)
        duplicate = copy.deepcopy(client.blocks[1])
        duplicate["id"] = "duplicate"
        client.blocks.insert(2, duplicate)
        r.sync(client, "today", block)
        self.assertEqual(sum(r.is_managed_block(b) for b in client.blocks), 1)

    def test_missing_heading_does_not_write(self):
        client = FakeNotion()
        client.blocks = client.blocks[1:]
        with self.assertRaises(ValueError):
            r.sync(client, "today", r.render([], dt.date(2026, 9, 6)))
        self.assertFalse(client.calls)

    def test_managed_block_not_carried_into_tomorrow(self):
        block = r.render([], dt.date(2026, 9, 6))
        with patch.object(daily.requests, "get") as get:
            get.return_value.json.return_value = {"results": [block], "has_more": False}
            self.assertEqual(daily.read_tree("yesterday"), [])

    def test_manual_nested_carry_forward_stays_intact(self):
        child = {"type": "numbered_list_item", "text": "Subtask", "children": []}
        node = {"type": "numbered_list_item", "text": "Personal reminder", "children": [child]}
        result = daily.build_top_level(dt.date(2026, 9, 6), {"Reminder": [node]})
        self.assertIn(node, result)
        self.assertEqual(node["children"], [child])


class QueryTests(unittest.TestCase):
    def test_http_diagnostic_checks_all_sources_without_leaking_content(self):
        client = r.Notion("fake")
        response = r.requests.Response()
        response.status_code = 404
        error = r.requests.HTTPError("private source URL and response", response=response)
        with patch.object(client, "query", side_effect=error) as query:
            with self.assertRaisesRegex(r.SourceReadError, "source 1: HTTP 404; source 2: HTTP 404") as caught:
                r.collect(client, [{"label": "A"}, {"label": "B"}], dt.date(2026, 9, 7))
            self.assertEqual(query.call_count, 2)
            self.assertNotIn("private", str(caught.exception))

    def test_pagination_and_opt_in_filter(self):
        client = r.Notion("fake")
        with patch.object(client, "request", side_effect=[
            {"results": [page()], "has_more": True, "next_cursor": "next"},
            {"results": [], "has_more": False}]) as request:
            self.assertEqual(len(client.query({"data_source_id": "source"})), 1)
            self.assertTrue(request.call_args_list[0].kwargs["read_only"])
            self.assertEqual(request.call_args.kwargs["json"]["start_cursor"], "next")
            self.assertEqual(request.call_args.kwargs["json"]["filter"]["property"], "Daily Reminder")

    def test_duplicate_source_record_only_rendered_once(self):
        client = r.Notion("fake")
        sources = [{"label": "A"}, {"label": "B"}]
        with patch.object(client, "query", return_value=[page()]):
            self.assertEqual(len(r.collect(client, sources, dt.date(2026, 9, 7))), 1)

    def test_partial_source_read_is_not_accepted(self):
        client = r.Notion("fake")
        with patch.object(client, "query", side_effect=[[page()], RuntimeError("Read failed")]):
            with self.assertRaises(RuntimeError):
                r.collect(client, [{"label": "A"}, {"label": "B"}], dt.date(2026, 9, 7))

    def test_dry_run_never_reads_or_writes_daily_pages(self):
        with patch.object(daily, "NOTION_TOKEN", "fake"), \
             patch("sys.argv", ["daily_calendar.py", "--dry-run", "--date", "2026-09-07"]), \
             patch.object(r, "sources_from_env", return_value=[{"label": "A"}]), \
             patch.object(r, "collect", return_value=[]), \
             patch.object(daily, "find_prior_daily") as prior, \
             patch.object(r, "sync") as sync:
            self.assertEqual(daily.main(), 0)
            prior.assert_not_called()
            sync.assert_not_called()

    def test_failed_read_does_not_replace_existing_snapshot(self):
        with patch.object(daily, "NOTION_TOKEN", "fake"), \
             patch("sys.argv", ["daily_calendar.py"]), \
             patch.object(r, "sources_from_env", return_value=[{"label": "A"}]), \
             patch.object(r, "collect", side_effect=ValueError("bad source")), \
             patch.object(daily, "find_prior_daily", return_value=(None, [])), \
             patch.object(daily, "list_child_pages", return_value=[{
                 "id": "today", "title": daily.title_for(dt.datetime.now(r.TZ).date())}]), \
             patch.object(r, "sync") as sync:
            self.assertEqual(daily.main(), 1)
            sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
