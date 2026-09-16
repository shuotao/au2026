# -*- coding: utf-8 -*-
"""[browser] extra_args：使用者自訂的瀏覽器旗標會原樣帶到命令列。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from au2026rec import browserlaunch  # noqa: E402
from au2026rec.config import ConfigError, load_config  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def write_config(replace: tuple[str, str] | None = None) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    if replace:
        old, new = replace
        assert old in text, f"範本裡找不到 {old!r}"
        text = text.replace(old, new)
    target = Path(tempfile.mkdtemp()) / "config.toml"
    target.write_text(text, encoding="utf-8")
    return target


class RecordingPopen:
    """攔下 Popen，記住實際組出來的命令列。"""

    def __init__(self) -> None:
        self.command: list[str] = []

    def __call__(self, command, **_kwargs):  # noqa: ANN001, ANN204
        self.command = list(command)
        return object()


class TestExtraArgsConfig(unittest.TestCase):
    def test_default_is_empty(self) -> None:
        cfg = load_config(write_config())
        self.assertEqual(cfg.get("browser", "extra_args"), [])

    def test_accepts_string_list(self) -> None:
        cfg = load_config(
            write_config(("extra_args = []", 'extra_args = ["--foo", "--bar=1"]'))
        )
        self.assertEqual(cfg.get("browser", "extra_args"), ["--foo", "--bar=1"])

    def test_rejects_non_list(self) -> None:
        path = write_config(("extra_args = []", 'extra_args = "--foo"'))
        with self.assertRaises(ConfigError) as caught:
            load_config(path)
        self.assertIn("extra_args", str(caught.exception))

    def test_rejects_non_string_items(self) -> None:
        path = write_config(("extra_args = []", "extra_args = [1, 2]"))
        with self.assertRaises(ConfigError):
            load_config(path)


class TestLaunchPassesExtraArgs(unittest.TestCase):
    def setUp(self) -> None:
        self.popen = RecordingPopen()
        self._real_popen = browserlaunch.subprocess.Popen
        self._real_port = browserlaunch.port_is_open
        browserlaunch.subprocess.Popen = self.popen  # type: ignore[assignment]
        calls = {"n": 0}

        def fake_port_is_open(port: int = 0, host: str = "localhost") -> bool:
            # 第一次問是「埠有沒有被佔用」（要 False），之後是「開起來了沒」（要 True）
            calls["n"] += 1
            return calls["n"] > 1

        browserlaunch.port_is_open = fake_port_is_open  # type: ignore[assignment]

    def tearDown(self) -> None:
        browserlaunch.subprocess.Popen = self._real_popen  # type: ignore[assignment]
        browserlaunch.port_is_open = self._real_port  # type: ignore[assignment]

    def launch(self, **kwargs) -> list[str]:  # noqa: ANN003
        choice = browserlaunch.BrowserChoice("chrome", "Google Chrome", Path("chrome.exe"))
        browserlaunch.launch(
            choice, profile_dir=Path(tempfile.mkdtemp()), port=9999, **kwargs
        )
        return self.popen.command

    def test_extra_args_land_on_command_line(self) -> None:
        command = self.launch(extra_args=["--host-resolver-rules=MAP a.example [::1]"])
        self.assertIn("--host-resolver-rules=MAP a.example [::1]", command)

    def test_extra_args_come_before_the_url(self) -> None:
        command = self.launch(extra_args=["--flag"], url="https://example.com/")
        self.assertLess(command.index("--flag"), command.index("https://example.com/"))

    def test_blank_entries_are_dropped(self) -> None:
        command = self.launch(extra_args=["", "   ", "--real"])
        self.assertEqual([a for a in command if a.startswith("--real")], ["--real"])
        self.assertNotIn("", command[1:])

    def test_no_extra_args_keeps_command_minimal(self) -> None:
        """沒設定時命令列要跟以前一模一樣 —— 這支程式刻意不亂加旗標。"""
        command = self.launch()
        self.assertEqual(
            command[1:],
            [
                "--remote-debugging-port=9999",
                f"--user-data-dir={command[2].split('=', 1)[1]}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )


if __name__ == "__main__":
    unittest.main()
