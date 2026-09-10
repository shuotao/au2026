"""設定驗證、設定寫回、導頁模式選擇的測試。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from au2026rec.browser import (  # noqa: E402
    MODE_OPEN,
    BrowserSettings,
    BrowserError,
    OsOpenNavigator,
    make_navigator,
)
from au2026rec.config import ConfigError, load_config, set_value  # noqa: E402

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def write_config(extra: str = "", *, replace: tuple[str, str] | None = None) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    if replace:
        old, new = replace
        assert old in text, f"範本裡找不到 {old!r}"
        text = text.replace(old, new)
    directory = Path(tempfile.mkdtemp())
    target = directory / "config.toml"
    target.write_text(text + extra, encoding="utf-8")
    return target


class TestConfigValidation(unittest.TestCase):
    def test_example_config_is_valid(self) -> None:
        cfg = load_config(write_config())
        self.assertEqual(cfg.get("browser", "mode"), "launch")
        self.assertEqual(cfg.get("schedule", "overlap_policy"), "shift")

    def test_paths_resolve_against_config_dir(self) -> None:
        path = write_config()
        cfg = load_config(path)
        self.assertEqual(cfg.resolve("schedule", "file").parent, path.parent)

    def test_rejects_unknown_browser_mode(self) -> None:
        path = write_config(replace=('mode = "launch"', 'mode = "telepathy"'))
        with self.assertRaises(ConfigError) as caught:
            load_config(path)
        self.assertIn("launch / attach / open", str(caught.exception))

    def test_rejects_bad_window_position(self) -> None:
        path = write_config(replace=("window_position = []", "window_position = [1920]"))
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_accepts_window_position_pair(self) -> None:
        path = write_config(replace=("window_position = []", "window_position = [1920, 0]"))
        self.assertEqual(load_config(path).get("browser", "window_position"), [1920, 0])

    def test_rejects_bad_overlap_policy(self) -> None:
        path = write_config(replace=('overlap_policy = "shift"', 'overlap_policy = "yolo"'))
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_rejects_negative_lead(self) -> None:
        path = write_config(replace=("lead_seconds = 90", "lead_seconds = -5"))
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_rejects_unknown_section(self) -> None:
        path = write_config(extra="\n[nonsense]\nfoo = 1\n")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_missing_config_explains_init(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config(Path(tempfile.mkdtemp()) / "config.toml")
        self.assertIn("init", str(caught.exception))


class TestSetValue(unittest.TestCase):
    def test_writes_into_right_section(self) -> None:
        path = write_config()
        self.assertTrue(set_value(path, "obs", "scene", '"AU2026 錄課"'))
        self.assertEqual(load_config(path).get("obs", "scene"), "AU2026 錄課")

    def test_second_write_is_noop(self) -> None:
        path = write_config()
        set_value(path, "obs", "scene", '"AU2026 錄課"')
        self.assertFalse(set_value(path, "obs", "scene", '"AU2026 錄課"'))

    def test_keeps_comments(self) -> None:
        path = write_config()
        set_value(path, "obs", "port", "4456")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 錄影前要切到的場景", text.replace("# 錄影前切到這個場景", "# 錄影前要切到的場景"))
        self.assertIn("port = 4456", text)

    def test_unknown_key_raises(self) -> None:
        path = write_config()
        with self.assertRaises(ConfigError):
            set_value(path, "obs", "no_such_key", "1")

    def test_does_not_touch_same_key_in_other_section(self) -> None:
        path = write_config()
        set_value(path, "obs", "host", '"192.168.1.5"')
        cfg = load_config(path)
        self.assertEqual(cfg.get("obs", "host"), "192.168.1.5")
        # [schedule] 的 file 不該被動到
        self.assertEqual(cfg.get("schedule", "file"), "my_schedule.csv")


class TestNavigatorFactory(unittest.TestCase):
    def test_open_mode_needs_no_playwright(self) -> None:
        navigator = make_navigator(BrowserSettings(mode=MODE_OPEN))
        self.assertIsInstance(navigator, OsOpenNavigator)
        navigator.close()

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(BrowserError):
            make_navigator(BrowserSettings(mode="nope"))

    def test_attach_failure_falls_back_to_open(self) -> None:
        # 沒有東西在聽 9999 埠，attach 一定失敗 → 應該退回 open 而不是炸掉
        navigator = make_navigator(
            BrowserSettings(
                mode="attach", cdp_url="http://localhost:9999", fallback_to_open=True
            )
        )
        self.assertIsInstance(navigator, OsOpenNavigator)
        navigator.close()

    def test_attach_failure_raises_when_no_fallback(self) -> None:
        with self.assertRaises(BrowserError):
            make_navigator(
                BrowserSettings(
                    mode="attach", cdp_url="http://localhost:9999", fallback_to_open=False
                )
            )


if __name__ == "__main__":
    unittest.main()
