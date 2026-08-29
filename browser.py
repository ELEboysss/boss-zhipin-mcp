"""Playwright browser management — connects to existing Chrome via CDP."""

import json
import os
import asyncio
import random
import logging
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from config import (
    COOKIES_DIR, COOKIES_FILE,
    MIN_DELAY, MAX_DELAY, BOSS_BASE_URL
)

log = logging.getLogger("boss-browser")

# CDP endpoint for Chrome launched with --remote-debugging-port
CDP_URL = os.getenv("BOSS_CDP_URL", "http://localhost:9222")
CDP_DETECT_PORTS = [9222, 9229, 19222]


class BossBrowser:
    """Connects to an existing Chrome via CDP for BOSS 直聘.

    Supports multiple connection strategies:
    1. CDP connect to user-specified port (CDP_URL env var)
    2. Auto-detect Chrome debug port on common ports
    3. Fallback: launch a new Chromium instance
    """

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def launch(self):
        """Connect to Chrome via CDP with automatic fallback."""
        self._playwright = await async_playwright().start()

        # Strategy 1: Try configured CDP URL
        if await self._try_cdp_connect(CDP_URL):
            log.info(f"Connected via CDP: {CDP_URL}")
            return

        # Strategy 2: Auto-detect Chrome debug port
        for port in CDP_DETECT_PORTS:
            url = f"http://localhost:{port}"
            if url == CDP_URL:
                continue  # already tried
            if await self._try_cdp_connect(url):
                log.info(f"Auto-detected Chrome at port {port}")
                return

        # Strategy 3: Launch system Chrome with debug port, then connect via CDP
        log.info("No running Chrome found, launching system Chrome with debug port")
        launched_port = await self._launch_system_chrome()
        if launched_port:
            cdp_url = f"http://localhost:{launched_port}"
            if await self._try_cdp_connect(cdp_url):
                log.info(f"Connected to system Chrome at port {launched_port}")
                return

        # Strategy 4: Fallback to bare Chromium (no user profile)
        log.info("System Chrome launch failed, falling back to bare Chromium")
        await self._launch_new_browser()

    async def _launch_system_chrome(self) -> int | None:
        """Launch system Chrome with --remote-debugging-port.

        Uses the user's default Chrome profile so existing cookies/logins are available.
        Returns the debug port if successful, None otherwise.
        """
        import subprocess
        import platform

        port = 9222
        system = platform.system()
        if system == "Darwin":
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        elif system == "Linux":
            chrome_path = "google-chrome"
        else:
            chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        # Use a dedicated user-data-dir so debug port works even if Chrome was running
        profile_dir = os.path.join(os.path.dirname(__file__), "chrome-profile")
        try:
            subprocess.Popen(
                [chrome_path, f"--remote-debugging-port={port}",
                 f"--user-data-dir={profile_dir}",
                 # 防止 Chrome 卡在首次运行引导（FRE）：FRE 活跃时会劫持/清空
                 # 所有新标签页（about:blank），自动化全部失效。详见 docs/TROUBLESHOOTING.md P1
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-session-crashed-bubble"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait for Chrome to start and debug port to be ready
            for _ in range(15):
                await asyncio.sleep(1)
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2)
                    return port
                except Exception:
                    continue
        except FileNotFoundError:
            log.warning(f"Chrome not found at {chrome_path}")
        except Exception as e:
            log.warning(f"Failed to launch system Chrome: {e}")
        return None

    async def _try_cdp_connect(self, url: str) -> bool:
        """Try to connect to Chrome via CDP at the given URL."""
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                url, timeout=5000
            )
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
                pages = self._context.pages
                self._page = pages[0] if pages else await self._context.new_page()
            else:
                self._context = await self._browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    locale="zh-CN",
                )
                self._page = await self._context.new_page()
            return True
        except Exception:
            return False

    async def _launch_new_browser(self):
        """Fallback: launch a new Chromium instance (user must log in manually)."""
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        await self._load_cookies()
        self._page = await self._context.new_page()

    async def close(self):
        """Disconnect (does NOT close the user's Chrome)."""
        if self._context:
            await self._save_cookies()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._context = None
        self._page = None

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    @property
    def is_alive(self) -> bool:
        """Check if browser is still connected."""
        try:
            return self._browser is not None and self._browser.is_connected()
        except Exception:
            return False

    async def _load_cookies(self):
        """Load cookies from file."""
        if os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE, "r") as f:
                cookies = json.load(f)
            await self._context.add_cookies(cookies)

    async def _save_cookies(self):
        """Save cookies to file."""
        os.makedirs(COOKIES_DIR, exist_ok=True)
        try:
            cookies = await self._context.cookies()
            with open(COOKIES_FILE, "w") as f:
                json.dump(cookies, f, indent=2)
        except Exception:
            pass

    async def is_logged_in(self) -> bool:
        """Check if currently logged in to BOSS 直聘.

        First tries to check from current page without navigation.
        Only navigates if current page is not a BOSS page.
        """
        # Try checking current page first (no navigation)
        try:
            current_url = self.page.url
            if "zhipin.com" in current_url:
                return await self._check_current_page_logged_in()
        except Exception:
            pass

        # Current page is not BOSS — navigate to check
        await self.page.goto(BOSS_BASE_URL, wait_until="networkidle")
        await asyncio.sleep(3)
        return await self._check_current_page_logged_in()

    async def _check_current_page_logged_in(self) -> bool:
        """Check login state from current page WITHOUT navigating away."""
        current_url = self.page.url
        if "login" in current_url or "/web/user" in current_url or "bticket" in current_url:
            body_class = await self.page.evaluate("document.body.className || ''")
            if "login" in body_class:
                return False
        if "/web/boss/" in current_url or "/web/chat/" in current_url:
            return True
        try:
            logged_in = await self.page.query_selector(".user-nav, .btn-post-job, .nav-figure, .menu-list")
            return logged_in is not None
        except Exception:
            return False

    async def check_and_screenshot_verification(self) -> dict | None:
        """Check if the current page has a verification/CAPTCHA overlay.

        Returns screenshot info if verification detected, None otherwise.
        """
        try:
            has_verify = await self.page.evaluate("""() => {
                const text = document.body.innerText || '';
                const selectors = [
                    '.verify-wrap', '.captcha', '.slider-verify',
                    '[class*="verify"]', '[class*="captcha"]',
                    '.boss-popup__wrapper'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetWidth > 0) return true;
                }
                return text.includes('安全验证') || text.includes('滑动验证')
                    || text.includes('请完成验证');
            }""")
            if has_verify:
                screenshot_path = os.path.join(
                    os.path.dirname(__file__), "screenshot_verification.png"
                )
                await self.page.screenshot(path=screenshot_path)
                return {
                    "needs_verification": True,
                    "screenshot": screenshot_path,
                    "message": "检测到安全验证，请在浏览器中手动完成验证后告知",
                }
        except Exception:
            pass
        return None

    async def login(self) -> dict:
        """Navigate to login page. User needs to manually complete login."""
        await self.page.goto(f"{BOSS_BASE_URL}/web/user/?ka=header-login", wait_until="domcontentloaded")

        for _ in range(90):
            await asyncio.sleep(2)
            if await self._check_current_page_logged_in():
                await self._save_cookies()
                return {"status": "success", "message": "登录成功，Cookie 已保存"}

        return {"status": "timeout", "message": "登录超时（3分钟），请在浏览器中完成登录后重试"}

    async def random_delay(self):
        """Random delay to mimic human behavior."""
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        await asyncio.sleep(delay)

    # --- 运行时环境自检（详见 docs/TROUBLESHOOTING.md） ---

    # Chrome 首次运行引导（FRE）相关的标签页特征
    FRE_URL_MARKERS = ("chrome://intro", "privacy-sandbox-dialog",
                       "accounts.google.com/signin", "chrome-error://")

    async def diagnose_environment(self, probe_tab: bool = True) -> dict:
        """自检浏览器环境，返回结构化诊断结果（供 agent 快速定位问题）。

        检测项：
        - first_run_experience: Chrome 卡在首次运行引导，新标签页会被劫持清空
        - tab_wipe: 新标签页导航后存活测试（验证 BOSS 反自动化/ FRE 是否影响）
        - login_state: 当前页面登录态（仅在 zhipin 页面时判定，不主动导航）
        - proxy_env: 系统代理缺少 NO_PROXY 会导致 CDP 连不上 localhost

        probe_tab=False 时跳过新建标签页的存活测试（更轻量）。
        """
        findings: list[dict] = []

        if not self.is_alive:
            return {"ok": False, "findings": [{
                "code": "NO_BROWSER", "level": "error",
                "detail": "未连接到浏览器（CDP）",
                "fix": "调用任一 boss_* 工具触发连接；若仍失败，检查 Chrome 是否以 "
                       "--remote-debugging-port=9222 启动，以及系统代理是否拦截 localhost"
                       "（需 NO_PROXY=localhost,127.0.0.1）",
            }]}

        # 1. FRE 检测
        fre_tabs = []
        try:
            for pg in self._context.pages:
                u = pg.url or ""
                if any(m in u for m in self.FRE_URL_MARKERS):
                    fre_tabs.append(u)
        except Exception:
            pass
        if fre_tabs:
            findings.append({
                "code": "FIRST_RUN_EXPERIENCE", "level": "error",
                "detail": f"Chrome 卡在首次运行引导，相关标签页：{fre_tabs}。"
                          "此状态下新建标签页会在几秒内被清成 about:blank，自动化必然失败",
                "fix": "让用户在该 Chrome 窗口中完成/跳过引导（关掉「登录 Chrome」和隐私弹窗）；"
                       "或删除 mcp/chrome-profile 后重启（将用 --no-first-run 重新拉起）；"
                       "采集类临时任务可改用独立 profile 的浏览器（docs/TROUBLESHOOTING.md P1）",
            })

        # 2. 标签页存活测试（可选）
        if probe_tab:
            wiped = None
            page = None
            try:
                page = await self._context.new_page()
                await page.goto(f"{BOSS_BASE_URL}/web/boss/recommend",
                                wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(4)
                url = page.url or ""
                wiped = "zhipin.com" not in url
            except Exception as e:
                wiped = f"exception: {str(e)[:100]}"
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
            if wiped is True:
                findings.append({
                    "code": "TAB_WIPE", "level": "error",
                    "detail": "新标签页导航后被清空（about:blank）。若同时存在 FIRST_RUN_EXPERIENCE "
                              "则是引导问题；否则是 BOSS 反自动化风控升级（有累积效应）",
                    "fix": "FRE → 见上；风控 → 立即停止自动化，冷却数小时~几天后再试，"
                           "切勿高频重试（docs/TROUBLESHOOTING.md P3/P7）",
                })
            elif isinstance(wiped, str):
                findings.append({
                    "code": "TAB_PROBE_ERROR", "level": "warning",
                    "detail": f"标签页测试异常：{wiped}",
                    "fix": "重试 boss_diagnose；持续异常则按 TAB_WIPE 处理",
                })

        # 3. 登录态（仅当前页面在 zhipin 时判定，不主动导航劫持用户标签页）
        try:
            cur = self.page.url or ""
            if "zhipin.com" in cur:
                if await self._check_current_page_logged_in():
                    findings.append({"code": "LOGIN_STATE", "level": "info",
                                     "detail": "当前页面处于已登录状态", "fix": ""})
                else:
                    findings.append({
                        "code": "NOT_LOGGED_IN", "level": "warning",
                        "detail": "当前 zhipin 页面未检测到登录态。注意：cookie 里出现 "
                                  "__c/__g/__l 不代表登录成功（匿名也会种）",
                        "fix": "调用 boss_login 并提示用户：扫码后手机上要确认，"
                               "如有身份选择按用途选（招聘者/牛人）",
                    })
            else:
                findings.append({"code": "LOGIN_STATE", "level": "info",
                                 "detail": "当前页面不在 zhipin.com，未判定登录态", "fix": ""})
        except Exception:
            pass

        # 4. 代理环境
        http_proxy = os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""
        no_proxy = os.getenv("NO_PROXY") or os.getenv("no_proxy") or ""
        if http_proxy and "localhost" not in no_proxy and "127.0.0.1" not in no_proxy:
            findings.append({
                "code": "PROXY_MISSING_NO_PROXY", "level": "warning",
                "detail": f"检测到系统代理 {http_proxy} 但未配置 NO_PROXY，"
                          "localhost 的 CDP 请求可能被代理拦截",
                "fix": "为 MCP server 进程设置 NO_PROXY=localhost,127.0.0.1"
                       "（preset/mcp-row.yml 已内置，自定义部署时需自行添加）",
            })

        ok = not any(f["level"] == "error" for f in findings)
        if not findings:
            findings.append({"code": "ALL_CLEAR", "level": "info",
                             "detail": "环境正常", "fix": ""})
        return {"ok": ok, "findings": findings}
