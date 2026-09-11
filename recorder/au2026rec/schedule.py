"""課表解析：AU 官方匯出的 CSV（或 ICS）→ 有時區的 Session 清單。

AU2026 My Schedule 匯出的 CSV 長這樣（時間為太平洋時間）：

    Status,Session Title,Session Code,Date,Start Time,End Time,Room
    Scheduled,Day 1 Keynote,KEY1001-D,2026-09-15,09:00,10:30,Digital G

裡面沒有課程網址，所以 Session Code 要透過 catalog.json 補上 URL；
catalog.json 由 `au2026rec catalog` 從 AU2026_挑課工具.html 產生。
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "code": ("code", "session code", "sessioncode", "session id", "sessionid", "代碼", "課程代碼"),
    "title": ("title", "session title", "sessiontitle", "name", "session name", "標題", "課程名稱"),
    "url": ("url", "link", "session url", "sessionurl", "連結", "網址"),
    "date": ("date", "session date", "start date", "日期"),
    "start": ("start", "start time", "starttime", "begin", "from", "開始", "開始時間"),
    "end": ("end", "end time", "endtime", "finish", "to", "結束", "結束時間"),
    "start_dt": ("start datetime", "startdatetime", "start iso", "starts at"),
    "end_dt": ("end datetime", "enddatetime", "end iso", "ends at"),
    "duration": ("duration", "length", "時長", "長度"),
    "mode": ("mode", "format", "tabs", "delivery", "session type", "類型", "形式"),
    "track": ("track", "topic", "主題"),
    "room": ("room", "location", "venue", "地點"),
    "status": ("status", "registration status", "狀態"),
    "scene": ("scene", "obs scene", "obsscene"),
    "timezone": ("timezone", "time zone", "tz", "時區"),
}

_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?(?::(\d{2}))?\s*([ap])\.?\s?m\.?$", re.I)
_TIME24_RE = re.compile(r"^(\d{1,2})[:.](\d{2})(?::(\d{2}))?$")
_RANGE_SPLIT = re.compile(r"\s*(?:–|—|-|~|to)\s*", re.I)
_DURATION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.I)
_SESSION_CODE_RE = re.compile(r"\b([A-Z]{2,4}\d{3,5}(?:-[A-Z]{1,2})?)\b")
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%d %B %Y",
    "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y", "%Y%m%d",
)
_DATE_FORMATS_NO_YEAR = ("%b %d", "%B %d", "%m/%d", "%m-%d")


class ScheduleError(RuntimeError):
    pass


@dataclass
class Session:
    """一場課，時間已帶時區。"""

    code: str
    title: str
    start: datetime
    end: datetime
    url: str | None = None
    mode: str = ""
    track: str = ""
    room: str = ""
    scene: str = ""
    source_row: int = 0
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def is_live(self, live_modes: Iterable[str]) -> bool:
        haystack = f"{self.mode} {self.room}".lower()
        return any(token.lower() in haystack for token in live_modes if token)

    def label(self) -> str:
        return f"{self.code or '?'} {self.title}".strip()


# ── 欄位對應 ────────────────────────────────────────────────────────────

def _normalize_header(name: str) -> str:
    text = name.replace("﻿", "").strip().lower()
    text = re.sub(r"\((?:pdt|pst|pt|utc[^)]*|local)\)", " ", text)
    text = re.sub(r"[_\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def map_columns(headers: Iterable[str]) -> dict[str, str]:
    """把實際欄名對到內部欄位名。回傳 {內部欄位: 原始欄名}。"""
    normalized = {_normalize_header(h): h for h in headers if h and h.strip()}
    mapping: dict[str, str] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field_name] = normalized[alias]
                break
    if "code" not in mapping and "title" not in mapping:
        raise ScheduleError(
            "課表看不出課程欄位，至少要有 Session Code 或 Session Title。"
            f"讀到的欄位：{', '.join(normalized.values()) or '（空白）'}"
        )
    return mapping


# ── 時間解析 ────────────────────────────────────────────────────────────

def parse_time_of_day(text: str) -> tuple[int, int, int] | None:
    value = (text or "").strip()
    if not value:
        return None
    match = _TIME_RE.match(value)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(4).lower() == "p":
            hour += 12
        return hour, int(match.group(2) or 0), int(match.group(3) or 0)
    match = _TIME24_RE.match(value)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        return hour, minute, int(match.group(3) or 0)
    return None


def _split_range(text: str) -> tuple[str, str] | None:
    """處理單格區間（例：9:00 到 11:15 AM）；前半沒 AM/PM 時沿用後半的。"""
    parts = [p for p in _RANGE_SPLIT.split(text.strip()) if p]
    if len(parts) != 2:
        return None
    left, right = parts
    if not re.search(r"[ap]\.?m", left, re.I):
        meridiem = re.search(r"([ap]\.?\s?m\.?)", right, re.I)
        if meridiem:
            left = f"{left} {meridiem.group(1)}"
    return left, right


def parse_date(text: str, event_year: int) -> _date | None:
    value = re.sub(r"^[A-Za-z]+day,?\s*", "", (text or "").strip())
    value = re.sub(r"\((\d+)(?:st|nd|rd|th)\)", r"\1", value)
    value = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", value).strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    for fmt in _DATE_FORMATS_NO_YEAR:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return parsed.date().replace(year=event_year)
    return None


def parse_duration_minutes(text: str) -> int | None:
    total = 0.0
    found = False
    for amount, unit in _DURATION_RE.findall(text or ""):
        value = float(amount.replace(",", "."))
        total += value * 60 if unit.lower().startswith(("h", "hr")) else value
        found = True
    if not found:
        return None
    return int(round(total))


def _parse_iso(text: str, tz: ZoneInfo) -> datetime | None:
    value = (text or "").strip().replace("Z", "+00:00")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleError(
            f"認不得時區 {name!r}。Windows 上請先執行 pip install tzdata。"
        ) from exc


# ── 對照表 ──────────────────────────────────────────────────────────────

def normalize_code(code: str) -> str:
    return re.sub(r"\s+", "", (code or "")).upper()


def load_catalog(path: Path | None) -> dict[str, dict[str, Any]]:
    """讀 code → 課程資料 的對照表；檔案不存在就回空 dict。"""
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {str(item.get("code", "")).strip(): item for item in data if item.get("code")}
    return {normalize_code(k): v for k, v in data.items() if k}


# ── 課表載入 ────────────────────────────────────────────────────────────

def load_schedule(
    path: Path,
    *,
    source_tz: str,
    event_year: int,
    default_duration_minutes: int,
    catalog: dict[str, dict[str, Any]] | None = None,
    status_include: Iterable[str] | None = None,
) -> tuple[list[Session], list[str]]:
    """回傳 (sessions, warnings)。sessions 依開始時間排序。"""
    if not path.exists():
        raise ScheduleError(f"找不到課表檔 {path}")
    if path.suffix.lower() in {".ics", ".ical", ".ifb"}:
        return _load_ics(path, source_tz=source_tz, catalog=catalog or {})
    return _load_csv(
        path,
        source_tz=source_tz,
        event_year=event_year,
        default_duration_minutes=default_duration_minutes,
        catalog=catalog or {},
        status_include=status_include,
    )


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """讀 CSV，跳過官方匯出裡的空白行，並容忍標題列前面的說明文字。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ScheduleError(f"課表檔 {path} 是空的")
    try:
        dialect: Any = csv.Sniffer().sniff("\n".join(lines[:5]), delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    header_index = 0
    for index, line in enumerate(lines[:10]):
        probe = _normalize_header(line)
        if any(alias in probe for alias in ("session code", "session title", "start time", "code", "title")):
            header_index = index
            break
    reader = csv.DictReader(lines[header_index:], dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h]
    rows = [row for row in reader if any((v or "").strip() for v in row.values())]
    return headers, rows


def _load_csv(
    path: Path,
    *,
    source_tz: str,
    event_year: int,
    default_duration_minutes: int,
    catalog: dict[str, dict[str, Any]],
    status_include: Iterable[str] | None,
) -> tuple[list[Session], list[str]]:
    headers, rows = _read_csv_rows(path)
    mapping = map_columns(headers)
    tz = get_zone(source_tz)
    allowed = {s.strip().lower() for s in (status_include or [])}
    sessions: list[Session] = []
    warnings: list[str] = []

    for offset, row in enumerate(rows, start=2):
        def cell(key: str, _row: dict[str, str] = row) -> str:
            if key not in mapping:
                return ""
            return (_row.get(mapping[key], "") or "").strip()

        status = cell("status")
        if allowed and status.lower() not in allowed:
            warnings.append(f"第 {offset} 行：狀態為 {status!r}，不在 status_include 內，已略過")
            continue

        code = normalize_code(cell("code"))
        entry = catalog.get(code, {})
        title = cell("title") or str(entry.get("title", "")) or code
        row_tz = get_zone(cell("timezone")) if cell("timezone") else tz

        start, end, problem = _resolve_row_times(
            cell=cell,
            entry=entry,
            tz=row_tz,
            event_year=event_year,
            default_duration_minutes=default_duration_minutes,
        )
        if problem:
            warnings.append(f"第 {offset} 行（{code or title}）：{problem}，已略過")
            continue
        assert start is not None and end is not None

        url = cell("url") or str(entry.get("url", "") or "")
        if not url:
            warnings.append(
                f"第 {offset} 行（{code or title}）：對照表查不到課程網址，"
                "請先執行 au2026rec catalog，或在課表加一欄 URL"
            )
        sessions.append(
            Session(
                code=code,
                title=title,
                start=start,
                end=end,
                url=url or None,
                mode=cell("mode") or str(entry.get("mode", "") or ""),
                track=cell("track") or str(entry.get("track", "") or ""),
                room=cell("room"),
                scene=cell("scene"),
                source_row=offset,
                raw=dict(row),
            )
        )

    sessions.sort(key=lambda s: (s.start, s.code))
    return sessions, warnings


def _resolve_row_times(
    *,
    cell,
    entry: dict[str, Any],
    tz: ZoneInfo,
    event_year: int,
    default_duration_minutes: int,
) -> tuple[datetime | None, datetime | None, str | None]:
    """依序嘗試：完整 ISO 時間 → 日期＋時間 → 對照表時間。"""
    start = _parse_iso(cell("start_dt"), tz)
    end = _parse_iso(cell("end_dt"), tz)
    if start:
        return start, end or (start + timedelta(minutes=default_duration_minutes)), None

    date_text = cell("date") or str(entry.get("date", "") or "")
    day = parse_date(date_text, event_year)
    if day is None:
        return None, None, f"日期 {date_text!r} 解析不出來"

    start_text = cell("start") or str(entry.get("start", "") or "")
    end_text = cell("end") or str(entry.get("end", "") or "")
    if not end_text:
        both = _split_range(start_text)
        if both:
            start_text, end_text = both

    start_hms = parse_time_of_day(start_text)
    if start_hms is None:
        return None, None, f"開始時間 {start_text!r} 解析不出來"
    start = datetime(day.year, day.month, day.day, *start_hms, tzinfo=tz)

    end_hms = parse_time_of_day(end_text)
    if end_hms is not None:
        end = datetime(day.year, day.month, day.day, *end_hms, tzinfo=tz)
        if end <= start:  # 跨日場次
            end += timedelta(days=1)
        return start, end, None

    minutes = parse_duration_minutes(cell("duration") or str(entry.get("duration", "") or ""))
    return start, start + timedelta(minutes=minutes or default_duration_minutes), None


# ── ICS ────────────────────────────────────────────────────────────────

def _unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_ics_datetime(value: str, params: dict[str, str], fallback_tz: ZoneInfo) -> datetime | None:
    raw = value.strip()
    tz: Any = fallback_tz
    if raw.endswith("Z"):
        raw, tz = raw[:-1], ZoneInfo("UTC")
    elif "TZID" in params:
        try:
            tz = get_zone(params["TZID"])
        except ScheduleError:
            tz = fallback_tz
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _load_ics(
    path: Path, *, source_tz: str, catalog: dict[str, dict[str, Any]]
) -> tuple[list[Session], list[str]]:
    tz = get_zone(source_tz)
    sessions: list[Session] = []
    warnings: list[str] = []
    current: dict[str, Any] | None = None

    for line in _unfold_ics(path.read_text(encoding="utf-8-sig", errors="replace")):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
            continue
        if stripped == "END:VEVENT":
            if current is not None:
                session, problem = _ics_event_to_session(current, tz, catalog)
                if session:
                    sessions.append(session)
                elif problem:
                    warnings.append(problem)
            current = None
            continue
        if current is None or ":" not in stripped:
            continue
        name_part, _, value = stripped.partition(":")
        name, *param_parts = name_part.split(";")
        params = {
            p.split("=", 1)[0].upper(): p.split("=", 1)[1]
            for p in param_parts
            if "=" in p
        }
        current[name.upper()] = (value, params)

    sessions.sort(key=lambda s: (s.start, s.code))
    return sessions, warnings


def _ics_event_to_session(
    event: dict[str, Any], tz: ZoneInfo, catalog: dict[str, dict[str, Any]]
) -> tuple[Session | None, str | None]:
    summary = event.get("SUMMARY", ("", {}))[0].strip()
    dtstart = event.get("DTSTART")
    dtend = event.get("DTEND")
    if not dtstart:
        return None, f"ICS 事件 {summary!r} 沒有 DTSTART，已略過"
    start = _parse_ics_datetime(dtstart[0], dtstart[1], tz)
    end = _parse_ics_datetime(dtend[0], dtend[1], tz) if dtend else None
    if start is None:
        return None, f"ICS 事件 {summary!r} 的 DTSTART 解析不出來，已略過"
    if end is None or end <= start:
        end = start + timedelta(hours=1)

    text = " ".join(event.get(key, ("", {}))[0] for key in ("SUMMARY", "DESCRIPTION", "LOCATION"))
    code_match = _SESSION_CODE_RE.search(text)
    code = normalize_code(code_match.group(1)) if code_match else ""
    entry = catalog.get(code, {})
    url = event.get("URL", ("", {}))[0].strip() or str(entry.get("url", "") or "")
    if not url:
        found = re.search(r"https?://\S+", text)
        url = found.group(0).rstrip(">,)") if found else ""

    title = re.sub(r"^\s*[A-Z]{2,4}\d{3,5}(?:-[A-Z]{1,2})?[\s:—–-]*", "", summary) or str(
        entry.get("title", "") or code
    )
    return (
        Session(
            code=code,
            title=title,
            start=start,
            end=end,
            url=url or None,
            mode=str(entry.get("mode", "") or ""),
            track=str(entry.get("track", "") or ""),
            room=event.get("LOCATION", ("", {}))[0].strip(),
            raw={"summary": summary},
        ),
        None,
    )
