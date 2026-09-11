"""au2026rec 命令列介面。

    au2026rec setup                第一次使用：引導式設定（推薦）
    au2026rec init                 只產生 config.toml
    au2026rec catalog              從挑課工具 HTML 建立 code → 網址 對照表
    au2026rec validate             檢查課表能不能讀、網址有沒有齊
    au2026rec plan                 印出實際錄影時間軸
    au2026rec login                開瀏覽器手動登入 Autodesk（存進 profile）
    au2026rec probe <url>          印出頁面上的可點元素，用來補 play_selectors
    au2026rec display              列出螢幕、建立錄課場景（螢幕擷取 + 桌面音訊）
    au2026rec obs-doctor           檢查本機 OBS 設定，可自動填入密碼
    au2026rec obs-test             測 OBS 連線與場景
    au2026rec test-record <code>   單場試錄，驗證整條流程
    au2026rec run                  照課表無人值守執行
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from au2026rec import __version__, obslocal
from au2026rec import obsscene
from au2026rec.browser import (
    BrowserError,
    BrowserSettings,
    Navigator,
    PlaywrightNavigator,
    make_navigator,
)
from au2026rec.catalog import CatalogError, build_catalog, resolve_sources, write_catalog
from au2026rec.config import Config, ConfigError, load_config, set_value
from au2026rec.obs import NullObsController, ObsController, ObsError, ObsSettings
from au2026rec.plan import (
    STATUS_OK,
    STATUS_SHIFTED,
    STATUS_SKIPPED,
    PlanItem,
    build_plan,
    format_delta,
    summarize,
)
from au2026rec.runner import RunOptions, Runner, now_utc, setup_logging
from au2026rec.schedule import ScheduleError, get_zone, load_catalog, load_schedule

def setup_console() -> None:
    """讓主控台能印中文與 ⚠ ✓ ✗ 這類符號。

    Windows 的主控台預設是 cp950（繁中），遇到 U+26A0 這種不在字碼表裡的字元
    會直接拋 UnicodeEncodeError 把程式打掛 —— 打包成 exe 後特別明顯，因為沒有
    PYTHONIOENCODING 可以靠。所以這裡把主控台與 stdout/stderr 都切成 UTF-8，
    並用 errors="replace" 保底：就算字型缺字也只是顯示成問號，不會中斷。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass  # 沒有主控台（例如被重導向）時失敗無所謂
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


DISCLAIMER = (
    "本工具錄影僅供個人學習與課後複習；請遵守 AU 使用條款與著作權法。"
    "違法或侵權使用與開發者無關，詳見 README 的免責聲明。"
)

PACKAGE_ROOT = Path(__file__).resolve().parent


