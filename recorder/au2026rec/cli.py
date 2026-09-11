"""au2026rec 命令列介面。

    au2026rec init                 產生 config.toml
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

DISCLAIMER = (
    "本工具錄影僅供個人學習與課後複習；請遵守 AU 使用條款與著作權法。"
    "違法或侵權使用與開發者無關，詳見 README 的免責聲明。"
)

PACKAGE_ROOT = Path(__file__).resolve().parent
EXAMPLE_CONFIG = PACKAGE_ROOT.parent / "config.example.toml"


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
    if not url.startswith("http"):
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


def main(argv: Sequence[str] | None = None) -> int:
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
