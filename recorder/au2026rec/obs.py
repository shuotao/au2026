"""透過 obs-websocket 控制 OBS 錄影。

需要在 OBS 裡開啟：工具 → WebSocket 伺服器設定 → 啟用 WebSocket 伺服器，
記下埠號（預設 4455）與密碼，填進 config.toml 的 [obs] 區段。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


class ObsError(RuntimeError):
    pass


@dataclass
class ObsSettings:
    host: str = "localhost"
    port: int = 4455
    password: str = ""
    scene: str = ""
    timeout: int = 5


class ObsController:
    """OBS 錄影控制。所有方法在未連線時都會拋 ObsError。"""

    def __init__(self, settings: ObsSettings) -> None:
        self.settings = settings
        self._client: Any = None
        # 指定錄影檔名靠的是 OBS profile 裡的 FilenameFormatting，那是持久設定，
        # 不還原的話跑完會留在使用者的 OBS 裡，影響之後手動錄影的命名。
        self._original_filename_format: str | None = None

    # ── 連線 ────────────────────────────────────────────────────────────
    def connect(self) -> str:
        """連上 OBS，回傳版本描述字串。"""
        try:
            import obsws_python as obsws
        except ImportError as exc:  # pragma: no cover - 安裝問題
            raise ObsError("缺少 obsws-python，請執行 pip install obsws-python") from exc

        # obsws-python 連不上時會自己 log 一整串 traceback，這裡先壓住，
        # 由我們回報一句看得懂的錯誤訊息。
        noisy = logging.getLogger("obsws_python")
        previous_level = noisy.level
        noisy.setLevel(logging.CRITICAL)
        try:
            self._client = obsws.ReqClient(
                host=self.settings.host,
                port=self.settings.port,
                password=self.settings.password or "",
                timeout=self.settings.timeout,
            )
            version = self._client.get_version()
        except Exception as exc:
            detail = str(exc).rstrip("。.")
            raise ObsError(
                f"連不上 OBS（{self.settings.host}:{self.settings.port}）：{detail}。"
                "請確認 OBS 已開啟，且工具 → WebSocket 伺服器設定裡已啟用、埠號與密碼相符。"
            ) from exc
        finally:
            noisy.setLevel(previous_level)
        return f"OBS {getattr(version, 'obs_version', '?')} / websocket {getattr(version, 'obs_web_socket_version', '?')}"

    def close(self) -> None:
        self.restore_filename_format()
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:  # pragma: no cover - 關閉失敗無所謂
            log.debug("關閉 OBS 連線時出錯", exc_info=True)

    @property
    def connected(self) -> bool:
        return self._client is not None

    def _require(self) -> Any:
        if self._client is None:
            raise ObsError("尚未連上 OBS")
        return self._client

    # ── 場景 ────────────────────────────────────────────────────────────
    def scene_names(self) -> list[str]:
        response = self._require().get_scene_list()
        scenes = getattr(response, "scenes", []) or []
        return [str(s.get("sceneName", "")) for s in scenes if isinstance(s, dict)]

    def switch_scene(self, name: str) -> None:
        if not name:
            return
        available = self.scene_names()
        if available and name not in available:
            raise ObsError(f"OBS 裡沒有場景 {name!r}。現有場景：{', '.join(available) or '（無）'}")
        self._require().set_current_program_scene(name)
        log.info("OBS 場景切到 %s", name)

    # ── 錄影 ────────────────────────────────────────────────────────────
    def is_recording(self) -> bool:
        status = self._require().get_record_status()
        return bool(getattr(status, "output_active", False))

    def filename_format(self) -> str | None:
        """讀 OBS 目前的檔名樣板。"""
        try:
            response = self._require().get_profile_parameter("Output", "FilenameFormatting")
        except Exception as exc:
            log.debug("讀不到 OBS 檔名樣板：%s", exc)
            return None
        value = getattr(response, "parameter_value", None)
        if value is None:
            value = getattr(response, "default_parameter_value", None)
        return str(value) if value is not None else None

    def set_filename(self, name: str) -> bool:
        """設定下一段錄影的檔名。第一次呼叫會記住原本的樣板，close() 時還原。"""
        if self._original_filename_format is None:
            self._original_filename_format = self.filename_format() or ""
            log.debug("記住 OBS 原本的檔名樣板：%r", self._original_filename_format)
        try:
            self._require().set_profile_parameter("Output", "FilenameFormatting", name)
        except Exception as exc:
            log.warning("設定 OBS 檔名失敗，將沿用 OBS 預設命名：%s", exc)
            return False
        return True

    def restore_filename_format(self) -> None:
        """把 FilenameFormatting 還原成我們動手前的樣子。"""
        original, self._original_filename_format = self._original_filename_format, None
        if original is None or self._client is None:
            return
        try:
            self._client.set_profile_parameter("Output", "FilenameFormatting", original)
            log.info("已還原 OBS 檔名樣板：%s", original)
        except Exception as exc:
            log.warning(
                "還原 OBS 檔名樣板失敗，請自行到 OBS 設定 → 進階 → 檔名格式改回 %r：%s",
                original,
                exc,
            )

    def start_recording(self, filename: str | None = None) -> None:
        client = self._require()
        if self.is_recording():
            raise ObsError("OBS 已經在錄影了，先手動停止再重跑，避免蓋掉現有檔案")
        if filename:
            self.set_filename(filename)
        client.start_record()
        log.info("OBS 開始錄影（檔名 %s）", filename or "OBS 預設")

    def stop_recording(self) -> str | None:
        """停止錄影，回傳輸出檔案路徑（OBS 版本較舊時可能為 None）。"""
        client = self._require()
        if not self.is_recording():
            log.warning("要求停止錄影，但 OBS 目前並未在錄影")
            return None
        response = client.stop_record()
        path = getattr(response, "output_path", None)
        log.info("OBS 停止錄影，輸出：%s", path or "（OBS 未回報路徑）")
        return str(path) if path else None


class NullObsController(ObsController):
    """continue_without_obs = true 時的替身：只記 log，不真的錄。"""

    def __init__(self) -> None:
        super().__init__(ObsSettings())

    def connect(self) -> str:
        return "未連線（continue_without_obs = true，只開課不錄影）"

    @property
    def connected(self) -> bool:
        return False

    def scene_names(self) -> list[str]:
        return []

    def switch_scene(self, name: str) -> None:
        log.info("[無 OBS] 略過切換場景 %s", name)

    def is_recording(self) -> bool:
        return False

    def filename_format(self) -> str | None:
        return None

    def set_filename(self, name: str) -> bool:
        return False

    def restore_filename_format(self) -> None:
        return None

    def start_recording(self, filename: str | None = None) -> None:
        log.warning("[無 OBS] 略過開始錄影（原本檔名 %s）", filename)

    def stop_recording(self) -> str | None:
        log.warning("[無 OBS] 略過停止錄影")
        return None
