"""用「一般方式」啟動瀏覽器並開著除錯埠，給 attach 模式接管。

為什麼需要這個：Playwright 自己啟動的瀏覽器帶有自動化特徵
（navigator.webdriver = true、CDP 由它建立的工作階段），有些登入頁會因此
拒絕登入 —— Autodesk SSO、以及任何走 Google 帳號的登入特別容易中。

改成這樣就沒事：
    1. 這裡用一般參數啟動瀏覽器，只多加 --remote-debugging-port
    2. 你在裡面像平常一樣登入（瀏覽器不知道有人要接管它）
    3. 程式事後透過除錯埠接上去導頁

Chrome 136 之後不接受對「預設 profile」開除錯埠，所以一定要另外指定
--user-data-dir。這裡預設開一個專用的 profile 目錄，登入狀態會留在裡面。
"""
from __future__ import annotations

import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 9222


class LaunchError(RuntimeError):
    pass


@dataclass
class BrowserChoice:
    key: str
    name: str
    path: Path


_WINDOWS_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("chrome", "Google Chrome", r"{pf}\Google\Chrome\Application\chrome.exe"),
    ("chrome", "Google Chrome", r"{pf86}\Google\Chrome\Application\chrome.exe"),
    ("edge", "Microsoft Edge", r"{pf86}\Microsoft\Edge\Application\msedge.exe"),
    ("edge", "Microsoft Edge", r"{pf}\Microsoft\Edge\Application\msedge.exe"),
    ("brave", "Brave", r"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ("brave", "Brave", r"{pf86}\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ("brave", "Brave", r"{local}\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ("chrome", "Google Chrome", r"{local}\Google\Chrome\Application\chrome.exe"),
)


def find_browsers() -> list[BrowserChoice]:
    """找出這台電腦裝了哪些 Chromium 系瀏覽器，依序去重。"""
    found: list[BrowserChoice] = []
    seen: set[Path] = set()

    if platform.system() == "Windows":
        import os

        slots = {
            "pf": os.environ.get("ProgramFiles", r"C:\Program Files"),
            "pf86": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "local": os.environ.get("LOCALAPPDATA", ""),
        }
        for key, name, template in _WINDOWS_CANDIDATES:
            if "{local}" in template and not slots["local"]:
                continue
            path = Path(template.format(**slots))
            if path.exists() and path not in seen:
                seen.add(path)
                found.append(BrowserChoice(key, name, path))
    else:
        for key, name, command in (
            ("chrome", "Google Chrome", "google-chrome"),
            ("edge", "Microsoft Edge", "microsoft-edge"),
            ("brave", "Brave", "brave-browser"),
            ("chromium", "Chromium", "chromium"),
        ):
            which = shutil.which(command)
            if which:
                path = Path(which)
                if path not in seen:
                    seen.add(path)
                    found.append(BrowserChoice(key, name, path))
    return found


def port_is_open(port: int = DEFAULT_PORT, host: str = "localhost") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def launch(
    browser: BrowserChoice,
    *,
    profile_dir: Path,
    port: int = DEFAULT_PORT,
    url: str = "",
    wait_seconds: int = 30,
) -> None:
    """啟動瀏覽器並等除錯埠打開。除了除錯埠與 profile 目錄外不加任何參數。"""
    if port_is_open(port):
        raise LaunchError(
            f"埠號 {port} 已經有東西在聽了。可能是你已經開過一個帶除錯埠的瀏覽器 —— "
            "直接用那一個就好；如果不是，換一個埠號或把佔用的程式關掉。"
        )
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser.path),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if url:
        command.append(url)
    try:
        subprocess.Popen(command, close_fds=True)
    except OSError as exc:
        raise LaunchError(f"啟動 {browser.name} 失敗：{exc}") from exc

    for _ in range(wait_seconds * 2):
        if port_is_open(port):
            return
        time.sleep(0.5)
    raise LaunchError(
        f"{browser.name} 開起來了，但 {wait_seconds} 秒內沒看到除錯埠 {port}。"
        "若該瀏覽器原本就在執行，新視窗會併進舊的程序而不會開除錯埠 —— "
        "請把該瀏覽器完全關閉（含背景常駐）後再試一次。"
    )
