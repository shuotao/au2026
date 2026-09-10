"""用 obs-websocket 建立一個專屬的錄課場景：螢幕擷取 + 桌面音訊。

不動你原本的場景，只新增一個（預設叫「AU2026 錄課」）。不想要了在 OBS 裡
直接刪掉那個場景即可。

為什麼要另建一個：原本的場景常綁著特定視窗的應用程式音訊擷取，換頁或換視窗
就會沒聲音；無人值守錄一整晚時，桌面音訊 + 螢幕擷取是最不會出事的組合。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

SCREEN_SOURCE = "AU2026 畫面"
AUDIO_SOURCE = "AU2026 桌面音訊"
MONITOR_KIND = "monitor_capture"
AUDIO_KIND = "wasapi_output_capture"
_PROBE_SCENE = "au2026rec_probe_暫存"


class SceneError(RuntimeError):
    pass


@dataclass
class Monitor:
    index: int
    name: str
    value: str

    def __str__(self) -> str:
        return f"[{self.index}] {self.name}"


# ── 查詢 ────────────────────────────────────────────────────────────────

def _scene_names(client: Any) -> list[str]:
    scenes = getattr(client.get_scene_list(), "scenes", []) or []
    return [str(s.get("sceneName", "")) for s in scenes if isinstance(s, dict)]


def _input_names(client: Any) -> list[str]:
    try:
        response = client.get_input_list(None)
    except TypeError:
        response = client.get_input_list()
    inputs = getattr(response, "inputs", []) or []
    return [str(i.get("inputName", "")) for i in inputs if isinstance(i, dict)]


def _scene_item_id(client: Any, scene: str, source: str) -> int | None:
    items = getattr(client.get_scene_item_list(scene), "scene_items", []) or []
    for item in items:
        if isinstance(item, dict) and item.get("sourceName") == source:
            return int(item.get("sceneItemId"))
    return None


def list_monitors(client: Any) -> list[Monitor]:
    """列出 OBS 認得的螢幕。清單與 OBS 介面上的「顯示器」下拉選單一致。"""
    existing = _input_names(client)
    probe_source = next(
        (name for name in (SCREEN_SOURCE, *existing) if name in existing and _is_monitor(client, name)),
        None,
    )
    temporary = False
    if probe_source is None:
        # 沒有現成的螢幕擷取來源可問，就開一個暫存的問完再刪。
        probe_source = "au2026rec_probe_螢幕"
        _ensure_scene(client, _PROBE_SCENE)
        client.create_input(_PROBE_SCENE, probe_source, MONITOR_KIND, {}, False)
        temporary = True
    try:
        items = getattr(
            client.get_input_properties_list_property_items(probe_source, "monitor_id"),
            "property_items",
            [],
        )
    except Exception as exc:
        raise SceneError(f"讀不到螢幕清單：{exc}") from exc
    finally:
        if temporary:
            try:
                client.remove_input(probe_source)
                client.remove_scene(_PROBE_SCENE)
            except Exception:
                log.debug("清理暫存探測來源失敗", exc_info=True)
    return [
        Monitor(index=number, name=str(item.get("itemName", "?")), value=str(item.get("itemValue", "")))
        for number, item in enumerate(items, start=1)
        if isinstance(item, dict)
    ]


def _is_monitor(client: Any, name: str) -> bool:
    try:
        kind = getattr(client.get_input_settings(name), "input_kind", "")
    except Exception:
        return False
    return str(kind) == MONITOR_KIND


# ── 建立 ────────────────────────────────────────────────────────────────

def _ensure_scene(client: Any, scene: str) -> bool:
    if scene in _scene_names(client):
        return False
    client.create_scene(scene)
    log.info("建立場景 %s", scene)
    return True


def _ensure_input(
    client: Any, scene: str, name: str, kind: str, settings: dict[str, Any]
) -> tuple[bool, str]:
    """確保來源存在且在這個場景裡。回傳 (是否新建, 說明)。"""
    if name in _input_names(client):
        if settings:
            client.set_input_settings(name, settings, True)
        if _scene_item_id(client, scene, name) is None:
            client.create_scene_item(scene, name, True)
            return False, f"{name}：沿用既有來源，加進場景"
        return False, f"{name}：已存在，設定已更新"
    client.create_input(scene, name, kind, settings or {}, True)
    return True, f"{name}：已建立"


def _fit_to_canvas(client: Any, scene: str, source: str) -> str:
    """把螢幕擷取縮放到剛好填滿畫布，避免留黑邊或超出。"""
    video = client.get_video_settings()
    width = int(getattr(video, "base_width", 1920))
    height = int(getattr(video, "base_height", 1080))
    item_id = _scene_item_id(client, scene, source)
    if item_id is None:
        return "找不到場景項目，略過縮放"
    try:
        client.set_scene_item_transform(
            scene,
            item_id,
            {
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsAlignment": 0,
                "boundsWidth": float(width),
                "boundsHeight": float(height),
                "positionX": 0.0,
                "positionY": 0.0,
            },
        )
    except Exception as exc:
        return f"縮放設定失敗（{exc}），請在 OBS 裡自己拉滿畫布"
    return f"已縮放填滿畫布 {width}x{height}"


def ensure_recording_scene(
    client: Any,
    *,
    scene: str,
    monitor: Monitor | None,
    desktop_audio: bool = True,
    switch_to: bool = False,
) -> list[str]:
    """建立/修好錄課場景，回傳做過哪些事。"""
    notes: list[str] = []
    if _ensure_scene(client, scene):
        notes.append(f"場景 {scene}：已建立")
    else:
        notes.append(f"場景 {scene}：已存在")

    settings: dict[str, Any] = {}
    if monitor is not None:
        settings = {"monitor_id": monitor.value, "capture_cursor": True}
    _, note = _ensure_input(client, scene, SCREEN_SOURCE, MONITOR_KIND, settings)
    notes.append(note)
    if monitor is not None:
        notes.append(f"{SCREEN_SOURCE}：擷取 {monitor.name}")
    notes.append(f"{SCREEN_SOURCE}：{_fit_to_canvas(client, scene, SCREEN_SOURCE)}")

    if desktop_audio:
        # device_id "default" = 系統預設輸出裝置，換耳機/喇叭都不用改設定。
        _, note = _ensure_input(
            client, scene, AUDIO_SOURCE, AUDIO_KIND, {"device_id": "default"}
        )
        notes.append(note)

    if switch_to:
        client.set_current_program_scene(scene)
        notes.append(f"已切到場景 {scene}")
    return notes


def audio_sources_in_scene(client: Any, scene: str) -> list[dict[str, Any]]:
    """列出場景裡的音訊來源，用來檢查會不會沒聲音或雙重收音。"""
    items = getattr(client.get_scene_item_list(scene), "scene_items", []) or []
    found: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("inputKind") or "")
        if "audio" not in kind and "wasapi" not in kind:
            continue
        name = str(item.get("sourceName", ""))
        entry: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "enabled": bool(item.get("sceneItemEnabled", True)),
            "settings": {},
        }
        try:
            entry["settings"] = getattr(client.get_input_settings(name), "input_settings", {}) or {}
        except Exception:
            log.debug("讀不到 %s 的設定", name, exc_info=True)
        try:
            muted = getattr(client.get_input_mute(name), "input_muted", None)
            entry["muted"] = muted
        except Exception:
            entry["muted"] = None
        found.append(entry)
    return found
