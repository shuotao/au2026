"""排程規劃：把課表變成不重疊的錄影計畫。

規則：
* 直播場次（live_modes）時間鎖死，錄不到就是錄不到，所以永遠優先佔位。
  兩場直播撞在一起時，晚開始的那場標為 skipped，由你自己決定要放棄哪一場。
* 非直播（On-demand）場次可以往後挪，依課表順序排隊，自動避開直播區塊。
* overlap_policy = keep 時完全照課表時間，不做任何調整。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from au2026rec.schedule import Session

STATUS_OK = "ok"
STATUS_SHIFTED = "shifted"
STATUS_SKIPPED = "skipped"

_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class PlanItem:
    """一場課的實際錄影安排。"""

    index: int
    session: Session
    start: datetime          # 實際錄影起點（= 課程開始，已含挪移）
    end: datetime            # 實際錄影終點（= 課程結束，已含挪移）
    lead_seconds: int
    tail_seconds: int
    anchored: bool
    status: str = STATUS_OK
    note: str = ""
    output_name: str = ""

    @property
    def open_at(self) -> datetime:
        """開網頁、按播放的時間點。"""
        return self.start - timedelta(seconds=self.lead_seconds)

    @property
    def stop_at(self) -> datetime:
        """停止錄影的時間點。"""
        return self.end + timedelta(seconds=self.tail_seconds)

    @property
    def shifted(self) -> bool:
        return self.start != self.session.start

    def duration(self) -> timedelta:
        return self.stop_at - self.open_at


def sanitize_filename(text: str, max_length: int) -> str:
    cleaned = _ILLEGAL_FILENAME.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")
    return cleaned or "session"


def render_output_name(
    item: PlanItem, *, template: str, local_tz, max_length: int
) -> str:
    session = item.session
    fields = {
        "code": session.code or "NOCODE",
        "title": session.title,
        "mode": session.mode or "unknown",
        "track": session.track,
        "room": session.room,
        "index": f"{item.index:02d}",
        "start_local": item.start.astimezone(local_tz),
        "end_local": item.end.astimezone(local_tz),
        "start_source": item.start,
        "end_source": item.end,
    }
    try:
        rendered = template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"filename_template 有問題：{exc}。可用欄位："
            f"{', '.join(sorted(fields))}"
        ) from exc
    return sanitize_filename(rendered, max_length)


def build_plan(
    sessions: Sequence[Session],
    *,
    live_modes: Iterable[str],
    overlap_policy: str,
    lead_seconds: int,
    tail_seconds: int,
    gap_seconds: int,
    filename_template: str,
    filename_max_length: int,
    local_tz,
) -> list[PlanItem]:
    """回傳依實際錄影時間排序的計畫，含 skipped 項目（方便報告）。"""
    live_modes = list(live_modes)
    policy = overlap_policy.lower()

    def make_item(session: Session) -> PlanItem:
        return PlanItem(
            index=0,
            session=session,
            start=session.start,
            end=session.end,
            lead_seconds=lead_seconds,
            tail_seconds=tail_seconds,
            anchored=session.is_live(live_modes),
        )

    items = [make_item(s) for s in sorted(sessions, key=lambda s: (s.start, s.code))]

    if policy == "keep":
        placed = items
    else:
        placed = _place(items, policy=policy, gap_seconds=gap_seconds)

    placed.sort(key=lambda item: (item.status == STATUS_SKIPPED, item.start, item.session.code))
    for number, item in enumerate(placed, start=1):
        item.index = number
        item.output_name = render_output_name(
            item, template=filename_template, local_tz=local_tz, max_length=filename_max_length
        )
    return placed


def _blocks(item: PlanItem, gap_seconds: int) -> tuple[datetime, datetime]:
    """這場實際佔用的時間區間，含前置與收尾緩衝。"""
    return item.open_at, item.stop_at + timedelta(seconds=gap_seconds)


def _place(items: list[PlanItem], *, policy: str, gap_seconds: int) -> list[PlanItem]:
    occupied: list[tuple[datetime, datetime, PlanItem]] = []

    def collides(start: datetime, end: datetime) -> PlanItem | None:
        for busy_start, busy_end, owner in occupied:
            if start < busy_end and busy_start < end:
                return owner
        return None

    # 第一輪：直播場次照原時間佔位。
    for item in [i for i in items if i.anchored]:
        window = _blocks(item, gap_seconds)
        clash = collides(*window)
        if clash is None:
            occupied.append((*window, item))
            continue
        item.status = STATUS_SKIPPED
        item.note = f"與直播場次 {clash.session.label()} 時間重疊，需自行取捨"

    # 第二輪：On-demand 依課表順序往後排隊。
    for item in [i for i in items if not i.anchored]:
        length = item.end - item.start
        cursor = item.start
        latest_end = max((busy_end for busy_start, busy_end, _ in occupied), default=None)
        for _ in range(len(occupied) + 1):
            window_start = cursor - timedelta(seconds=item.lead_seconds)
            window_end = cursor + length + timedelta(seconds=item.tail_seconds + gap_seconds)
            clash = collides(window_start, window_end)
            if clash is None:
                break
            if policy == "skip":
                item.status = STATUS_SKIPPED
                item.note = f"與 {clash.session.label()} 重疊，policy=skip 直接跳過"
                break
            cursor = clash.stop_at + timedelta(seconds=gap_seconds + item.lead_seconds)
        else:  # pragma: no cover - 迴圈次數足夠，理論上排得進去
            item.status = STATUS_SKIPPED
            item.note = "排不進任何空檔"

        if item.status == STATUS_SKIPPED:
            continue
        if cursor != item.start:
            item.status = STATUS_SHIFTED
            shift = cursor - item.start
            item.note = f"與其他場次重疊，往後挪 {format_delta(shift)}"
            if latest_end is not None:
                item.note += "（On-demand 隨時可看，挪動不影響內容）"
            item.start, item.end = cursor, cursor + length
        occupied.append((*_blocks(item, gap_seconds), item))

    return items


def format_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{sign}{hours} 小時 {minutes} 分"
    if minutes:
        return f"{sign}{minutes} 分 {seconds} 秒"
    return f"{sign}{seconds} 秒"


def summarize(items: Sequence[PlanItem]) -> dict[str, int]:
    counts = {STATUS_OK: 0, STATUS_SHIFTED: 0, STATUS_SKIPPED: 0}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts
