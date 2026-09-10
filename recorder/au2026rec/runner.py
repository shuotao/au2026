"""排程執行：等時間 → 開課程頁 → OBS 開錄 → 到點停錄 → 下一場。"""
from __future__ import annotations

import csv
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from au2026rec.browser import BrowserError, Navigator
from au2026rec.obs import ObsController, ObsError
from au2026rec.plan import STATUS_SKIPPED, PlanItem, format_delta

log = logging.getLogger("au2026rec")

REPORT_FIELDS = [
    "index", "code", "title", "mode",
    "planned_start_local", "planned_end_local",
    "recorded_start", "recorded_stop",
    "result", "output_path", "played", "fullscreen", "note", "url",
]


class Interrupted(RuntimeError):
    """使用者按了 Ctrl-C。"""


@dataclass
class RunOptions:
    gap_seconds: int = 15
    scene: str = ""
    local_tz: Any = None
    report_file: Path | None = None
    countdown_every: int = 30
    skip_past: bool = True


class _StopFlag:
    def __init__(self) -> None:
        self.raised = False

    def install(self) -> None:
        def handler(_signum: int, _frame: object) -> None:
            if self.raised:  # 第二次 Ctrl-C 直接砍
                raise KeyboardInterrupt
            self.raised = True
            log.warning("收到中斷訊號，會先把目前這場收尾（再按一次 Ctrl-C 立即中止）")

        try:
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):  # pragma: no cover - 非主執行緒
            log.debug("無法安裝 SIGINT handler")

    def check(self) -> None:
        if self.raised:
            raise Interrupted


def setup_logging(log_file: Path | None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("obsws_python").setLevel(logging.WARNING)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sleep_until(target: datetime, stop: _StopFlag, *, label: str, countdown_every: int = 30) -> None:
    """等到 target。每 countdown_every 秒印一次剩餘時間，可被 Ctrl-C 打斷。"""
    next_report = 0.0
    while True:
        stop.check()
        remaining = (target - now_utc()).total_seconds()
        if remaining <= 0:
            return
        if countdown_every and remaining >= 5 and time.monotonic() >= next_report:
            log.info("%s：還有 %s", label, format_delta(timedelta(seconds=int(remaining))))
            next_report = time.monotonic() + max(countdown_every, 5)
        time.sleep(min(1.0, max(remaining, 0.05)))


class Runner:
    def __init__(
        self,
        browser: Navigator,
        obs: ObsController,
        options: RunOptions,
    ) -> None:
        self.browser = browser
        self.obs = obs
        self.options = options
        self._stop = _StopFlag()
        self._rows: list[dict[str, object]] = []

    # ── 主流程 ──────────────────────────────────────────────────────────
    def run(self, items: Sequence[PlanItem]) -> list[dict[str, object]]:
        todo = [item for item in items if item.status != STATUS_SKIPPED]
        for item in items:
            if item.status == STATUS_SKIPPED:
                self._record_row(item, result="skipped", note=item.note)

        if self.options.skip_past:
            fresh = [item for item in todo if item.stop_at > now_utc()]
            for item in todo:
                if item not in fresh:
                    log.warning("略過已結束的場次：%s", item.session.label())
                    self._record_row(item, result="past", note="執行時該場次已結束")
            todo = fresh

        if not todo:
            log.warning("沒有待錄的場次（都結束了或都被跳過）")
            return self._rows

        log.info("待錄 %d 場，第一場 %s 開始", len(todo), self._fmt(todo[0].start))
        self._stop.install()
        try:
            for position, item in enumerate(todo, start=1):
                log.info("── [%d/%d] %s ──", position, len(todo), item.session.label())
                self._run_one(item)
        except Interrupted:
            log.warning("已中斷排程，尚未執行的場次不會錄")
        finally:
            self._flush_report()
        return self._rows

    def _run_one(self, item: PlanItem) -> None:
        session = item.session
        row: dict[str, object] = {}

        if not session.url:
            log.error("%s 沒有課程網址，跳過。先跑 au2026rec catalog 補對照表", session.label())
            self._record_row(item, result="no-url", note="缺少課程網址")
            return

        sleep_until(
            item.open_at,
            self._stop,
            label=f"等 {session.code or session.title} 開場",
            countdown_every=self.options.countdown_every,
        )

        try:
            opened = self.browser.open_session(session.url)
        except BrowserError as exc:
            log.error("開課程頁失敗：%s", exc)
            self._record_row(item, result="browser-error", note=str(exc))
            return
        except Exception as exc:  # 網頁千奇百怪，不要因為一場毀掉整晚
            log.exception("開課程頁時發生未預期錯誤：%s", exc)
            self._record_row(item, result="browser-error", note=str(exc))
            return

        row["played"] = opened.get("played")
        row["fullscreen"] = opened.get("fullscreen")
        if not opened.get("played"):
            log.warning(
                "找不到播放鍵，仍會照時間錄影（畫面可能停在課程頁）。"
                "可用 au2026rec probe 補 [browser] play_selectors"
            )

        sleep_until(item.start, self._stop, label="等課程開始", countdown_every=0)

        started_at: datetime | None = None
        output_path: str | None = None
        result = "recorded"
        note = item.note
        try:
            if self.options.scene or session.scene:
                self.obs.switch_scene(session.scene or self.options.scene)
            self.obs.start_recording(item.output_name)
            started_at = now_utc()
        except ObsError as exc:
            log.error("OBS 開始錄影失敗：%s", exc)
            self._record_row(item, result="obs-error", note=str(exc), extra=row)
            self.browser.leave_session()
            return

        try:
            sleep_until(item.stop_at, self._stop, label="錄影中", countdown_every=self.options.countdown_every)
        except Interrupted:
            result = "interrupted"
            note = (note + "；" if note else "") + "使用者中斷，已提前收尾"
            self._stop.raised = False  # 讓收尾流程跑完
            raise_after = True
        else:
            raise_after = False

        try:
            output_path = self.obs.stop_recording()
        except ObsError as exc:
            log.error("OBS 停止錄影失敗：%s", exc)
            result = "obs-stop-error"
            note = (note + "；" if note else "") + str(exc)

        self.browser.leave_session()
        self._record_row(
            item,
            result=result,
            note=note,
            output_path=output_path,
            started_at=started_at,
            stopped_at=now_utc(),
            extra=row,
        )
        log.info("完成 %s → %s", item.output_name, output_path or "（OBS 未回報路徑）")

        if raise_after:
            self._stop.raised = True
            raise Interrupted
        if self.options.gap_seconds:
            sleep_until(
                now_utc() + timedelta(seconds=self.options.gap_seconds),
                self._stop,
                label="場間休息",
                countdown_every=0,
            )

    # ── 報告 ────────────────────────────────────────────────────────────
    def _fmt(self, moment: datetime) -> str:
        tz = self.options.local_tz
        return moment.astimezone(tz).strftime("%m/%d %H:%M") if tz else moment.isoformat()

    def _record_row(
        self,
        item: PlanItem,
        *,
        result: str,
        note: str = "",
        output_path: str | None = None,
        started_at: datetime | None = None,
        stopped_at: datetime | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        row: dict[str, object] = {
            "index": item.index,
            "code": item.session.code,
            "title": item.session.title,
            "mode": item.session.mode,
            "planned_start_local": self._fmt(item.start),
            "planned_end_local": self._fmt(item.end),
            "recorded_start": self._fmt(started_at) if started_at else "",
            "recorded_stop": self._fmt(stopped_at) if stopped_at else "",
            "result": result,
            "output_path": output_path or "",
            "played": "",
            "fullscreen": "",
            "note": note,
            "url": item.session.url or "",
        }
        row.update(extra or {})
        self._rows.append(row)
        self._flush_report()

    def _flush_report(self) -> None:
        target = self.options.report_file
        if target is None or not self._rows:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._rows)
