"""au2026rec 的解析與排程測試：python -m unittest discover -s tests"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from au2026rec.plan import (  # noqa: E402
    STATUS_OK,
    STATUS_SHIFTED,
    STATUS_SKIPPED,
    build_plan,
    sanitize_filename,
)
from au2026rec.schedule import (  # noqa: E402
    ScheduleError,
    Session,
    load_schedule,
    map_columns,
    parse_date,
    parse_duration_minutes,
    parse_time_of_day,
)

PDT = "America/Los_Angeles"
TPE = ZoneInfo("Asia/Taipei")

AU_CSV = """Status,Session Title,Session Code,Date,Start Time,End Time,Room

Scheduled,Day 1 Keynote,KEY1001-D,2026-09-15,09:00,10:30,Digital G
Scheduled,Day 2 Keynote,KEY1002-D,2026-09-16,09:00,10:45,Digital E
"""


def write_temp(text: str, suffix: str = ".csv") -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    return Path(handle.name)


def load(text: str, suffix: str = ".csv", **kwargs):
    path = write_temp(text, suffix)
    try:
        return load_schedule(
            path,
            source_tz=kwargs.pop("source_tz", PDT),
            event_year=kwargs.pop("event_year", 2026),
            default_duration_minutes=kwargs.pop("default_duration_minutes", 60),
            **kwargs,
        )
    finally:
        path.unlink(missing_ok=True)


class TestTimeParsing(unittest.TestCase):
    def test_24h_and_12h(self) -> None:
        self.assertEqual(parse_time_of_day("09:00"), (9, 0, 0))
        self.assertEqual(parse_time_of_day("9:00 AM"), (9, 0, 0))
        self.assertEqual(parse_time_of_day("12:00 AM"), (0, 0, 0))
        self.assertEqual(parse_time_of_day("12:30 PM"), (12, 30, 0))
        self.assertEqual(parse_time_of_day("1:05 p.m."), (13, 5, 0))
        self.assertEqual(parse_time_of_day("18:45:30"), (18, 45, 30))
        self.assertEqual(parse_time_of_day("7 PM"), (19, 0, 0))

    def test_rejects_garbage(self) -> None:
        for bad in ("", "TBD", "25:00", "9:99", "上午九點"):
            self.assertIsNone(parse_time_of_day(bad), bad)

    def test_dates(self) -> None:
        self.assertEqual(parse_date("2026-09-15", 2026), datetime(2026, 9, 15).date())
        self.assertEqual(parse_date("9/15/2026", 2026), datetime(2026, 9, 15).date())
        self.assertEqual(parse_date("Sep 15, 2026", 2026), datetime(2026, 9, 15).date())
        self.assertEqual(parse_date("Tuesday, September 15", 2026), datetime(2026, 9, 15).date())
        self.assertEqual(parse_date("Sep 15", 2026), datetime(2026, 9, 15).date())
        self.assertIsNone(parse_date("someday", 2026))

    def test_durations(self) -> None:
        self.assertEqual(parse_duration_minutes("1 hr"), 60)
        self.assertEqual(parse_duration_minutes("1.5 hours"), 90)
        self.assertEqual(parse_duration_minutes("45 min"), 45)
        self.assertEqual(parse_duration_minutes("1 hr 30 min"), 90)
        self.assertIsNone(parse_duration_minutes("很久"))


class TestColumnMapping(unittest.TestCase):
    def test_au_headers(self) -> None:
        mapping = map_columns(
            ["Status", "Session Title", "Session Code", "Date", "Start Time", "End Time", "Room"]
        )
        self.assertEqual(mapping["code"], "Session Code")
        self.assertEqual(mapping["start"], "Start Time")
        self.assertEqual(mapping["room"], "Room")

    def test_picker_headers_and_timezone_suffix(self) -> None:
        mapping = map_columns(["code", "title", "start (PDT)", "end (PDT)", "url", "mode"])
        self.assertEqual(mapping["start"], "start (PDT)")
        self.assertEqual(mapping["url"], "url")

    def test_unusable_headers(self) -> None:
        with self.assertRaises(ScheduleError):
            map_columns(["foo", "bar"])


class TestScheduleLoading(unittest.TestCase):
    def test_official_au_csv(self) -> None:
        sessions, warnings = load(AU_CSV)
        self.assertEqual([s.code for s in sessions], ["KEY1001-D", "KEY1002-D"])
        # PDT 09:00 → 台灣 00:00 隔天
        self.assertEqual(sessions[0].start.astimezone(TPE).strftime("%m/%d %H:%M"), "09/16 00:00")
        self.assertEqual(sessions[0].duration, timedelta(minutes=90))
        self.assertTrue(all("網址" in w for w in warnings))  # 沒對照表 → 提醒缺網址

    def test_catalog_fills_url_and_mode(self) -> None:
        catalog = {
            "KEY1001-D": {"url": "https://example.test/k1", "mode": "Live", "track": "Keynote"}
        }
        sessions, warnings = load(AU_CSV, catalog=catalog)
        self.assertEqual(sessions[0].url, "https://example.test/k1")
        self.assertEqual(sessions[0].mode, "Live")
        self.assertEqual(len(warnings), 1)  # 只剩 KEY1002-D 缺網址

    def test_status_filter(self) -> None:
        text = AU_CSV + "Waitlisted,Some Talk,ABC1234-D,2026-09-17,09:00,10:00,Digital A\n"
        sessions, warnings = load(text, status_include=["scheduled", ""])
        self.assertEqual(len(sessions), 2)
        self.assertTrue(any("Waitlisted" in w for w in warnings))

    def test_duration_fallback_when_no_end(self) -> None:
        text = "code,title,date,start,duration\nX1-D,Talk,2026-09-15,09:00,45 min\n"
        sessions, _ = load(text)
        self.assertEqual(sessions[0].duration, timedelta(minutes=45))

    def test_default_duration_when_nothing_known(self) -> None:
        text = "code,title,date,start\nX1-D,Talk,2026-09-15,09:00\n"
        sessions, _ = load(text, default_duration_minutes=30)
        self.assertEqual(sessions[0].duration, timedelta(minutes=30))

    def test_single_cell_range(self) -> None:
        text = "code,title,date,start\nX1-D,Talk,2026-09-15,9:00-11:15 AM\n"
        sessions, _ = load(text)
        self.assertEqual(sessions[0].duration, timedelta(minutes=135))

    def test_overnight_session(self) -> None:
        text = "code,title,date,start,end\nX1-D,Talk,2026-09-15,23:00,00:30\n"
        sessions, _ = load(text)
        self.assertEqual(sessions[0].duration, timedelta(minutes=90))

    def test_iso_datetime_columns(self) -> None:
        text = (
            "code,title,start datetime,end datetime\n"
            "X1-D,Talk,2026-09-15T09:00:00,2026-09-15T10:00:00\n"
        )
        sessions, _ = load(text)
        self.assertEqual(sessions[0].start.utcoffset(), timedelta(hours=-7))

    def test_bad_row_is_skipped_not_fatal(self) -> None:
        text = AU_CSV + "Scheduled,Broken,BAD1-D,someday,09:00,10:00,Digital A\n"
        sessions, warnings = load(text)
        self.assertEqual(len(sessions), 2)
        self.assertTrue(any("解析不出來" in w for w in warnings))

    def test_semicolon_delimiter(self) -> None:
        text = "code;title;date;start;end\nX1-D;Talk;2026-09-15;09:00;10:00\n"
        sessions, _ = load(text)
        self.assertEqual(len(sessions), 1)

    def test_ics(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
            "SUMMARY:KEY1001-D: Day 1 Keynote\r\n"
            "DTSTART;TZID=America/Los_Angeles:20260915T090000\r\n"
            "DTEND;TZID=America/Los_Angeles:20260915T103000\r\n"
            "URL:https://example.test/k1\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        sessions, _ = load(ics, suffix=".ics")
        self.assertEqual(sessions[0].code, "KEY1001-D")
        self.assertEqual(sessions[0].title, "Day 1 Keynote")
        self.assertEqual(sessions[0].url, "https://example.test/k1")
        self.assertEqual(sessions[0].duration, timedelta(minutes=90))


def make_session(code: str, start: str, minutes: int, mode: str) -> Session:
    begin = datetime.fromisoformat(start).replace(tzinfo=ZoneInfo(PDT))
    return Session(
        code=code,
        title=f"Talk {code}",
        start=begin,
        end=begin + timedelta(minutes=minutes),
        url=f"https://example.test/{code}",
        mode=mode,
    )


def plan(sessions, **kwargs):
    defaults = dict(
        live_modes=["live"],
        overlap_policy="shift",
        lead_seconds=60,
        tail_seconds=60,
        gap_seconds=30,
        filename_template="{start_local:%Y%m%d_%H%M}_{code}_{title}",
        filename_max_length=120,
        local_tz=TPE,
    )
    defaults.update(kwargs)
    return build_plan(sessions, **defaults)


class TestPlanning(unittest.TestCase):
    def test_non_overlapping_untouched(self) -> None:
        items = plan([
            make_session("A", "2026-09-15T09:00", 60, "Live"),
            make_session("B", "2026-09-15T11:00", 60, "On-demand"),
        ])
        self.assertEqual([i.status for i in items], [STATUS_OK, STATUS_OK])
        self.assertFalse(any(i.shifted for i in items))

    def test_ondemand_queues_after_live(self) -> None:
        items = plan([
            make_session("LIVE", "2026-09-15T09:00", 60, "Live"),
            make_session("OD", "2026-09-15T09:30", 60, "On-demand"),
        ])
        live, od = items[0], items[1]
        self.assertEqual(live.status, STATUS_OK)
        self.assertEqual(live.start, live.session.start)  # 直播不動
        self.assertEqual(od.status, STATUS_SHIFTED)
        self.assertGreaterEqual(od.open_at, live.stop_at)  # 排在直播收尾之後
        self.assertEqual(od.duration(), timedelta(minutes=60 + 2))  # 長度不變

    def test_ondemand_chain_queues_in_order(self) -> None:
        items = plan([
            make_session("A", "2026-09-15T09:00", 60, "On-demand"),
            make_session("B", "2026-09-15T09:10", 60, "On-demand"),
            make_session("C", "2026-09-15T09:20", 60, "On-demand"),
        ])
        self.assertEqual([i.session.code for i in items], ["A", "B", "C"])
        for earlier, later in zip(items, items[1:]):
            self.assertGreaterEqual(later.open_at, earlier.stop_at)

    def test_live_clash_marks_later_skipped(self) -> None:
        items = plan([
            make_session("L1", "2026-09-15T09:00", 60, "Live"),
            make_session("L2", "2026-09-15T09:30", 60, "Live"),
        ])
        by_code = {i.session.code: i for i in items}
        self.assertEqual(by_code["L1"].status, STATUS_OK)
        self.assertEqual(by_code["L2"].status, STATUS_SKIPPED)
        self.assertIn("重疊", by_code["L2"].note)

    def test_skip_policy(self) -> None:
        items = plan(
            [
                make_session("L", "2026-09-15T09:00", 60, "Live"),
                make_session("OD", "2026-09-15T09:30", 60, "On-demand"),
            ],
            overlap_policy="skip",
        )
        by_code = {i.session.code: i for i in items}
        self.assertEqual(by_code["OD"].status, STATUS_SKIPPED)

    def test_keep_policy_changes_nothing(self) -> None:
        sessions = [
            make_session("L", "2026-09-15T09:00", 60, "Live"),
            make_session("OD", "2026-09-15T09:30", 60, "On-demand"),
        ]
        items = plan(sessions, overlap_policy="keep")
        self.assertTrue(all(i.status == STATUS_OK for i in items))
        self.assertEqual(items[1].start, sessions[1].start)

    def test_lead_and_tail_applied(self) -> None:
        item = plan([make_session("A", "2026-09-15T09:00", 60, "Live")])[0]
        self.assertEqual(item.session.start - item.open_at, timedelta(seconds=60))
        self.assertEqual(item.stop_at - item.session.end, timedelta(seconds=60))

    def test_filename_uses_local_time(self) -> None:
        item = plan([make_session("KEY1001-D", "2026-09-15T09:00", 90, "Live")])[0]
        self.assertTrue(item.output_name.startswith("20260916_0000_KEY1001-D"))

    def test_room_marks_live_too(self) -> None:
        session = make_session("A", "2026-09-15T09:00", 60, "")
        session.room = "Live Stream Studio"
        self.assertTrue(session.is_live(["live"]))


class TestFilenames(unittest.TestCase):
    def test_strips_illegal_characters(self) -> None:
        self.assertEqual(sanitize_filename('a/b\\c:d*e?"f<g>h|i', 120), "abcdefghi")

    def test_truncates_and_trims(self) -> None:
        self.assertEqual(len(sanitize_filename("x" * 300, 50)), 50)
        self.assertEqual(sanitize_filename("  spaced   out . ", 120), "spaced out")

    def test_never_empty(self) -> None:
        self.assertEqual(sanitize_filename("///", 120), "session")


if __name__ == "__main__":
    unittest.main()
