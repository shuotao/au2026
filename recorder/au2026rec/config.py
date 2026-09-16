"""設定檔載入：TOML + 內建預設值，路徑一律相對於設定檔所在目錄。"""
from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, dict[str, Any]] = {
    "schedule": {
        "file": "my_schedule.csv",
        "catalog": "catalog.json",
        "source_timezone": "America/Los_Angeles",
        "local_timezone": "Asia/Taipei",
        "default_duration_minutes": 60,
        "event_year": 2026,
        "live_modes": ["live", "live-streamed", "live streamed", "livestream", "直播"],
        "overlap_policy": "shift",
        "status_include": ["scheduled", "registered", "confirmed", "已排定", ""],
    },
    "recording": {
        "lead_seconds": 90,
        "tail_seconds": 120,
        "gap_seconds": 15,
        "filename_template": "{start_local:%Y%m%d_%H%M}_{code}_{title}",
        "filename_max_length": 120,
        # 錄影期間每隔幾秒確認影片還在播；0 = 不監看
        "watch_every": 30,
        "watch_max_reloads": 3,
    },
    "obs": {
        "host": "localhost",
        "port": 4455,
        "password": "",
        "scene": "",
        "continue_without_obs": False,
    },
    "browser": {
        "mode": "attach",
        "cdp_url": "http://localhost:9222",
        "fallback_to_open": True,
        "login_url": "https://conferences.autodesk.com/flow/autodesk/au2026/sessioncatalog/page/digital",
        "attach_profile_dir": "attach-profile",
        "allow_autoplay": False,
        # 開登入用瀏覽器時要多帶的旗標，原樣接在命令列後面（預設不帶）。
        # 留給「只能靠瀏覽器旗標解」的環境問題，例如某條線路連 CDN 特別慢時
        # 用 --host-resolver-rules 把它導到別的位址。
        "extra_args": [],
        "settle_seconds": 8,
        "play_selectors": [
            # 2026-09-15 真實課程頁實測：AU 用 video.js（Brightcove）。
            # 只列「明確是按鈕」的選擇器 —— 不要放 video 或播放器容器，
            # 點那些會 toggle 暫停，直播進頁面就自動播，點它等於把直播按停。
            ".vjs-big-play-button",
            "button:has-text('Play Video')",
            "button:has-text('Watch now')",
            "button:has-text('Join session')",
            "button:has-text('Play')",
            "a:has-text('Watch now')",
        ],
        "dismiss_selectors": [
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('Got it')",
            "button[aria-label='Close']",
        ],
        "center_player": True,
        "unmute": True,
        "preferred_height": 1080,
        "close_page_after": True,
    },
    "library": {
        # 留空 = 放在使用者的「影片」資料夾底下（AU2026）。課程影片動輒幾十 GB，
        # 不該躺在程式資料夾裡跟著一起被搬、被打包、被刪。
        "root": "",
        "enabled": True,
        "attachments": True,
        "subtitles": True,
        "download_height": 720,
        "ffmpeg": "ffmpeg",
        "manifest_wait_seconds": 45,
    },
    "paths": {
        "log_file": "logs/au2026rec.log",
        "report_file": "logs/sessions.csv",
    },
}

_PATH_KEYS = {
    ("schedule", "file"),
    ("schedule", "catalog"),
    ("browser", "attach_profile_dir"),
    ("paths", "log_file"),
    ("paths", "report_file"),
}


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    path: Path
    root: Path
    data: dict[str, dict[str, Any]] = field(default_factory=dict)

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    def get(self, section: str, key: str) -> Any:
        try:
            return self.data[section][key]
        except KeyError as exc:  # pragma: no cover - 設定表寫死，理論上不會缺
            raise ConfigError(f"設定缺少 [{section}] {key}") from exc

    def resolve(self, section: str, key: str) -> Path:
        return (self.root / str(self.get(section, key))).resolve()

    def library_root(self) -> Path:
        """課程資料夾要放哪。留空就放使用者的「影片」資料夾底下。"""
        configured = str(self.get("library", "root")).strip()
        if configured:
            return (self.root / configured).resolve()
        return videos_dir() / "AU2026"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def videos_dir() -> Path:
    """使用者的「影片」資料夾。

    Windows 上這個位置可以被搬到別的碟，所以先問登錄檔，不要寫死 ~/Videos。
    """
    if sys.platform == "win32":
        try:
            import winreg

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value = winreg.QueryValueEx(handle, "My Video")[0]
            if value and Path(value).is_dir():
                return Path(value)
        except Exception:
            pass  # 讀不到就用家目錄底下的慣例位置
    return Path.home() / "Videos"


