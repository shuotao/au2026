# -*- coding: utf-8 -*-
"""Zoom webinar 場次的偵測。

實際遇到的情況：AU2026 有一部分 Digital 場次不是內嵌播放器，而是 Zoom webinar。
那種頁面打得開、也不是 No session to display，只是沒有 <video> 可以播；
不先講一聲的話，會排到整段時間、錄出一支只有網頁的影片，隔天才發現。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from au2026rec.browser import AttachNavigator, BrowserSettings  # noqa: E402


class FakeLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class FakePage:
    """只回答 locator(...).count() 的假頁面。matches 是「選擇器 → 命中幾個」。"""

    def __init__(self, matches: dict[str, int]) -> None:
        self.matches = matches

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.matches.get(selector, 0))


def navigator(matches: dict[str, int]) -> AttachNavigator:
    nav = AttachNavigator(BrowserSettings())
    nav._page = FakePage(matches)
    return nav


class TestWebinarDetection(unittest.TestCase):
    def test_join_button_without_player_is_webinar(self) -> None:
        nav = navigator({".webinar-join-btn": 1})
        self.assertTrue(nav.session_is_webinar())

    def test_page_with_player_is_not_webinar(self) -> None:
        """播放器旁邊也放 Zoom 連結的場次照樣錄得到，不能誤判。"""
        nav = navigator({".video-js, video": 1, ".webinar-join-btn": 1})
        self.assertFalse(nav.session_is_webinar())

    def test_plain_session_page_is_not_webinar(self) -> None:
        nav = navigator({".video-js, video": 1})
        self.assertFalse(nav.session_is_webinar())

    def test_page_with_neither_is_not_webinar(self) -> None:
        """還沒載完的頁面什麼都找不到 —— 不要因此報 webinar。"""
        self.assertFalse(navigator({}).session_is_webinar())

    def test_detection_failure_is_not_fatal(self) -> None:
        class Exploding:
            def locator(self, selector: str):  # noqa: ANN001, ANN202
                raise RuntimeError("page closed")

        nav = AttachNavigator(BrowserSettings())
        nav._page = Exploding()
        self.assertFalse(nav.session_is_webinar())


if __name__ == "__main__":
    unittest.main()
