"""把瀏覽器導到課程網頁。三種模式，預設 launch。

launch（推薦）
    程式自己開一個它控制得到的瀏覽器，用固定的 profile 目錄記住登入狀態。
    第一次跑的時候你在那個視窗手動登入 AU2026，之後每場課都由程式導同一個視窗。
    程式預設不碰視窗大小與位置 —— 你把瀏覽器擺在哪個螢幕、多大，錄到的就是那樣。

attach
    接管你自己開好、已登入的 Chromium 系瀏覽器（Brave / Chrome / Edge），
    要先用 --remote-debugging-port=9222 啟動它。程式導的是同一個分頁，
    所以視窗位置、大小、在哪個螢幕都不會變 —— OBS 錄到的永遠是對的畫面。

open
    直接呼叫系統「開這個網址」，零設定零依賴。缺點是每場開一個新分頁，
    而且新分頁會落在最後使用的那個瀏覽器視窗。

自動化那端出問題時（瀏覽器沒開起來、找不到播放鍵），會依 fallback_to_open
退回 open 模式，不讓一場課因為自動化壞掉而完全錄不到。
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

MODE_ATTACH = "attach"
MODE_OPEN = "open"
MODE_LAUNCH = "launch"
MODES = (MODE_ATTACH, MODE_OPEN, MODE_LAUNCH)


class BrowserError(RuntimeError):
    pass


@dataclass
class BrowserSettings:
    mode: str = MODE_LAUNCH
    cdp_url: str = "http://localhost:9222"
    fallback_to_open: bool = True
    user_data_dir: Path = Path("browser-profile")
    channel: str = "chrome"
    headless: bool = False
    window_size: tuple[int, int] | None = None
    window_position: tuple[int, int] | None = None
    start_fullscreen: bool = False
    settle_seconds: int = 8
    play_selectors: Sequence[str] = field(default_factory=list)
    fullscreen: bool = False
    fullscreen_selectors: Sequence[str] = field(default_factory=list)
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
            "fullscreen": False,
            "navigator": self.label,
            "note": "由系統瀏覽器開啟，未嘗試按播放鍵",
        }


# ── Playwright 共用 ────────────────────────────────────────────────────

class PlaywrightNavigator(Navigator):
    """attach / launch 共用的部分：導頁、關彈窗、按播放、全螢幕。"""

    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    # ── 生命週期 ────────────────────────────────────────────────────────
    def _sync_playwright(self) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 安裝問題
            raise BrowserError(
                "缺少 playwright，請執行 pip install playwright"
            ) from exc
        return sync_playwright().start()

    def close(self) -> None:
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:  # pragma: no cover - 關閉失敗無所謂
            log.debug("關閉 Playwright 時出錯", exc_info=True)
        self._playwright = self._browser = self._context = self._page = None

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
        result: dict[str, Any] = {
            "url": url,
            "played": False,
            "fullscreen": False,
            "navigator": self.label,
        }
        self.goto(url)
        if self.settings.start_fullscreen:
            result["window_fullscreen"] = self.force_fullscreen()
        self.dismiss_popups()
        if self.settings.settle_seconds:
            time.sleep(self.settings.settle_seconds)
        self.dismiss_popups()

        hit = self._click_first(self.settings.play_selectors, what="播放")
        result["play_selector"] = hit
        result["played"] = hit is not None

        if hit is not None and self.settings.fullscreen:
            time.sleep(2)
            full = self._click_first(
                self.settings.fullscreen_selectors, what="全螢幕", timeout_ms=1_500
            )
            if full is None:
                try:  # 大多數播放器支援 f 熱鍵
                    self.page.keyboard.press("f")
                    full = "keyboard:f"
                    log.info("全螢幕：改用 f 熱鍵")
                except Exception:
                    log.debug("全螢幕熱鍵失敗", exc_info=True)
            result["fullscreen"] = full is not None
            result["fullscreen_selector"] = full
        return result

    def leave_session(self) -> None:
        try:
            self.page.keyboard.press("Escape")
        except Exception:
            log.debug("退出全螢幕失敗", exc_info=True)
        if not self.settings.close_page_after:
            return
        try:  # 導回空白頁，確保影片與聲音停下來
            self.page.goto("about:blank", wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            log.debug("導回空白頁失敗", exc_info=True)

    def force_fullscreen(self) -> bool:
        """用 CDP 把視窗切成全螢幕。

        比按 F11 或播放器的全螢幕鍵可靠：Fullscreen API 要求「使用者手勢」，
        Playwright 送的合成按鍵不算數，但 CDP 的 Browser.setWindowBounds 不受限制。
        """
        if self._context is None or self._page is None:
            return False
        try:
            session = self._context.new_cdp_session(self._page)
            window = session.send("Browser.getWindowForTarget")
            session.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": "fullscreen"}},
            )
        except Exception as exc:
            log.warning("CDP 全螢幕失敗（錄到的畫面會含瀏覽器工具列）：%s", exc)
            return False
        log.info("視窗已切成全螢幕")
        return True

    def show_for_login(self, url: str) -> None:
        """開 AU2026 頁面讓使用者登入；登入狀態會留在 profile 目錄裡。"""
        self.goto(url)
        self.dismiss_popups()

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


# ── attach：接管已開好的瀏覽器 ──────────────────────────────────────────

class AttachNavigator(PlaywrightNavigator):
    label = "接管已開啟的瀏覽器（CDP）"

    def start(self) -> None:
        self._playwright = self._sync_playwright()
        url = self.settings.cdp_url
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(url, timeout=10_000)
        except Exception as exc:
            self.close()
            raise BrowserError(
                f"接不上瀏覽器的除錯埠（{url}）：{exc}\n"
                "請先關掉瀏覽器，再用下列參數重新啟動它，登入 Autodesk 後放在要錄的螢幕上：\n"
                '  brave.exe --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\\au2026-profile"\n'
                "（Chrome 136 之後一定要另外指定 --user-data-dir，用預設 profile 會被拒絕）"
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
        except Exception:  # pragma: no cover
            log.debug("停止 Playwright 時出錯", exc_info=True)
        self._playwright = None


# ── launch：程式自己開一個 ──────────────────────────────────────────────

class LaunchNavigator(PlaywrightNavigator):
    label = "程式自己啟動的瀏覽器"

    def start(self) -> None:
        self.settings.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = self._sync_playwright()
        # 預設完全不碰視窗大小與位置：你把視窗擺成什麼樣，錄到的就是什麼樣。
        args: list[str] = []
        if self.settings.window_size is not None:
            args.append("--window-size={},{}".format(*self.settings.window_size))
        if self.settings.window_position is not None:
            args.append("--window-position={},{}".format(*self.settings.window_position))
        if self.settings.start_fullscreen:
            args.append("--start-fullscreen")
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(self.settings.user_data_dir),
            "headless": self.settings.headless,
            # viewport=None 讓頁面跟著實際視窗大小，全螢幕才不會留白邊
            "viewport": None,
            "args": args,
            # 這兩個是為了讓錄到的畫面乾淨：
            #   chromium_sandbox=True  → 不傳 --no-sandbox，就不會出現安全性警告橫幅
            #   忽略 --enable-automation → 不會出現「正受到自動測試軟體控制」橫幅
            "chromium_sandbox": True,
            "ignore_default_args": ["--enable-automation"],
        }
        if self.settings.channel and self.settings.channel != "chromium":
            launch_kwargs["channel"] = self.settings.channel
        try:
            self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            self.close()
            raise BrowserError(
                f"啟動瀏覽器失敗（channel={self.settings.channel!r}）：{exc}。"
                "若沒裝 Chrome，把 [browser] channel 改成 chromium 並執行 playwright install chromium。"
            ) from exc
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # pragma: no cover
            log.debug("關閉瀏覽器 context 時出錯", exc_info=True)
        self._context = self._page = None
        super().close()


# ── 工廠 ────────────────────────────────────────────────────────────────

def make_navigator(settings: BrowserSettings) -> Navigator:
    """依設定建立導頁器；attach 接不上時可退回 open。"""
    mode = (settings.mode or MODE_LAUNCH).lower()
    if mode not in MODES:
        raise BrowserError(f"[browser] mode 只能是 {' / '.join(MODES)}，讀到 {settings.mode!r}")

    if mode == MODE_OPEN:
        navigator: Navigator = OsOpenNavigator()
        navigator.start()
        return navigator

    navigator = AttachNavigator(settings) if mode == MODE_ATTACH else LaunchNavigator(settings)
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
