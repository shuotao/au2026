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
    },
    "obs": {
        "host": "localhost",
        "port": 4455,
        "password": "",
        "scene": "",
        "continue_without_obs": False,
    },
    "browser": {
        "mode": "launch",
        "cdp_url": "http://localhost:9222",
        "fallback_to_open": True,
        "login_url": "https://conferences.autodesk.com/flow/autodesk/au2026/sessioncatalog/page/digital",
        "wait_for_login": True,
        "window_position": [],
        "start_fullscreen": False,
        "user_data_dir": "browser-profile",
        "channel": "chrome",
        "headless": False,
        "window_size": [],
        "settle_seconds": 8,
        "play_selectors": [
            "button:has-text('Watch now')",
            "button:has-text('Join session')",
            "button:has-text('Play')",
            "a:has-text('Watch now')",
            "[aria-label*='Play' i]",
            ".vjs-big-play-button",
            "video",
        ],
        "fullscreen": False,
        "fullscreen_selectors": [
            "[aria-label*='Fullscreen' i]",
            ".vjs-fullscreen-control",
        ],
        "dismiss_selectors": [
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('Got it')",
            "button[aria-label='Close']",
        ],
        "close_page_after": True,
    },
    "paths": {
        "log_file": "logs/au2026rec.log",
        "report_file": "logs/sessions.csv",
    },
}

_PATH_KEYS = {
    ("schedule", "file"),
    ("schedule", "catalog"),
    ("browser", "user_data_dir"),
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


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


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
    size = cfg.get("browser", "window_size")
    if not (isinstance(size, list) and len(size) in (0, 2) and all(isinstance(n, int) for n in size)):
        raise ConfigError(
            "[browser] window_size 要嘛留空 []（不動視窗大小），要嘛是兩個整數，例如 [1920, 1080]"
        )
    position = cfg.get("browser", "window_position")
    if not (isinstance(position, list) and (len(position) == 0 or len(position) == 2)):
        raise ConfigError(
            "[browser] window_position 要嘛留空 []（不動視窗位置），"
            "要嘛是兩個整數座標，例如 [1920, 0]"
        )
    mode = str(cfg.get("browser", "mode")).lower()
    if mode not in {"launch", "attach", "open"}:
        raise ConfigError(f"[browser] mode 只能是 launch / attach / open，讀到 {mode!r}")


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
