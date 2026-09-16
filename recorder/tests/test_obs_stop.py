# -*- coding: utf-8 -*-
"""stop_recording 一律送出 StopRecord。

會踩到的情境：剛下 StartRecord、OBS 還沒把 output_active 翻成 True 時就要求停止
（例如試錄秒數比頁面載入時間短）。舊寫法會直接 return，OBS 就一直錄下去，
檔案也不會收尾。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from au2026rec.obs import ObsController, ObsError, ObsSettings  # noqa: E402


class FakeResponse:
    def __init__(self, output_path: str | None = None) -> None:
        self.output_path = output_path


class FakeClient:
    """最小 obs-websocket 替身：可控制 output_active 與 stop_record 行為。"""

    def __init__(self, *, active: bool, stop_raises: Exception | None = None) -> None:
        self.active = active
        self.stop_raises = stop_raises
        self.stop_calls = 0

    def get_record_status(self) -> FakeResponse:
        r = FakeResponse()
        r.output_active = self.active
        return r

    def stop_record(self) -> FakeResponse:
        self.stop_calls += 1
        if self.stop_raises is not None:
            raise self.stop_raises
        self.active = False
        return FakeResponse(r"C:/Videos/AU2026/session.mp4")


def controller(client: FakeClient) -> ObsController:
    c = ObsController(ObsSettings())
    c._client = client
    return c


class TestStopRecording(unittest.TestCase):
    def test_stops_when_obs_reports_recording(self) -> None:
        client = FakeClient(active=True)
        path = controller(client).stop_recording()
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(path, "C:/Videos/AU2026/session.mp4")

    def test_still_sends_stop_when_status_says_idle(self) -> None:
        """狀態空窗：查詢說沒在錄，其實錄影已經開始 —— 一定要送 StopRecord。"""
        client = FakeClient(active=False)
        path = controller(client).stop_recording()
        self.assertEqual(client.stop_calls, 1, "狀態回報沒在錄時仍必須送出停止指令")
        self.assertEqual(path, "C:/Videos/AU2026/session.mp4")
        self.assertFalse(client.active)

    def test_idle_and_obs_rejects_stop_is_only_a_warning(self) -> None:
        """真的沒在錄：OBS 會回錯誤，這時不該讓整場錄影流程炸掉。"""
        client = FakeClient(active=False, stop_raises=RuntimeError("not recording"))
        self.assertIsNone(controller(client).stop_recording())
        self.assertEqual(client.stop_calls, 1)

    def test_error_while_recording_propagates(self) -> None:
        """確實在錄卻停不下來是真的故障，要讓上層知道。"""
        client = FakeClient(active=True, stop_raises=RuntimeError("websocket closed"))
        with self.assertRaises(ObsError):
            controller(client).stop_recording()


if __name__ == "__main__":
    unittest.main()
