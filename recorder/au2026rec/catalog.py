"""建立 code → 課程網址 對照表。

AU2026 的 My Schedule CSV 只有 Session Code，沒有網址，所以要從課程目錄補。
來源優先用同專案的 AU2026_挑課工具.html（內嵌 173 筆 session JSON，172 個場次代碼；2026-09-11 依官方數位目錄同步）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from au2026rec.schedule import normalize_code

KEEP_FIELDS = ("code", "title", "url", "mode", "type", "track", "duration", "date", "start", "end")
_EMBEDDED_JSON = re.compile(
    r"<script[^>]*id=[\"']data[\"'][^>]*>\s*(\[.*?\])\s*</script>", re.S | re.I
)
DEFAULT_SOURCES = (
    "../AU2026_挑課工具.html",
    "AU2026_挑課工具.html",
    "../AU2026_Planner.html",
    "../index.html",
)


class CatalogError(RuntimeError):
    pass


def extract_sessions(path: Path) -> list[dict[str, Any]]:
    """從挑課工具 HTML（或純 JSON 檔）取出 session 清單。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        match = _EMBEDDED_JSON.search(text)
        if not match:
            raise CatalogError(
                f"{path.name} 裡找不到內嵌的 session JSON"
                "（預期有 <script id=\"data\" type=\"application/json\">）"
            )
        data = json.loads(match.group(1))
    if isinstance(data, dict):
        data = list(data.values())
    if not isinstance(data, list):
        raise CatalogError(f"{path.name} 的資料不是清單")
    return [row for row in data if isinstance(row, dict)]


def build_catalog(sources: Iterable[Path]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """合併多個來源，回傳 (catalog, notes)。後面的來源不會覆蓋已有的網址。"""
    catalog: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    explicit = list(sources)
    for source in explicit:
        if not source.exists():
            continue
        try:
            rows = extract_sessions(source)
        except (CatalogError, json.JSONDecodeError) as exc:
            # 預設會掃好幾個候選檔，其中不含 session 資料的直接跳過即可。
            notes.append(f"{source.name}：略過（{exc}）")
            continue
        added = 0
        for row in rows:
            code = normalize_code(str(row.get("code", "")))
            if not code:
                continue
            entry = {key: row.get(key, "") for key in KEEP_FIELDS}
            entry["code"] = code
            existing = catalog.get(code)
            if existing and existing.get("url"):
                continue
            catalog[code] = entry
            added += 1
        notes.append(f"{source.name}：{len(rows)} 筆，採用 {added} 筆")
    if not catalog:
        raise CatalogError(
            "沒有任何來源可用。請用 --source 指定 AU2026_挑課工具.html，"
            "或自備一份 code/url 的 JSON。"
        )
    missing_url = [code for code, entry in catalog.items() if not entry.get("url")]
    if missing_url:
        notes.append(f"其中 {len(missing_url)} 筆沒有網址：{', '.join(sorted(missing_url)[:10])}")
    return catalog, notes


def write_catalog(catalog: dict[str, dict[str, Any]], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def resolve_sources(explicit: Iterable[str], root: Path) -> list[Path]:
    paths = [Path(item) if Path(item).is_absolute() else root / item for item in explicit]
    if paths:
        return paths
    return [root / candidate for candidate in DEFAULT_SOURCES]