def app_dir() -> Path:
    """程式所在的資料夾。打包成 exe 時是 exe 旁邊，否則是套件的上層目錄。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_candidates(path: str | Path | None) -> list[Path]:
    """設定檔的尋找順序：指定路徑 → 目前工作目錄 → 程式所在資料夾。"""
    if path:
        return [Path(path)]
    seen: list[Path] = []
    for candidate in (Path.cwd() / "config.toml", app_dir() / "config.toml"):
        resolved = candidate.resolve()
        if resolved not in [p.resolve() for p in seen]:
            seen.append(candidate)
    return seen


def load_config(path: str | Path | None) -> Config:
    """讀取設定檔。沒指定路徑時，先找工作目錄，再找程式所在資料夾。"""
    candidates = config_candidates(path)
    candidate = next((c for c in candidates if c.exists()), None)
    if candidate is None:
        looked = "\n".join(f"    {c.resolve()}" for c in candidates)
        raise ConfigError(
            "找不到設定檔 config.toml。找過這些地方：\n"
            f"{looked}\n"
            "  執行 `au2026rec init`（或在選單選 9）在目前資料夾產生一份，\n"
            "  記得把你的課表 CSV 也放到同一個資料夾。"
        )
    with candidate.open("rb") as fh:
        user_data = tomllib.load(fh)
    unknown = set(user_data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"設定檔有不認識的區段：{', '.join(sorted(unknown))}")
    merged = _merge(DEFAULTS, user_data)
    cfg = Config(path=candidate.resolve(), root=candidate.resolve().parent, data=merged)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    policy = str(cfg.get("schedule", "overlap_policy")).lower()
    if policy not in {"shift", "skip", "keep"}:
        raise ConfigError(f"[schedule] overlap_policy 只能是 shift / skip / keep，讀到 {policy!r}")
    for section, key in (("recording", "lead_seconds"), ("recording", "tail_seconds"), ("recording", "gap_seconds")):
        value = cfg.get(section, key)
        if not isinstance(value, int) or value < 0:
            raise ConfigError(f"[{section}] {key} 必須是 0 或正整數，讀到 {value!r}")
    extra_args = cfg.get("browser", "extra_args")
    if not isinstance(extra_args, list) or any(not isinstance(a, str) for a in extra_args):
        raise ConfigError(
            "[browser] extra_args 必須是字串陣列，例如 "
            'extra_args = ["--host-resolver-rules=MAP example.com 1.2.3.4"]'
            f"，讀到 {extra_args!r}"
        )

    mode = str(cfg.get("browser", "mode")).lower()
    if mode == "launch":
        # 舊版的模式。不要因為設定檔沒跟著改就整個跑不動 —— 直接當成 attach。
        cfg.data["browser"]["mode"] = "attach"
    elif mode not in {"attach", "open"}:
        raise ConfigError(f"[browser] mode 只能是 attach / open，讀到 {mode!r}")


def set_value(path: Path, section: str, key: str, literal: str) -> bool:
    """就地改寫設定檔裡的一個值，保留註解與排版。literal 要是合法的 TOML 值。"""
    if not path.exists():
        raise ConfigError(f"找不到 {path}，先執行 au2026rec init")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            if in_section:
                break
            in_section = stripped == f"[{section}]"
            continue
        if in_section and stripped.split("=")[0].strip() == key:
            if stripped == f"{key} = {literal}":
                return False
            lines[index] = f"{key} = {literal}\n"
            path.write_text("".join(lines), encoding="utf-8")
            return True
    raise ConfigError(f"設定檔裡找不到 [{section}] 的 {key}，請比對 config.example.toml")


def write_example_config(target: Path, source: Path) -> None:
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
