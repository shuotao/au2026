"""把瀏覽器導到課程網頁。

attach（預設）
    接管一個用一般方式開、你自己登入好的瀏覽器。開瀏覽器的事交給
    browserlaunch 做：它只多加 --remote-debugging-port 與一個專用 profile
    目錄，其餘一律不加，所以登入頁看到的就是普通瀏覽器，不會被自動化偵測擋下。
    程式事後才從除錯埠接上去導頁 —— 導的是同一個分頁，視窗位置、大小、
    在哪個螢幕都不會變，OBS 錄到的永遠是對的畫面。

open
    只呼叫系統「開這個網址」，零自動化。attach 接不上時的退路
    （fallback_to_open），也可以自己設成主要模式。

程式不碰視窗大小與位置：你把瀏覽器擺成什麼樣，錄到的就是什麼樣。
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger(__name__)

MODE_ATTACH = "attach"
MODE_OPEN = "open"
MODES = (MODE_ATTACH, MODE_OPEN)


class BrowserError(RuntimeError):
    pass


@dataclass
class BrowserSettings:
    mode: str = MODE_ATTACH
    cdp_url: str = "http://localhost:9222"
    fallback_to_open: bool = True
    settle_seconds: int = 8
    play_selectors: Sequence[str] = field(default_factory=list)
    dismiss_selectors: Sequence[str] = field(default_factory=list)
    close_page_after: bool = True


class Navigator:
    """導頁介面。runner 只認這四個方法。"""

    label = "navigator"

    def start(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def open_session(self, url: str) -> dict[str, Any]:
        raise NotImplementedError

    def leave_session(self) -> None:
        return None

    def __enter__(self) -> "Navigator":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ── 系統預設瀏覽器 ──────────────────────────────────────────────────────

class OsOpenNavigator(Navigator):
    """把網址交給系統預設瀏覽器。沒有任何自動化能力，但幾乎不會壞。"""

    label = "系統預設瀏覽器（開新分頁）"

    def start(self) -> None:
        log.info("導頁方式：%s", self.label)

    def close(self) -> None:
        return None

    def open_session(self, url: str) -> dict[str, Any]:
        log.info("交給系統開啟 %s", url)
        opened = False
        if platform.system() == "Windows":
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                opened = True
            except OSError as exc:
                log.warning("os.startfile 失敗，改用 webbrowser：%s", exc)
        elif platform.system() == "Darwin":
            opened = subprocess.run(["open", url], check=False).returncode == 0
        if not opened:
            opened = webbrowser.open(url, new=2)
        if not opened:
            raise BrowserError(f"系統開不起這個網址：{url}")
        return {
            "url": url,
            "played": False,
            "navigator": self.label,
            "note": "由系統瀏覽器開啟，未嘗試按播放鍵",
        }


# ── attach：接管已開好的瀏覽器 ──────────────────────────────────────────

class AttachNavigator(Navigator):
    label = "接管已開啟的瀏覽器（CDP）"

    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    # ── 生命週期 ────────────────────────────────────────────────────────
    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 安裝問題
            raise BrowserError("缺少 playwright，請執行 pip install playwright") from exc

        self._playwright = sync_playwright().start()
        url = self.settings.cdp_url
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(url, timeout=10_000)
        except Exception as exc:
            self.close()
            raise BrowserError(
                f"接不上瀏覽器的除錯埠（{url}）：{exc}\n"
                "請先用選單的「開瀏覽器登入 AU2026」（或指令 au2026rec browser --set-mode）"
                "把瀏覽器開起來並登入，而且那個視窗要一直開著。"
            ) from exc

        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[0] if pages else self._context.new_page()
        log.info(
            "已接管瀏覽器（%s），使用分頁：%s",
            url,
            (self._page.title() or self._page.url or "?")[:60],
        )

    def close(self) -> None:
        # 這個瀏覽器是使用者自己開的，只斷開連線，絕對不要關掉它。
        self._browser = self._context = self._page = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # pragma: no cover - 關閉失敗無所謂
            log.debug("停止 Playwright 時出錯", exc_info=True)
        self._playwright = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise BrowserError("瀏覽器尚未就緒")
        return self._page

    # ── 頁面操作 ────────────────────────────────────────────────────────
    def goto(self, url: str, *, timeout_ms: int = 60_000) -> Any:
        page = self.page
        log.info("導到 %s", url)
        try:
            page.bring_to_front()
        except Exception:
            log.debug("bring_to_front 失敗", exc_info=True)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            log.debug("networkidle 等不到，繼續往下走")
        return page

    def _click_first(
        self, selectors: Sequence[str], *, what: str, timeout_ms: int = 2_500
    ) -> str | None:
        page = self.page
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=timeout_ms)
                locator.click(timeout=timeout_ms)
            except Exception:
                continue
            log.info("%s：點到 %s", what, selector)
            return selector
        log.info("%s：候選選擇器都沒命中（不影響錄影，只是不會自動播）", what)
        return None

    def dismiss_popups(self) -> None:
        page = self.page
        for selector in self.settings.dismiss_selectors:
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=1_000):
                    locator.click(timeout=1_500)
                    log.info("關掉彈窗：%s", selector)
            except Exception:
                continue

    def open_session(self, url: str) -> dict[str, Any]:
        result: dict[str, Any] = {"url": url, "played": False, "navigator": self.label}
        self.goto(url)
        self.dismiss_popups()
        if self.settings.settle_seconds:
            time.sleep(self.settings.settle_seconds)
        self.dismiss_popups()

        hit = self._click_first(self.settings.play_selectors, what="播放")
        result["play_selector"] = hit
        result["played"] = hit is not None
        return result

    def leave_session(self) -> None:
        if not self.settings.close_page_after:
            return
        try:  # 導回空白頁，確保影片與聲音停下來
            self.page.goto("about:blank", wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            log.debug("導回空白頁失敗", exc_info=True)

    # ── 除錯輔助 ────────────────────────────────────────────────────────
    def probe(self, url: str, *, limit: int = 40) -> list[dict[str, str]]:
        """列出頁面上可點的元素，用來補 play_selectors 清單。"""
        self.goto(url)
        self.dismiss_popups()
        if self.settings.settle_seconds:
            time.sleep(self.settings.settle_seconds)
        script = """
        (limit) => {
          const out = [];
          const nodes = document.querySelectorAll(
            "button, a, [role=button], video, [class*=play i], [aria-label]"
          );
          for (const el of nodes) {
            const box = el.getBoundingClientRect();
            if (box.width < 4 || box.height < 4) continue;
            const style = getComputedStyle(el);
            if (style.visibility === "hidden" || style.display === "none") continue;
            out.push({
              tag: el.tagName.toLowerCase(),
              text: (el.innerText || el.value || "").trim().slice(0, 60),
              aria: el.getAttribute("aria-label") || "",
              id: el.id || "",
              cls: (el.className && el.className.toString ? el.className.toString() : "").slice(0, 80),
            });
            if (out.length >= limit) break;
          }
          return out;
        }
        """
        return list(self.page.evaluate(script, limit))


# ── 工廠 ────────────────────────────────────────────────────────────────

def make_navigator(settings: BrowserSettings) -> Navigator:
    """依設定建立導頁器；attach 接不上時可退回 open。"""
    mode = (settings.mode or MODE_ATTACH).lower()
    if mode not in MODES:
        raise BrowserError(f"[browser] mode 只能是 {' / '.join(MODES)}，讀到 {settings.mode!r}")

    if mode == MODE_OPEN:
        navigator: Navigator = OsOpenNavigator()
        navigator.start()
        return navigator

    navigator = AttachNavigator(settings)
    try:
        navigator.start()
    except BrowserError as exc:
        if settings.fallback_to_open:
            log.warning("%s", exc)
            log.warning("改用系統預設瀏覽器開新分頁（錄影照舊進行，只是不會自動按播放）")
            fallback = OsOpenNavigator()
            fallback.start()
            return fallback
        raise
    return navigator
