# Daily calendar and tracker reminders

The scheduled workflow creates the daily page around 03:00 Asia/Seoul and
refreshes a linked `자동 일정` callout under `Reminder`. Personal sections
continue using the existing carry-forward rules. Automatic entries are
rebuilt from their source trackers and never copied from yesterday.

## Tracker setup

Share each tracker database with the **same Notion integration** used by the
repository's existing `NOTION_TOKEN` secret. Database IDs and data source IDs
are different: the query API needs data source IDs.

Each source uses these properties:

| Property | Notion type | Use |
| --- | --- | --- |
| Any title property | Title | Reminder label and link |
| Start | Date | Event/period start; fallback deadline |
| End | Date | Deadline, or end of an event/period |
| Status | Select or Status | `완료`/`Done`/`Complete`/`Completed` hides it |
| Notes | Text | Up to 240 characters shown beside the source link |
| Daily Reminder | Checkbox | Explicit opt-in; unchecked rows are ignored |
| Advance Days | Number | Whole days before the relevant date; 0 means that day |
| Reminder Type | Select | `Deadline`, `Event`, `Period`, or `Exam` |

Set the encrypted repository Actions secret `REMINDER_SOURCES_JSON` to a JSON array:

```json
[{"data_source_id": "YOUR_DATA_SOURCE_UUID", "label": "Tracker"}]
```

Keep personal task data and actual configuration out of source control.
An empty configuration leaves the calendar in its original manual-only mode.

## When entries appear

| Type | Default advance | Anchor | Stop showing |
| --- | --- | --- | --- |
| Deadline | 7 days | End, or Start if End is empty | Mark complete, disable, or archive the source |
| Event | 7 days | Start | After the end date, or when completed |
| Period | 7 days | Start | After the end date, or when completed |
| Exam | 14 days | Start | After the end date, or when completed |

`Advance Days` overrides the defaults, including zero. Dates are inclusive
calendar days in Korea time. Timed events show their start/end times in KST.
Deadlines remain visible as overdue; they are never rolled into next year.
Preparation periods show a countdown before they start and a progress label
through their end. Long semester overview rows should generally stay disabled.

An interview preparation window is not a confirmed appointment. Once its
actual date is known, create a **separate contact action** dated three days
before the interview, with `Reminder Type=Deadline` and `Advance Days=0`.
Mark that action complete after sending the reminder. Undated rows are ignored.
Estimated dates keep their original labels and notes; the script does not
verify or invent official deadlines.

## Running and checking

```sh
pip install requests tzdata
python -m unittest discover -s tests -v
python daily_calendar.py --dry-run
python daily_calendar.py --dry-run --date 2026-09-07
python daily_calendar.py
```

In GitHub Actions, run `daily-notion-calendar` with mode `preview` first.
Preview queries trackers but performs **no Notion writes** and reports counts
only, because workflow logs in this repository are public. Then use `apply`
to generate or refresh today's page. Scheduled runs use `apply` automatically.
The workflow concurrency group serializes scheduled and manual runs.

Edit dates and completion in the linked source page. The next execution
reflects those changes. Manual reruns replace only the owned automatic
callout; personal reminders and other sections on today's page are untouched.
Archived daily pages keep their snapshots. These are visible page entries,
not Notion push notifications.

## Failure behavior

Every source must be read successfully before the automatic block is updated.
A missing permission or invalid configuration fails the workflow rather than
pretending a source is empty. Existing automatic content stays intact, while
the normal daily page generation can still proceed. On a new page, failed
source reads leave no automatic callout; check the failed Actions run.

Replacement creates the new callout before removing the old one. A rerun
reconciles duplicates after interruption. The special source-control link on
the callout title identifies the owned block; do not remove that link or move
the callout outside `Reminder`. Put personal notes outside this callout.
If more than 99 items qualify, the refresh fails safely rather than truncating
your reminders.

The existing free-text reminder parser is separate from tracker reminders.
Structured reminders use full Notion dates, so Korean date phrases in
personal notes do not affect their scheduling.
