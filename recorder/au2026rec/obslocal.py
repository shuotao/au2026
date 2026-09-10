"""讀本機 OBS 自己的設定檔，自動抓出 WebSocket 埠號/密碼與錄影輸出設定。

OBS 把設定放在（Windows）%APPDATA%\\obs-studio：
  plugin_config/obs-websocket/config.json  WebSocket 開關、埠號、密碼
  global.ini                               目前使用的 profile 與場景集合
  basic/profiles/<名稱>/basic.ini           錄影輸出路徑、格式、檔名樣板
  basic/scenes/<名稱>.json                  場景清單

這樣就不用手抄密碼，也能在還沒開 OBS 前先驗設定對不對。
"""
from __future__ import annotations

import configparser
import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


class ObsLocalError(RuntimeError):
    pass


@dataclass
class WebsocketConfig:
    path: Path
    enabled: bool
    port: int
    password: str
    auth_required: bool


@dataclass
class ProfileConfig:
    name: str
    path: Path
    mode: str = ""
    rec_path: str = ""
    rec_format: str = ""
    filename_formatting: str = ""
    encoder: str = ""
    base_resolution: str = ""
    output_resolution: str = ""
    fps: str = ""


@dataclass
class ObsLocal:
    root: Path
    websocket: WebsocketConfig | None = None
    profile: ProfileConfig | None = None
    profiles: list[str] = field(default_factory=list)
    scene_collection: str = ""
    scenes: list[str] = field(default_factory=list)
    executable: Path | None = None


# ── 路徑探索 ────────────────────────────────────────────────────────────