def bundled(name: str) -> Path:
    """找隨程式附帶的檔案。打包成 exe 時在 PyInstaller 的暫存目錄，否則在原始碼旁。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / name
        if candidate.exists():
            return candidate
    return PACKAGE_ROOT.parent / name


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


EXAMPLE_CONFIG = bundled("config.example.toml")


# ── 共用 ────────────────────────────────────────────────────────────────

def _load(args: argparse.Namespace) -> Config:
    return load_config(args.config)


def _load_sessions(cfg: Config) -> tuple[list[Any], list[str]]:
    catalog = load_catalog(cfg.resolve("schedule", "catalog"))
    sessions, warnings = load_schedule(
        cfg.resolve("schedule", "file"),
        source_tz=str(cfg.get("schedule", "source_timezone")),
        event_year=int(cfg.get("schedule", "event_year")),
        default_duration_minutes=int(cfg.get("schedule", "default_duration_minutes")),
        catalog=catalog,
        status_include=cfg.get("schedule", "status_include"),
    )
    if not catalog:
        warnings.insert(
            0,
            "找不到 catalog.json，課程網址只能靠課表自帶的 URL 欄。"
            "建議先執行 au2026rec catalog。",
        )
    return sessions, warnings


def _build_plan(cfg: Config, sessions: Sequence[Any]) -> list[PlanItem]:
    return build_plan(
        sessions,
        live_modes=cfg.get("schedule", "live_modes"),
        overlap_policy=str(cfg.get("schedule", "overlap_policy")),
        lead_seconds=int(cfg.get("recording", "lead_seconds")),
        tail_seconds=int(cfg.get("recording", "tail_seconds")),
        gap_seconds=int(cfg.get("recording", "gap_seconds")),
        filename_template=str(cfg.get("recording", "filename_template")),
        filename_max_length=int(cfg.get("recording", "filename_max_length")),
        local_tz=get_zone(str(cfg.get("schedule", "local_timezone"))),
    )


def _browser_settings(cfg: Config) -> BrowserSettings:
    size = cfg.get("browser", "window_size")
    position = cfg.get("browser", "window_position")
    return (
        BrowserSettings(
            mode=str(cfg.get("browser", "mode")).lower(),
            cdp_url=str(cfg.get("browser", "cdp_url")),
            fallback_to_open=bool(cfg.get("browser", "fallback_to_open")),
            window_position=(int(position[0]), int(position[1])) if len(position) == 2 else None,
            start_fullscreen=bool(cfg.get("browser", "start_fullscreen")),
            user_data_dir=cfg.resolve("browser", "user_data_dir"),
            channel=str(cfg.get("browser", "channel")),
            headless=bool(cfg.get("browser", "headless")),
            window_size=(int(size[0]), int(size[1])) if len(size) == 2 else None,
            settle_seconds=int(cfg.get("browser", "settle_seconds")),
            play_selectors=list(cfg.get("browser", "play_selectors")),
            fullscreen=bool(cfg.get("browser", "fullscreen")),
            fullscreen_selectors=list(cfg.get("browser", "fullscreen_selectors")),
            dismiss_selectors=list(cfg.get("browser", "dismiss_selectors")),
            close_page_after=bool(cfg.get("browser", "close_page_after")),
        )
    )


def _make_navigator(cfg: Config) -> Navigator:
    return make_navigator(_browser_settings(cfg))


def _make_obs(cfg: Config) -> ObsController:
    return ObsController(
        ObsSettings(
            host=str(cfg.get("obs", "host")),
            port=int(cfg.get("obs", "port")),
            password=str(cfg.get("obs", "password")),
            scene=str(cfg.get("obs", "scene")),
        )
    )


def _print_warnings(warnings: Sequence[str]) -> None:
    for warning in warnings:
        print(f"  ! {warning}")


# ── 指令 ────────────────────────────────────────────────────────────────

def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.config or "config.toml")
    if target.exists() and not args.force:
        print(f"{target} 已存在，要覆蓋請加 --force")
        return 1
    if not EXAMPLE_CONFIG.exists():
        print(f"找不到範本 {EXAMPLE_CONFIG}")
        return 1
    target.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"已產生 {target}")
    if getattr(args, "quiet", False):
        return 0  # 引導設定裡呼叫時，後續步驟由精靈自己帶，不要印手動指示
    print("接下來：")
    print("  1. 改 [schedule] file 指向你從 AU2026 匯出的 CSV")
    print("  2. 填 [obs] password（OBS → 工具 → WebSocket 伺服器設定）")
    print("  3. au2026rec catalog   建立課程網址對照表")
    print("  4. au2026rec login     手動登入 Autodesk 一次")
    print("  5. au2026rec plan      確認時間軸")
    print()
    print(f"⚠ {DISCLAIMER}")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    cfg = _load(args)
    sources = resolve_sources(args.source or [], cfg.root)
    packaged = bundled("catalog.json")
    if packaged.exists() and packaged != cfg.resolve("schedule", "catalog"):
        sources.append(packaged)  # 打包版附帶的對照表當最後備援
    catalog, notes = build_catalog(sources)
    target = cfg.resolve("schedule", "catalog")
    write_catalog(catalog, target)
    print(f"對照表已寫入 {target}（{len(catalog)} 筆）")
    for note in notes:
        print(f"  · {note}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = _load(args)
    sessions, warnings = _load_sessions(cfg)
    print(f"設定檔：{cfg.path}")
    print(f"課表：  {cfg.resolve('schedule', 'file')}")
    print(f"讀到 {len(sessions)} 場課")
    if warnings:
        print("提醒：")
        _print_warnings(warnings)
    missing = [s for s in sessions if not s.url]
    if missing:
        print(f"缺網址 {len(missing)} 場：{', '.join(s.code or s.title for s in missing)}")
    if not sessions:
        return 1
    return 1 if missing else 0


def _plan_table(cfg: Config, items: Sequence[PlanItem]) -> str:
    local_tz = get_zone(str(cfg.get("schedule", "local_timezone")))
    source_tz = get_zone(str(cfg.get("schedule", "source_timezone")))
    marks = {STATUS_OK: " ", STATUS_SHIFTED: "→", STATUS_SKIPPED: "×"}
    lines = [
        f"{'#':>3} {'':1} {'台灣時間':<17} {'太平洋':<12} {'長度':>6} {'類型':<10} 課程",
        "─" * 96,
    ]
    for item in items:
        start_local = item.start.astimezone(local_tz)
        end_local = item.end.astimezone(local_tz)
        start_src = item.start.astimezone(source_tz)
        length = int(item.duration().total_seconds() // 60)
        lines.append(
            f"{item.index:>3} {marks.get(item.status, ' ')} "
            f"{start_local:%m/%d %H:%M}–{end_local:%H:%M}   "
            f"{start_src:%m/%d %H:%M}   "
            f"{length:>4}m  "
            f"{(item.session.mode or '-')[:10]:<10} "
            f"{item.session.code} {item.session.title[:48]}"
        )
        if item.note:
            lines.append(f"        └ {item.note}")
    return "\n".join(lines)


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = _load(args)
    sessions, warnings = _load_sessions(cfg)
    if warnings:
        print("提醒：")
        _print_warnings(warnings)
    if not sessions:
        print("課表沒有可用的場次")
        return 1
    items = _build_plan(cfg, sessions)
    print(_plan_table(cfg, items))
    counts = summarize(items)
    total = sum(item.duration().total_seconds() for item in items if item.status != STATUS_SKIPPED)
    print()
    print(
        f"合計 {len(items)} 場："
        f"正常 {counts.get(STATUS_OK, 0)}、"
        f"往後挪 {counts.get(STATUS_SHIFTED, 0)}、"
        f"跳過 {counts.get(STATUS_SKIPPED, 0)}"
    )
    print(f"預估錄影總時長（含前後緩衝）：{format_delta(timedelta(seconds=total))}")
    print("圖例：→ = 時間往後挪過，× = 直播撞期需自行取捨")
    if counts.get(STATUS_SKIPPED):
        return 2
    return 0


def _login_prompt(cfg: Config, navigator: Navigator, *, closing: bool) -> None:
    """開 AU2026 頁面，停下來等使用者登入並把視窗擺到要錄的螢幕。"""
    url = str(cfg.get("browser", "login_url"))
    if isinstance(navigator, PlaywrightNavigator):
        try:
            navigator.show_for_login(url)
        except Exception as exc:
            print(f"! 開登入頁失敗（{exc}），請自己在那個視窗打開 AU2026")
    else:
        navigator.open_session(url)
    print()
    print("瀏覽器已開啟，請在那個視窗裡：")
    print("  1. 登入 Autodesk，確認你點得進課程播放頁")
    print("  2. 把視窗拖到 OBS 要錄的那個螢幕，並設成全螢幕")
    print(f"登入狀態會留在 {cfg.resolve('browser', 'user_data_dir')}，下次不用再登")
    print()
    print("完成後回到這裡按 Enter " + ("關閉瀏覽器。" if closing else "開始排程待機。"))
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt from None


def cmd_login(args: argparse.Namespace) -> int:
    cfg = _load(args)
    setup_logging(None, args.verbose)
    if args.url:
        cfg.data["browser"]["login_url"] = args.url
    navigator = _make_navigator(cfg)
    try:
        _login_prompt(cfg, navigator, closing=True)
    except BrowserError as exc:
        print(f"啟動瀏覽器失敗：{exc}")
        return 1
    finally:
        navigator.close()
    print("已儲存登入狀態。")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = _load(args)
    setup_logging(None, args.verbose)
    url = args.url
    if "://" not in url:  # 不像網址就當成課程代碼，去對照表查
        catalog = load_catalog(cfg.resolve("schedule", "catalog"))
        entry = catalog.get(url.upper())
        if not entry or not entry.get("url"):
            print(f"對照表裡找不到 {url}，請直接給網址")
            return 1
        url = str(entry["url"])
    navigator = _make_navigator(cfg)
    if not isinstance(navigator, PlaywrightNavigator):
        navigator.close()
        print(
            "probe 需要能控制瀏覽器（[browser] mode 要是 launch 或 attach）；"
            "目前是 open 模式，沒有辦法檢查頁面元素。"
        )
        return 1
    try:
        elements = navigator.probe(url, limit=args.limit)
    except BrowserError as exc:
        print(f"失敗：{exc}")
        return 1
    finally:
        try:
            navigator.close()
        except Exception:
            pass
    print(f"\n{url}\n可點元素（{len(elements)}）：")
    for element in elements:
        parts = [element["tag"]]
        if element["text"]:
            parts.append(f"text={element['text']!r}")
        if element["aria"]:
            parts.append(f"aria={element['aria']!r}")
        if element["id"]:
            parts.append(f"#{element['id']}")
        if element["cls"]:
            parts.append(f".{element['cls'].split()[0] if element['cls'].split() else ''}")
        print("  " + "  ".join(parts))
    print("\n把命中播放的那一個寫進 config.toml 的 [browser] play_selectors，例如：")
    print("  \"button:has-text('Watch now')\"  或  \"#playButton\"")
    return 0


# ── 引導設定精靈 ────────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "") -> str:
    suffix = f"（直接按 Enter = {default}）" if default else ""
    try:
        answer = input(f"{prompt}{suffix}：").strip()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt from None
    return answer or default


def _confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = _ask(f"{prompt} [{hint}]").lower()
    if not answer:
        return default
    return answer.startswith("y")


def pick_file_dialog(title: str) -> str | None:
    """開系統的檔案選取視窗。沒有 tkinter（或跑在沒桌面的環境）就回 None。"""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askopenfilename(
            title=title,
            filetypes=[("課表檔", "*.csv *.ics"), ("所有檔案", "*.*")],
            initialdir=str(Path.home() / "Downloads"),
        )
        root.destroy()
    except Exception:
        return None
    return chosen or None


def _toml_path(value: Path) -> str:
    """TOML 字面字串：單引號內不處理跳脫，Windows 路徑的反斜線才不會出事。"""
    return "'" + str(value).replace("'", "") + "'"


def _step(number: int, total: int, title: str) -> None:
    print(f"\n{'─' * 62}\n【步驟 {number}/{total}】{title}\n")


def cmd_setup(args: argparse.Namespace) -> int:
    """第一次使用的引導設定：一路問到底，問完就能開始錄。"""
    setup_console()
    total = 6
    config_path = Path(args.config or "config.toml")

    print("\n" + "=" * 62)
    print("  au2026rec 引導設定")
    print("=" * 62)
    print(f"\n⚠ {DISCLAIMER}\n")
    print("這個精靈會幫你把 OBS、螢幕、課表、登入全部設定好。")
    print("任何一步都可以按 Ctrl-C 中斷，已經設定好的不會不見。")

    try:
        # ── 1. 設定檔 ───────────────────────────────────────────────────
        _step(1, total, "設定檔")
        if config_path.exists():
            print(f"已經有設定檔：{config_path.resolve()}")
        else:
            args.force = False
            args.quiet = True
            if cmd_init(args) != 0:
                return 1
            print("（OBS 密碼、場景、課表等等，接下來幾步會幫你填好）")

        # ── 2. OBS 連線 ────────────────────────────────────────────────
        _step(2, total, "OBS 連線")
        while True:
            info = obslocal.inspect(None)
            websocket = info.websocket
            if websocket is None:
                print("找不到 OBS 的設定檔 —— OBS 至少要開啟過一次。")
                if not _confirm("已經開過 OBS 了，再檢查一次？"):
                    return 1
                continue
            if websocket.enabled:
                print(f"OBS WebSocket 已啟用（埠號 {websocket.port}）")
                break
            print("OBS 的 WebSocket 伺服器還沒啟用，程式可以幫你開。")
            if obslocal.obs_is_running():
                print("但 OBS 現在是開著的 —— 這時候改設定會在 OBS 關閉時被蓋掉。")
                _ask("請先完全關閉 OBS，然後按 Enter 繼續")
                continue
            backup, websocket = obslocal.enable_websocket(websocket)
            print(f"✓ 已啟用（原設定備份在 {backup.name}）")
            break

        changes = obslocal.apply_to_config(
            config_path, port=websocket.port, password=websocket.password
        )
        print(f"✓ 密碼與埠號已寫入設定檔{('：' + '、'.join(changes)) if changes else '（原本就是對的）'}")

        print("\n接下來要用 OBS 建立錄課場景，請現在開啟 OBS。")
        _ask("OBS 開好了就按 Enter")

        cfg = load_config(config_path)
        obs = _make_obs(cfg)
        try:
            print(f"✓ 連上 {obs.connect()}")
        except ObsError as exc:
            print(f"✗ {exc}")
            return 1

        try:
            client = obs._require()  # noqa: SLF001

            # ── 3. 選螢幕、建場景 ──────────────────────────────────────
            _step(3, total, "要錄哪一個螢幕")
            monitors = obsscene.list_monitors(client)
            if not monitors:
                print("讀不到螢幕清單，請在 OBS 裡自己建場景後回來。")
            else:
                for monitor in monitors:
                    print(f"  {monitor}")
                print("\n把瀏覽器擺在哪個螢幕，就選哪個。")
                while True:
                    raw = _ask("螢幕編號", str(monitors[0].index))
                    chosen = next((m for m in monitors if str(m.index) == raw), None)
                    if chosen:
                        break
                    print("沒有這個編號，再選一次")
                scene = "AU2026 錄課"
                for note in obsscene.ensure_recording_scene(
                    client, scene=scene, monitor=chosen, desktop_audio=True, switch_to=True
                ):
                    print(f"  · {note}")
                set_value(config_path, "obs", "scene", f'"{scene}"')
                print(f"✓ 場景「{scene}」已就緒，錄影前會自動切過去")

            # ── 4. 試錄 ────────────────────────────────────────────────
            _step(4, total, "試錄一段，確認畫面與聲音")
            print("接下來錄 8 秒。**現在先在那個螢幕上放點會動、有聲音的東西**（例如播一段影片），")
            print("這樣才驗得出畫面跟收音都正常。")
            if _confirm("開始試錄？"):
                name = f"au2026rec_setup_{datetime.now():%H%M%S}"
                obs.start_recording(name)
                for remaining in range(8, 0, -1):
                    print(f"  錄影中… {remaining}", end="\r", flush=True)
                    time.sleep(1)
                path = obs.stop_recording()
                obs.restore_filename_format()
                print(f"\n✓ 輸出：{path or '（OBS 未回報路徑，去 OBS 的錄影資料夾看）'}")
                if path and Path(path).exists():
                    size = Path(path).stat().st_size / 1024 / 1024
                    print(f"  大小 {size:.1f} MB")
                    if size < 0.05:
                        print("  ✗ 檔案幾乎是空的 —— 場景可能沒有畫面來源，回 OBS 檢查")
                print("\n請打開那個檔案看一下：畫面對不對？有沒有聲音？")
                if not _confirm("沒問題，繼續？"):
                    print("請在 OBS 裡調整場景後，再跑一次引導設定。")
                    return 1
        finally:
            obs.close()

        # ── 5. 課表 ────────────────────────────────────────────────────
        _step(5, total, "你的課表")
        print("課表要從 AU2026 網站匯出（登入後進 My Schedule，找 Export / Download），")
        print("下載到的 CSV 或 ICS 都可以。")
        current = cfg.resolve("schedule", "file")
        if current.exists():
            print(f"\n目前設定的課表：{current}")
        if not current.exists() or _confirm("要改用別的課表檔嗎？", default=not current.exists()):
            chosen_file = pick_file_dialog("選擇你從 AU2026 匯出的課表")
            if chosen_file is None:
                chosen_file = _ask("找不到檔案選取視窗，請直接貼上課表檔的完整路徑")
            schedule_path = Path(chosen_file.strip('"')) if chosen_file else None
            if schedule_path and schedule_path.exists():
                set_value(config_path, "schedule", "file", _toml_path(schedule_path))
                print(f"✓ 課表設定為 {schedule_path}")
            else:
                print("! 沒有選到檔案，維持原設定")

        cfg = load_config(config_path)
        catalog_path = cfg.resolve("schedule", "catalog")
        if not catalog_path.exists():
            print("\n建立課程網址對照表…")
            packaged = bundled("catalog.json")
            sources = resolve_sources([], cfg.root) + ([packaged] if packaged.exists() else [])
            catalog, _ = build_catalog(sources)
            write_catalog(catalog, catalog_path)
            print(f"✓ 對照表已建立（{len(catalog)} 筆）")

        sessions, warnings = _load_sessions(cfg)
        if warnings:
            print("\n提醒：")
            _print_warnings(warnings[:5])
        if sessions:
            print()
            print(_plan_table(cfg, _build_plan(cfg, sessions)))
        else:
            print("! 課表裡沒有讀到可用的場次，請確認匯出的檔案內容")

        # ── 6. 登入 ────────────────────────────────────────────────────
        _step(6, total, "登入 AU2026")
        if _confirm("現在開瀏覽器登入？（登入狀態會記住，之後不用再登）"):
            navigator = _make_navigator(cfg)
            try:
                _login_prompt(cfg, navigator, closing=True)
            finally:
                navigator.close()

        print("\n" + "=" * 62)
        print("  設定完成")
        print("=" * 62)
        print("\n正式開始錄影：回主選單選 6（或執行 au2026rec run）")
        print("⚠ 第一場請盯著前 5 分鐘 —— 真實課程頁的播放鍵還沒驗證過，")
        print("  詳細判斷方式看「使用說明.md」第六節。")
        return 0

    except KeyboardInterrupt:
        print("\n\n已中斷。已經設定好的部分都留著，隨時可以再跑一次引導設定。")
        return 130


def cmd_display(args: argparse.Namespace) -> int:
    """列出 OBS 認得的螢幕，並建立/修好專屬的錄課場景。"""
    cfg = _load(args)
    setup_logging(None, args.verbose)
    obs = _make_obs(cfg)
    try:
        print(f"OBS：{obs.connect()}")
        client = obs._require()  # noqa: SLF001 - 場景設定要用到原生 request client

        monitors = obsscene.list_monitors(client)
        print("\nOBS 認得的螢幕：")
        for monitor in monitors:
            print(f"  {monitor}")
        if not monitors:
            print("  （讀不到，請確認 OBS 版本支援螢幕擷取）")

        scene = args.scene or str(cfg.get("obs", "scene")) or "AU2026 錄課"
        if args.use is None:
            print(f"\n目前設定要用的場景：{str(cfg.get('obs', 'scene')) or '（未設定，錄影前不切換）'}")
            existing = obs.scene_names()
            print(f"OBS 現有場景：{', '.join(existing) or '（無）'}")
            for name in existing:
                audio = obsscene.audio_sources_in_scene(client, name)
                if not audio:
                    print(f"  · {name}：沒有音訊來源 → 錄起來會沒聲音")
                    continue
                for source in audio:
                    state = "啟用" if source["enabled"] else "停用"
                    muted = "、靜音" if source.get("muted") else ""
                    target = source["settings"].get("window") or source["settings"].get("device_id", "")
                    print(f"  · {name}：{source['name']}（{source['kind']}，{state}{muted}）{str(target)[:60]}")
            print(
                f"\n選好螢幕後執行：au2026rec display --use N"
                f"（會建立場景「{scene}」：螢幕擷取 + 桌面音訊，並寫回設定）"
            )
            return 0

        chosen = next((m for m in monitors if m.index == args.use), None)
        if chosen is None:
            print(f"\n✗ 沒有第 {args.use} 個螢幕，請從上面清單挑編號")
            return 1
        print(f"\n建立錄課場景「{scene}」，畫面來源 = {chosen.name}")
        for note in obsscene.ensure_recording_scene(
            client,
            scene=scene,
            monitor=chosen,
            desktop_audio=not args.no_desktop_audio,
            switch_to=True,
        ):
            print(f"  · {note}")

        changed = set_value(Path(args.config or "config.toml"), "obs", "scene", f'"{scene}"')
        print(f"  · config.toml：{'已把 [obs] scene 設為 ' + scene if changed else '[obs] scene 已經是 ' + scene}")
        print("\n接著：")
        print("  1. au2026rec obs-test --record-seconds 8   確認這個場景錄得出東西、有聲音")
        print(f"  2. au2026rec login                        把瀏覽器視窗拖到 {chosen.name} 並登入")
        return 0
    except (ObsError, obsscene.SceneError) as exc:
        print(f"✗ {exc}")
        return 1
    finally:
        obs.close()


def cmd_obs_doctor(args: argparse.Namespace) -> int:
    """讀本機 OBS 設定，回報 WebSocket 與錄影輸出狀態，必要時自動填進 config.toml。"""
    info = obslocal.inspect(args.profile)
    print(f"OBS 設定目錄：{info.root}")
    print(f"OBS 執行檔：  {info.executable or '（找不到，手動開 OBS 也行）'}")
    print(f"OBS 執行中：  {'是' if obslocal.obs_is_running() else '否'}")
    problems: list[str] = []

    websocket = info.websocket
    print()
    if websocket is None:
        print("WebSocket：找不到設定檔 —— OBS 需要先開啟過一次")
        problems.append("OBS 從未產生 WebSocket 設定檔")
    else:
        print(f"WebSocket：{'已啟用' if websocket.enabled else '未啟用'}"
              f"  埠號 {websocket.port}"
              f"  驗證 {'需要密碼' if websocket.auth_required else '不需密碼'}")
        print(f"           密碼 {websocket.password or '（空）'}")
        if not websocket.enabled:
            problems.append("WebSocket 伺服器未啟用")

    profile = info.profile
    print()
    if profile is None:
        print("Profile：讀不到")
        problems.append("讀不到 OBS profile")
    else:
        print(f"Profile：  {profile.name}（{profile.mode} 模式）")
        print(f"  錄影輸出：{profile.rec_path or '（未設定）'}")
        print(f"  格式：    {profile.rec_format or '?'}   編碼器：{profile.encoder or '?'}")
        print(f"  解析度：  {profile.base_resolution} → {profile.output_resolution} @ {profile.fps or '?'} fps")
        print(f"  檔名樣板：{profile.filename_formatting or '（未設定）'}")
        if not profile.rec_path:
            problems.append("profile 沒有設定錄影輸出資料夾")
        elif not Path(profile.rec_path).is_dir():
            problems.append(f"錄影輸出資料夾不存在：{profile.rec_path}")
        if profile.rec_format.lower() == "mp4":
            print("  ! mp4 在錄影中斷電會整檔壞掉，長時間無人值守建議改用 mkv")
    if info.profiles:
        print(f"  其他 profile：{', '.join(info.profiles)}")

    print()
    print(f"場景集合：{info.scene_collection or '?'}")
    print(f"  場景：  {', '.join(info.scenes) or '（讀不到）'}")

    if args.enable_websocket and websocket is not None and not websocket.enabled:
        print()
        backup, websocket = obslocal.enable_websocket(websocket)
        print(f"✓ 已啟用 OBS WebSocket 伺服器（原設定備份在 {backup.name}）")
        problems = [p for p in problems if p != "WebSocket 伺服器未啟用"]

    if args.apply:
        if websocket is None:
            print("\n✗ 沒有 WebSocket 設定可套用")
            return 1
        cfg_path = Path(args.config or "config.toml")
        changes = obslocal.apply_to_config(
            cfg_path, port=websocket.port, password=websocket.password
        )
        print()
        if changes:
            print(f"✓ 已更新 {cfg_path}：{'、'.join(changes)}")
        else:
            print(f"✓ {cfg_path} 的 [obs] 設定已經是對的，沒有改動")

    print()
    if problems:
        print("需要處理：")
        for problem in problems:
            print(f"  ✗ {problem}")
        if any("未啟用" in p for p in problems):
            print("    → 關掉 OBS 後執行 au2026rec obs-doctor --enable-websocket --apply")
            print("    → 或在 OBS 裡：工具 → WebSocket 伺服器設定 → 勾選「啟用 WebSocket 伺服器」")
        return 1
    print("✓ OBS 這端看起來沒問題，接著跑 au2026rec obs-test --record-seconds 8")
    return 0


def cmd_obs_test(args: argparse.Namespace) -> int:
    cfg = _load(args)
    setup_logging(None, args.verbose)
    obs = _make_obs(cfg)
    try:
        version = obs.connect()
    except ObsError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ 已連上 {version}")
    scenes = obs.scene_names()
    print(f"  場景：{', '.join(scenes) or '（無）'}")
    wanted = str(cfg.get("obs", "scene"))
    if wanted:
        if wanted in scenes:
            print(f"  ✓ 設定的場景 {wanted!r} 存在")
        else:
            print(f"  ✗ 設定的場景 {wanted!r} 不存在")
    print(f"  檔名樣板：{obs.filename_format() or '（讀不到）'}")
    print(f"  目前錄影中：{'是' if obs.is_recording() else '否'}")
    if not args.record_seconds:
        obs.close()
        print("\n加上 --record-seconds 8 可以實際錄一段來驗證整條路徑")
        return 0

    wanted = f"au2026rec_test_{datetime.now():%Y%m%d_%H%M%S}"
    print(f"\n  試錄 {args.record_seconds} 秒（檔名 {wanted}）…")
    try:
        obs.start_recording(wanted)
        time.sleep(args.record_seconds)
        path = obs.stop_recording()
    except ObsError as exc:
        print(f"  ✗ {exc}")
        obs.close()
        return 1
    finally:
        obs.restore_filename_format()

    if not path:
        print("  ! OBS 沒回報輸出路徑（OBS 版本較舊），請自己到錄影資料夾確認")
        obs.close()
        return 1

    # OBS 停止錄影後還在寫檔尾，稍等一下再量大小。
    output = Path(path)
    for _ in range(20):
        if output.exists() and output.stat().st_size > 0:
            break
        time.sleep(0.5)
    print(f"  輸出：{output}")
    if not output.exists():
        print("  ✗ 檔案不存在 —— 檢查 OBS 的錄影資料夾權限與硬碟空間")
        obs.close()
        return 1
    size_mb = output.stat().st_size / 1024 / 1024
    print(f"  大小：{size_mb:.1f} MB（{size_mb / max(args.record_seconds, 1) * 60:.0f} MB/分鐘）")
    if size_mb < 0.05:
        print("  ✗ 檔案幾乎是空的 —— 場景可能沒有任何來源，或編碼器啟動失敗（看 OBS 的 log）")
        obs.close()
        return 1
    if wanted not in output.name:
        print(f"  ! 檔名沒帶上 {wanted}，自動命名可能無效，錄出來的檔會用 OBS 預設名稱")
    print(f"  ✓ 錄影這段通了。長度換算：一場 90 分鐘的課約 {size_mb / max(args.record_seconds, 1) * 5400:.0f} MB")
    obs.close()
    return 0


def _run(cfg: Config, items: Sequence[PlanItem], args: argparse.Namespace) -> int:
    setup_logging(cfg.resolve("paths", "log_file"), args.verbose)
    obs: ObsController = _make_obs(cfg)
    try:
        print(f"OBS：{obs.connect()}")
    except ObsError as exc:
        if not bool(cfg.get("obs", "continue_without_obs")):
            print(f"✗ {exc}")
            return 1
        print(f"! {exc}")
        print("! continue_without_obs = true，改為只開課不錄影")
        obs = NullObsController()

    try:
        navigator = _make_navigator(cfg)
    except BrowserError as exc:
        print(f"✗ {exc}")
        obs.close()
        return 1

    if bool(cfg.get("browser", "wait_for_login")) and not args.yes:
        try:
            _login_prompt(cfg, navigator, closing=False)
        except KeyboardInterrupt:
            navigator.close()
            obs.close()
            print("\n已取消")
            return 130

    options = RunOptions(
        gap_seconds=int(cfg.get("recording", "gap_seconds")),
        scene=str(cfg.get("obs", "scene")),
        local_tz=get_zone(str(cfg.get("schedule", "local_timezone"))),
        report_file=cfg.resolve("paths", "report_file"),
        skip_past=not args.include_past,
    )
    runner = Runner(navigator, obs, options)
    try:
        rows = runner.run(items)
    finally:
        navigator.close()
        obs.close()

    print()
    print(f"報告：{options.report_file}")
    failed = [r for r in rows if r["result"] not in {"recorded", "skipped", "past"}]
    for row in failed:
        print(f"  ✗ {row['code']} {row['title']}：{row['result']} {row['note']}")
    return 1 if failed else 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args)
    sessions, warnings = _load_sessions(cfg)
    if warnings:
        print("提醒：")
        _print_warnings(warnings)
    if not sessions:
        print("課表沒有可用的場次")
        return 1
    items = _build_plan(cfg, sessions)
    if args.only:
        wanted = {code.upper() for code in args.only}
        items = [item for item in items if item.session.code.upper() in wanted]
        if not items:
            print(f"課表裡找不到 {', '.join(sorted(wanted))}")
            return 1
    print(_plan_table(cfg, items))
    print()
    print(f"⚠ {DISCLAIMER}")
    print()
    if not args.yes:
        try:
            answer = input("以上時間軸正確嗎？按 Enter 開始待機，輸入 n 取消：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 130
        if answer.startswith("n"):
            print("已取消")
            return 0
    return _run(cfg, items, args)


def cmd_test_record(args: argparse.Namespace) -> int:
    """拿一場真實課程跑完整流程，但只錄短短幾十秒，用來驗證接線。"""
    cfg = _load(args)
    sessions, warnings = _load_sessions(cfg)
    if warnings:
        _print_warnings(warnings)
    target = args.code.upper()
    matches = [s for s in sessions if s.code.upper() == target]
    if not matches:
        print(f"課表裡找不到 {target}，可用的有：{', '.join(s.code for s in sessions)}")
        return 1

    session = matches[0]
    lead = min(int(cfg.get("recording", "lead_seconds")), 10)
    start = now_utc() + timedelta(seconds=lead + 2)
    session.start = start
    session.end = start + timedelta(seconds=args.seconds)
    items = build_plan(
        [session],
        live_modes=cfg.get("schedule", "live_modes"),
        overlap_policy="keep",
        lead_seconds=lead,
        tail_seconds=0,
        gap_seconds=0,
        filename_template="au2026rec_test_" + str(cfg.get("recording", "filename_template")),
        filename_max_length=int(cfg.get("recording", "filename_max_length")),
        local_tz=get_zone(str(cfg.get("schedule", "local_timezone"))),
    )
    print(f"試錄 {session.label()}，約 {args.seconds} 秒後結束")
    return _run(cfg, items, args)


# ── 參數 ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="au2026rec",
        description="把 AU2026 已選課 CSV 變成自動開課 + OBS 錄影的排程器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"au2026rec {__version__}")
    parser.add_argument("-c", "--config", help="設定檔路徑（預設 config.toml）")
    parser.add_argument("-v", "--verbose", action="store_true", help="輸出除錯訊息")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="產生 config.toml")
    p_init.add_argument("--force", action="store_true", help="覆蓋既有設定檔")
    p_init.set_defaults(func=cmd_init)

    p_catalog = sub.add_parser("catalog", help="建立 code → 課程網址 對照表")
    p_catalog.add_argument("--source", action="append", help="來源 HTML/JSON，可重複指定")
    p_catalog.set_defaults(func=cmd_catalog)

    p_validate = sub.add_parser("validate", help="檢查課表與網址")
    p_validate.set_defaults(func=cmd_validate)

    p_plan = sub.add_parser("plan", help="印出實際錄影時間軸")
    p_plan.set_defaults(func=cmd_plan)

    p_login = sub.add_parser("login", help="開瀏覽器手動登入 Autodesk")
    p_login.add_argument("--url", help="要開的網址（預設 AU2026 數位課程目錄）")
    p_login.set_defaults(func=cmd_login)

    p_probe = sub.add_parser("probe", help="列出頁面可點元素，用來補 play_selectors")
    p_probe.add_argument("url", help="課程網址或 session code")
    p_probe.add_argument("--limit", type=int, default=40)
    p_probe.set_defaults(func=cmd_probe)

    p_setup = sub.add_parser("setup", help="第一次使用：引導式設定（推薦）")
    p_setup.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    p_setup.set_defaults(func=cmd_setup)

    p_display = sub.add_parser("display", help="列出螢幕並建立專屬的錄課場景")
    p_display.add_argument("--use", type=int, help="用第幾個螢幕（編號取自本指令列出的清單）")
    p_display.add_argument("--scene", help="場景名稱（預設 AU2026 錄課）")
    p_display.add_argument("--no-desktop-audio", action="store_true", help="不要加桌面音訊來源")
    p_display.set_defaults(func=cmd_display)

    p_doctor = sub.add_parser("obs-doctor", help="檢查本機 OBS 設定，可自動填入密碼")
    p_doctor.add_argument("--profile", help="指定要檢查的 OBS profile（預設目前使用中的）")
    p_doctor.add_argument("--apply", action="store_true", help="把埠號與密碼寫進 config.toml")
    p_doctor.add_argument(
        "--enable-websocket",
        action="store_true",
        help="幫你把 OBS 的 WebSocket 伺服器打開（需先關閉 OBS，會先備份原設定）",
    )
    p_doctor.set_defaults(func=cmd_obs_doctor)

    p_obs = sub.add_parser("obs-test", help="測 OBS 連線")
    p_obs.add_argument("--record-seconds", type=int, default=0, help="順便試錄幾秒")
    p_obs.set_defaults(func=cmd_obs_test)

    p_test = sub.add_parser("test-record", help="單場試錄，驗證整條流程")
    p_test.add_argument("code", help="課表裡的 session code，例如 KEY1001-D")
    p_test.add_argument("--seconds", type=int, default=30, help="錄幾秒（預設 30）")
    p_test.add_argument("--include-past", action="store_true", help=argparse.SUPPRESS)
    p_test.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    p_test.set_defaults(func=cmd_test_record)

    p_run = sub.add_parser("run", help="照課表無人值守執行")
    p_run.add_argument("--only", action="append", help="只錄這些 session code，可重複指定")
    p_run.add_argument("--include-past", action="store_true", help="不要略過已結束的場次")
    p_run.add_argument("-y", "--yes", action="store_true", help="不要問確認，直接開始待機")
    p_run.set_defaults(func=cmd_run)

    return parser


MENU = [
    ("1", "★ 第一次使用：一步一步幫你設定好", ["setup"]),
    ("2", "檢查 OBS 設定（密碼、錄影資料夾、場景）", ["obs-doctor"]),
    ("3", "列出螢幕、建立錄課場景", ["display"]),
    ("4", "試錄 8 秒，確認畫面與聲音都正常", ["obs-test", "--record-seconds", "8"]),
    ("5", "檢查課表、看錄影時間軸", ["plan"]),
    ("6", "開瀏覽器登入 AU2026（登入一次就好）", ["login"]),
    ("7", "▶ 開始排程錄影", ["run"]),
    ("8", "找播放鍵選擇器（需要輸入課程代碼）", ["probe"]),
    ("9", "重新建立課程網址對照表", ["catalog"]),
    ("i", "只產生設定檔 config.toml", ["init"]),
]


def interactive_menu() -> int:
    """沒帶參數執行時（例如在檔案總管點兩下 exe）給的選單。"""
    parser = build_parser()
    print(f"\nau2026rec {__version__} — AU2026 自動開課 + OBS 錄影")
    print(f"⚠ {DISCLAIMER}\n")
    print(f"工作目錄：{Path.cwd()}")
    config_path = Path("config.toml")
    if not config_path.exists():
        print("！這個資料夾還沒有設定檔。直接選 1，精靈會從頭帶你設定好。")

    while True:
        print("\n" + "─" * 60)
        for key, label, _ in MENU:
            print(f"  {key})  {label}")
        print("  0)  離開")
        try:
            choice = input("\n請輸入編號：").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice in {"0", "q", "Q", ""}:
            return 0
        entry = next((m for m in MENU if m[0] == choice), None)
        if entry is None:
            print("沒有這個選項")
            continue

        argv = list(entry[2])
        if argv[0] == "probe":
            try:
                code = input("課程代碼或網址（例如 KEY1001-D）：").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if not code:
                continue
            argv.append(code)

        print()
        try:
            args = parser.parse_args(argv)
            code = int(args.func(args))
        except SystemExit as exc:  # argparse 的錯誤不該讓選單整個結束
            code = int(exc.code or 0)
        except (ConfigError, ScheduleError, CatalogError, obslocal.ObsLocalError, ObsError) as exc:
            print(f"✗ {exc}")
            code = 1
        except KeyboardInterrupt:
            print("\n已中斷")
            code = 130
        print(f"\n（結束，代碼 {code}）")


def main(argv: Sequence[str] | None = None) -> int:
    setup_console()
    if argv is None and len(sys.argv) == 1 and sys.stdin is not None and sys.stdin.isatty():
        # 點兩下 exe 或直接執行不帶參數 → 給選單，不要丟一堆 usage 出來
        return interactive_menu()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, ScheduleError, CatalogError, obslocal.ObsLocalError, ObsError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中斷", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