def obs_config_root() -> Path:
    """OBS 設定目錄。找不到會拋 ObsLocalError。"""
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        candidates = [Path(appdata) / "obs-studio"] if appdata else []
    elif system == "Darwin":
        candidates = [Path.home() / "Library" / "Application Support" / "obs-studio"]
    else:
        candidates = [
            Path.home() / ".config" / "obs-studio",
            Path.home() / "snap" / "obs-studio" / "current" / ".config" / "obs-studio",
        ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise ObsLocalError(
        "找不到 OBS 設定目錄。OBS 至少要開啟過一次才會產生設定檔。"
        f"（找過：{', '.join(str(c) for c in candidates) or '無'}）"
    )


def find_obs_executable() -> Path | None:
    if platform.system() == "Windows":
        candidates = [
            Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"),
            Path(r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe"),
        ]
    elif platform.system() == "Darwin":
        candidates = [Path("/Applications/OBS.app/Contents/MacOS/OBS")]
    else:
        candidates = []
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = shutil.which("obs64") or shutil.which("obs")
    return Path(found) if found else None


# ── 讀取 ────────────────────────────────────────────────────────────────

def _read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # OBS 的鍵有大小寫
    if not path.exists():
        return parser
    # OBS 的 ini 檔開頭有 BOM，configparser 會因此找不到第一個區段標頭。
    parser.read_string(path.read_text(encoding="utf-8-sig", errors="replace"))
    return parser


def _clean_path(value: str) -> str:
    return value.replace("\\\\", "\\").strip()


def read_websocket_config(root: Path) -> WebsocketConfig | None:
    path = root / "plugin_config" / "obs-websocket" / "config.json"
    if not path.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ObsLocalError(f"OBS 的 WebSocket 設定檔壞了（{path}）：{exc}") from exc
    return WebsocketConfig(
        path=path,
        enabled=bool(data.get("server_enabled", False)),
        port=int(data.get("server_port", 4455)),
        password=str(data.get("server_password", "")),
        auth_required=bool(data.get("auth_required", True)),
    )


def read_profile(root: Path, name: str | None = None) -> ProfileConfig | None:
    profiles_dir = root / "basic" / "profiles"
    if not profiles_dir.is_dir():
        return None
    target = None
    if name:
        for candidate in profiles_dir.iterdir():
            if candidate.is_dir() and candidate.name == name:
                target = candidate
                break
        if target is None:
            raise ObsLocalError(
                f"OBS 裡沒有 profile {name!r}。現有：{', '.join(list_profiles(root)) or '（無）'}"
            )
    else:
        global_ini = _read_ini(root / "global.ini")
        wanted = global_ini.get("Basic", "ProfileDir", fallback="") or global_ini.get(
            "Basic", "Profile", fallback=""
        )
        for candidate in profiles_dir.iterdir():
            if candidate.is_dir() and candidate.name == wanted:
                target = candidate
                break
        if target is None:
            dirs = [d for d in profiles_dir.iterdir() if d.is_dir()]
            target = dirs[0] if dirs else None
    if target is None:
        return None

    ini = _read_ini(target / "basic.ini")
    mode = ini.get("Output", "Mode", fallback="Simple")
    if mode.lower().startswith("adv"):
        rec_path = ini.get("AdvOut", "RecFilePath", fallback="")
        rec_format = ini.get("AdvOut", "RecFormat2", fallback="") or ini.get(
            "AdvOut", "RecFormat", fallback=""
        )
        encoder = ini.get("AdvOut", "RecEncoder", fallback="")
    else:
        rec_path = ini.get("SimpleOutput", "FilePath", fallback="")
        rec_format = ini.get("SimpleOutput", "RecFormat2", fallback="") or ini.get(
            "SimpleOutput", "RecFormat", fallback=""
        )
        encoder = ini.get("SimpleOutput", "RecEncoder", fallback="")
    return ProfileConfig(
        name=target.name,
        path=target,
        mode=mode,
        rec_path=_clean_path(rec_path),
        rec_format=rec_format,
        filename_formatting=ini.get("Output", "FilenameFormatting", fallback=""),
        encoder=encoder,
        base_resolution=f"{ini.get('Video', 'BaseCX', fallback='?')}x{ini.get('Video', 'BaseCY', fallback='?')}",
        output_resolution=f"{ini.get('Video', 'OutputCX', fallback='?')}x{ini.get('Video', 'OutputCY', fallback='?')}",
        fps=ini.get("Video", "FPSCommon", fallback=""),
    )


def list_profiles(root: Path) -> list[str]:
    profiles_dir = root / "basic" / "profiles"
    if not profiles_dir.is_dir():
        return []
    return sorted(d.name for d in profiles_dir.iterdir() if d.is_dir())


def read_scenes(root: Path) -> tuple[str, list[str]]:
    """回傳 (場景集合名稱, 場景清單)。讀不到就回空的。"""
    global_ini = _read_ini(root / "global.ini")
    collection_file = global_ini.get("Basic", "SceneCollectionFile", fallback="")
    collection_name = global_ini.get("Basic", "SceneCollection", fallback="")
    scenes_dir = root / "basic" / "scenes"
    path = scenes_dir / f"{collection_file}.json" if collection_file else None
    if path is None or not path.exists():
        available = sorted(scenes_dir.glob("*.json")) if scenes_dir.is_dir() else []
        if not available:
            return collection_name, []
        path = available[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return collection_name, []
    names = [
        str(source.get("name", ""))
        for source in data.get("sources", [])
        if isinstance(source, dict) and source.get("id") == "scene"
    ]
    return collection_name or path.stem, [n for n in names if n]


def inspect(profile_name: str | None = None) -> ObsLocal:
    root = obs_config_root()
    collection, scenes = read_scenes(root)
    return ObsLocal(
        root=root,
        websocket=read_websocket_config(root),
        profile=read_profile(root, profile_name),
        profiles=list_profiles(root),
        scene_collection=collection,
        scenes=scenes,
        executable=find_obs_executable(),
    )


# ── 修改 ────────────────────────────────────────────────────────────────

def obs_is_running() -> bool:
    """OBS 在跑的時候不能改它的設定檔，關掉時它會整份覆寫回去。"""
    if platform.system() == "Windows":
        import subprocess

        try:
            output = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "obs64.exe" in output
    import subprocess

    try:
        output = subprocess.run(["pgrep", "-x", "obs"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return output.returncode == 0


def enable_websocket(config: WebsocketConfig) -> tuple[Path, WebsocketConfig]:
    """把 server_enabled 打開，回傳 (備份路徑, 新設定)。OBS 執行中會拒絕。"""
    if obs_is_running():
        raise ObsLocalError(
            "OBS 正在執行中，現在改設定檔會在 OBS 關閉時被覆寫。"
            "請先關掉 OBS 再執行，或直接在 OBS 裡「工具 → WebSocket 伺服器設定」勾選啟用。"
        )
    data: dict[str, Any] = json.loads(config.path.read_text(encoding="utf-8"))
    backup = config.path.with_suffix(f".json.bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(config.path, backup)
    data["server_enabled"] = True
    config.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return backup, WebsocketConfig(
        path=config.path,
        enabled=True,
        port=int(data.get("server_port", 4455)),
        password=str(data.get("server_password", "")),
        auth_required=bool(data.get("auth_required", True)),
    )


def apply_to_config(config_path: Path, *, port: int, password: str) -> list[str]:
    """把埠號與密碼寫進 config.toml 的 [obs] 區段，回傳實際改動的描述。"""
    if not config_path.exists():
        raise ObsLocalError(f"找不到 {config_path}，先執行 au2026rec init")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    changes: list[str] = []
    in_obs = False
    seen = {"port": False, "password": False}

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            if in_obs:
                break
            in_obs = stripped == "[obs]"
            continue
        if not in_obs:
            continue
        if stripped.startswith("port"):
            seen["port"] = True
            if f"= {port}" not in stripped:
                lines[index] = f"port = {port}\n"
                changes.append(f"port → {port}")
        elif stripped.startswith("password"):
            seen["password"] = True
            if password not in stripped:
                lines[index] = f'password = "{password}"\n'
                changes.append("password → 已從 OBS 設定填入")

    missing = [key for key, found in seen.items() if not found]
    if missing:
        raise ObsLocalError(
            f"config.toml 的 [obs] 區段缺少 {', '.join(missing)}，請比對 config.example.toml"
        )
    if changes:
        config_path.write_text("".join(lines), encoding="utf-8")
    return changes
