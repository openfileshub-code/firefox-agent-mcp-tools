import sys
import os
import logging
import asyncio
import subprocess
import re
import time
import shutil

# Настройка логирования
# ОТКЛЮЧЕНО: level=logging.CRITICAL + 1 подавляет все сообщения.
# Чтобы включить обратно, замени на logging.DEBUG или logging.INFO
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_debug.log")
logging.basicConfig(
    filename=LOG_FILE, 
    level=logging.CRITICAL + 1,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("FirefoxMCP_Camoufox_Clean")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("MCP library not installed. Run: pip install mcp")
    sys.exit(1)

# КРИТИЧНО: импортируем Camoufox ДО запуска event loop!
import io as _io
_old_stdout = sys.stdout
_old_stderr = sys.stderr
sys.stdout = _io.StringIO()
sys.stderr = _io.StringIO()
try:
    from camoufox.async_api import AsyncCamoufox
    _captured_out = sys.stdout.getvalue() if hasattr(sys.stdout, 'getvalue') else ''
    _captured_err = sys.stderr.getvalue() if hasattr(sys.stderr, 'getvalue') else ''
    sys.stdout = _old_stdout
    sys.stderr = _old_stderr
    if _captured_out:
        logger.warning(f"Playwright wrote to stdout during import (suppressed): {_captured_out[:500]}")
    if _captured_err:
        logger.debug(f"Playwright wrote to stderr during import (suppressed): {_captured_err[:500]}")
    logger.info("Camoufox imported at module level (before event loop, stdout protected).")
except ImportError:
    sys.stdout = _old_stdout
    sys.stderr = _old_stderr
    logger.error("Camoufox not installed. Run: pip install camoufox[geoip] && python -m camoufox fetch")
    AsyncCamoufox = None

mcp = FastMCP("FirefoxBrowserAgent")


async def nuke_all_browser_processes():
    targets = ["firefox.exe", "camoufox.exe", "playwright.exe"]
    for proc in targets:
        try:
            proc_obj = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/IM", proc, "/T",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc_obj.wait()
        except Exception as e:
            logger.debug(f"taskkill {proc} skipped: {e}")
    await asyncio.sleep(5)
    logger.info("All browser and driver processes nuked. Waiting 5s complete.")


def remove_lock_files(profile_dir: str):
    if not os.path.exists(profile_dir):
        return
    lock_files = ["parent.lock", "lock", ".parentlock"]
    for lf in lock_files:
        path = os.path.join(profile_dir, lf)
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"Removed lock file: {path}")
            except Exception as e:
                logger.warning(f"Could not remove {path}: {e}")


class BrowserManager:
    def __init__(self):
        self._camoufox_ctx = None
        self.page = None
        self.is_running = False
        self.engine = "none"
        self._keepalive_task = None
        self.profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playwright_profile")

    async def start(self, headless: bool = False) -> str:
        if self.is_running:
            return "Browser is already running."
        try:
            logger.info("STEP 1: Nuking browser processes...")
            await nuke_all_browser_processes()
            os.makedirs(self.profile_dir, exist_ok=True)
            logger.info("STEP 2: Removing lock files...")
            remove_lock_files(self.profile_dir)
            if AsyncCamoufox is None:
                return "❌ camoufox not installed. Run: pip install camoufox[geoip] && python -m camoufox fetch"
            logger.info("STEP 3: AsyncCamoufox available (imported at module level).")
            actual_headless = False
            if headless:
                logger.warning("Camoufox headless=True crashes on Windows. Forcing headless=False.")
            logger.info("STEP 4: Constructing AsyncCamoufox object...")
            self._camoufox_ctx = AsyncCamoufox(
                headless=actual_headless,
                os="windows",
                humanize=True,
                geoip=False,
                persistent_context=True,
                user_data_dir=self.profile_dir,
            )
            logger.info("STEP 4 complete: AsyncCamoufox object created.")
            logger.info("STEP 5: Entering AsyncCamoufox context (launching browser, timeout=60s)...")
            self.context = await asyncio.wait_for(
                self._camoufox_ctx.__aenter__(), 
                timeout=60.0
            )
            logger.info("STEP 5 complete: Browser launched successfully!")
            logger.info("STEP 6: Getting page...")
            pages = self.context.pages
            if pages:
                self.page = pages[0]
            else:
                self.page = await self.context.new_page()
            logger.info("STEP 6 complete: Page acquired.")
            self.is_running = True
            self.engine = "camoufox"
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            logger.info(f"STEP 7: ALL DONE. Camoufox running. Profile: {self.profile_dir}")
            return f"✅ Camoufox started successfully.\nProfile dir: {self.profile_dir}\nHeadless: {actual_headless}\nKeepalive: active (min 1h)"
        except asyncio.TimeoutError:
            logger.error("START TIMEOUT: __aenter__() took more than 60s. Browser failed to launch.")
            await self.stop()
            raise RuntimeError("Failed to start Camoufox: timeout after 60s. Check mcp_debug.log for details.")
        except Exception as e:
            logger.error(f"Start failed: {e}", exc_info=True)
            await self.stop()
            raise RuntimeError(f"Failed to start Camoufox: {str(e)}")

    async def _keepalive_loop(self):
        try:
            for i in range(12):
                await asyncio.sleep(300)
                if not self.is_running or not self.page:
                    break
                try:
                    await self.page.evaluate("() => document.readyState")
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def stop(self) -> str:
        if not self.is_running and not self._camoufox_ctx:
            return "Browser is not running."
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
        try:
            if self._camoufox_ctx:
                await self._camoufox_ctx.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"Error during stop: {e}")
        finally:
            self._camoufox_ctx = None
            self.context = None
            self.page = None
            self.is_running = False
            self.engine = "none"
            logger.info("Browser stopped.")
            await nuke_all_browser_processes()
            return "🛑 Browser stopped."

    async def navigate(self, url: str) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1.5)
            current_url = self.page.url
            title = await self.page.title()
            status = response.status if response else "unknown"
            logger.info(f"Navigated to {current_url} (Status: {status})")
            return f"✅ Navigated to: {current_url}\nTitle: {title}\nStatus: {status}"
        except Exception as e:
            logger.error(f"Navigation error: {e}")
            return f"❌ Navigation error: {str(e)}"

    async def get_content(self) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            html = await self.page.content()
            clean_text = re.sub(r'<script[^>]*?>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r'<style[^>]*?>.*?</style>', '', clean_text, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            if len(clean_text) > 8000:
                clean_text = clean_text[:8000] + "\n... [truncated]"
            return clean_text
        except Exception as e:
            logger.error(f"Content extraction error: {e}")
            return f"❌ Content extraction error: {str(e)}"

    async def click_element(self, selector: str) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.wait_for_selector(selector, state="visible", timeout=15000)
            await self.page.click(selector)
            await asyncio.sleep(0.5)
            return f"✅ Clicked: {selector}"
        except Exception as e:
            logger.error(f"Click error: {e}")
            return f"❌ Click error: {str(e)}"

    async def fill_input(self, selector: str, value: str) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.wait_for_selector(selector, state="visible", timeout=15000)
            await self.page.click(selector)
            await asyncio.sleep(0.3)
            await self.page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)
            await self.page.keyboard.press("Backspace")
            await asyncio.sleep(0.1)
            import random
            for char in value:
                await self.page.keyboard.type(char, delay=random.uniform(50, 120))
            return f"✅ Filled (human-like): {selector}"
        except Exception as e:
            logger.error(f"Fill error: {e}")
            try:
                await self.page.fill(selector, value)
                logger.warning(f"Used instant fill fallback for {selector} - bot detection risk!")
                return f"⚠️ Filled (instant fallback): {selector}"
            except Exception as e2:
                return f"❌ Fill error: {str(e2)}"

    async def screenshot(self, filename: str = "shot.png") -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
            await self.page.screenshot(path=full_path, full_page=False)
            return f"✅ Screenshot saved: {full_path}"
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return f"❌ Screenshot error: {str(e)}"

    async def select_option(self, selector: str, value: str) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.wait_for_selector(selector, state="visible", timeout=15000)
            await self.page.select_option(selector, value)
            await asyncio.sleep(0.3)
            return f"✅ Selected '{value}' in: {selector}"
        except Exception as e:
            logger.error(f"Select error: {e}")
            return f"❌ Select error: {str(e)}"

    async def press_key(self, key: str) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.keyboard.press(key)
            await asyncio.sleep(0.3)
            return f"✅ Key pressed: {key}"
        except Exception as e:
            logger.error(f"Key press error: {e}")
            return f"❌ Key press error: {str(e)}"

    async def wait_for(self, selector_or_url: str, timeout: int = 15000) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            if selector_or_url.startswith(("http://", "https://", "/")) or "/" in selector_or_url:
                await self.page.wait_for_url(f"**{selector_or_url}**", timeout=timeout)
                return f"✅ URL now contains: {selector_or_url}"
            else:
                await self.page.wait_for_selector(selector_or_url, state="visible", timeout=timeout)
                return f"✅ Element appeared: {selector_or_url}"
        except Exception as e:
            logger.error(f"Wait error: {e}")
            return f"❌ Wait timeout/error: {str(e)}"

    async def evaluate(self, expression: str) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            result = await self.page.evaluate(expression)
            result_str = str(result)[:2000]
            return f"✅ JS result: {result_str}"
        except Exception as e:
            logger.error(f"Evaluate error: {e}")
            return f"❌ JS error: {str(e)}"

    async def get_url(self) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        return f"📍 Current URL: {self.page.url}"

    async def check(self, selector: str) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.wait_for_selector(selector, state="visible", timeout=15000)
            await self.page.check(selector)
            return f"✅ Checked: {selector}"
        except Exception as e:
            logger.error(f"Check error: {e}")
            return f"❌ Check error: {str(e)}"

    async def uncheck(self, selector: str) -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.wait_for_selector(selector, state="visible", timeout=15000)
            await self.page.uncheck(selector)
            return f"✅ Unchecked: {selector}"
        except Exception as e:
            logger.error(f"Uncheck error: {e}")
            return f"❌ Uncheck error: {str(e)}"

    async def get_menu_links(self, menu_type: str = "all") -> str:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            js_code = """
            (menuType) => {
                const results = [];
                const seenUrls = new Set();
                const viewHeight = window.innerHeight || document.documentElement.clientHeight;
                const trashPatterns = [
                    'facebook.com', 'twitter.com', 'x.com', 'instagram.com',
                    'linkedin.com', 'youtube.com', 'tiktok.com', 'pinterest.com',
                    'mailto:', 'tel:', 'javascript:', 'data:'
                ];
                const isTrash = (href) => {
                    if (!href || href === '#' || href.trim() === '') return true;
                    const lower = href.toLowerCase();
                    return trashPatterns.some(t => lower.includes(t));
                };
                const getSection = (el) => {
                    try {
                        const rect = el.getBoundingClientRect();
                        const y = rect.top;
                        if (y < viewHeight * 0.35) return 'Header';
                        if (y > viewHeight * 0.65) return 'Footer';
                        return 'Sidebar/Main';
                    } catch(e) {
                        return 'Unknown';
                    }
                };
                const isMenuContext = (el) => {
                    if (!el || !el.tagName) return false;
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    if (['navigation', 'menu', 'menubar', 'menuitem', 'tablist'].includes(role)) return true;
                    const tag = el.tagName.toLowerCase();
                    if (['nav', 'header', 'footer'].includes(tag)) return true;
                    const classId = ((el.className || '') + ' ' + (el.id || '')).toLowerCase().replace(/[^a-z0-9-_]/g, ' ');
                    const menuKeywords = ['nav', 'menu', 'header', 'footer', 'topbar', 'navbar', 'sidebar', 'main-nav', 'site-nav', 'app-bar', 'toolbar'];
                    if (menuKeywords.some(kw => classId.includes(kw))) return true;
                    return false;
                };
                const allLinks = Array.from(document.querySelectorAll('a[href]'));
                const clusters = new Map();
                for (const link of allLinks) {
                    if (isTrash(link.href)) continue;
                    let container = link.parentElement;
                    let depth = 0;
                    let bestContainer = link.parentElement;
                    while (container && depth < 5 && container !== document.body) {
                        if (isMenuContext(container)) {
                            bestContainer = container;
                            break;
                        }
                        const tag = container.tagName.toLowerCase();
                        if (tag === 'ul' || tag === 'ol' || tag === 'nav') {
                            bestContainer = container;
                            break;
                        }
                        container = container.parentElement;
                        depth++;
                    }
                    if (!clusters.has(bestContainer)) {
                        clusters.set(bestContainer, []);
                    }
                    clusters.get(bestContainer).push(link);
                }
                for (const [container, links] of clusters.entries()) {
                    if (links.length < 2) continue;
                    const section = getSection(container);
                    if (menuType === 'header' && section !== 'Header') continue;
                    if (menuType === 'footer' && section !== 'Footer') continue;
                    for (const link of links) {
                        const href = link.href;
                        if (seenUrls.has(href)) continue;
                        seenUrls.add(href);
                        let text = link.textContent.trim().replace(/\\s+/g, ' ');
                        if (!text) {
                            text = link.getAttribute('aria-label') || link.getAttribute('title') || link.getAttribute('alt') || '[no text]';
                        }
                        results.push({
                            section: section,
                            text: text.substring(0, 100),
                            url: href
                        });
                    }
                }
                if (results.length === 0) {
                    for (const link of allLinks) {
                        if (isTrash(link.href)) continue;
                        if (seenUrls.has(link.href)) continue;
                        seenUrls.add(link.href);
                        let text = link.textContent.trim().replace(/\\s+/g, ' ') || link.getAttribute('aria-label') || '[no text]';
                        results.push({
                            section: 'Unclassified (Fallback)',
                            text: text.substring(0, 100),
                            url: link.href
                        });
                        if (results.length >= 50) break;
                    }
                }
                return results;
            }
            """
            links = await self.page.evaluate(js_code, menu_type)
            if not links:
                return f"⚠️ No menu links found (type: {menu_type}). Page might use non-standard navigation or has no links."
            output_lines = [f"📋 Menu links ({menu_type}) — found {len(links)}:\n"]
            current_section = None
            for item in links:
                section = item.get('section', 'Unknown')
                if section != current_section:
                    current_section = section
                    output_lines.append(f"\n[{current_section}]")
                text = item.get('text', '[no text]')
                url = item.get('url', '')
                output_lines.append(f"  • {text} → {url}")
            result_text = "\n".join(output_lines)
            if len(result_text) > 10000:
                result_text = result_text[:10000] + "\n... [truncated, too many links]"
            return result_text
        except Exception as e:
            logger.error(f"Get menu links error: {e}")
            return f"❌ Menu extraction error: {str(e)}"


manager = BrowserManager()


@mcp.tool()
async def browser_start(headless: bool = False) -> str:
    """Starts browser with anti-detect protection (Camoufox preferred). Kills existing instances first. Preserves session cookies between runs."""
    return await manager.start(headless)


@mcp.tool()
async def browser_stop() -> str:
    """Stops the browser."""
    return await manager.stop()


@mcp.tool()
async def browser_navigate(url: str) -> str:
    """Navigates to URL."""
    return await manager.navigate(url)


@mcp.tool()
async def browser_get_content() -> str:
    """Gets page text."""
    return await manager.get_content()


@mcp.tool()
async def browser_click(selector: str) -> str:
    """Clicks element."""
    return await manager.click_element(selector)


@mcp.tool()
async def browser_fill(selector: str, value: str) -> str:
    """Fills input field. Uses sequential typing to mimic human behavior."""
    return await manager.fill_input(selector, value)


@mcp.tool()
async def browser_screenshot(filename: str = "shot.png") -> str:
    """Takes screenshot."""
    return await manager.screenshot(filename)


@mcp.tool()
async def browser_select_option(selector: str, value: str) -> str:
    """Selects an option in a <select> dropdown by its value attribute. Use for country, role, or any dropdown form field."""
    return await manager.select_option(selector, value)


@mcp.tool()
async def browser_press_key(key: str) -> str:
    """Presses a keyboard key. Examples: 'Enter', 'Tab', 'Escape', 'ArrowDown'. Use after filling forms to submit or navigate between fields."""
    return await manager.press_key(key)


@mcp.tool()
async def browser_wait_for(selector_or_url: str, timeout: int = 15000) -> str:
    """Waits for a CSS selector to appear on page OR for URL to contain a substring. Use after login clicks to wait for redirect. Examples: selector='.dashboard', url='/account'."""
    return await manager.wait_for(selector_or_url, timeout)


@mcp.tool()
async def browser_evaluate(expression: str) -> str:
    """Executes JavaScript in the browser and returns the result. Use for getting/setting values, checking login state, or interacting with page APIs. Example: 'document.title' or 'localStorage.getItem(\"token\")'"""
    return await manager.evaluate(expression)


@mcp.tool()
async def browser_get_url() -> str:
    """Returns the current page URL. Use to verify navigation or check if login redirect happened."""
    return await manager.get_url()


@mcp.tool()
async def browser_check(selector: str) -> str:
    """Checks a checkbox or radio button. Use for 'I agree' checkboxes, 'Remember me', etc."""
    return await manager.check(selector)


@mcp.tool()
async def browser_uncheck(selector: str) -> str:
    """Unchecks a checkbox. Use to deselect options."""
    return await manager.uncheck(selector)


@mcp.tool()
async def browser_get_menu_links(menu_type: str = "all") -> str:
    """Extracts navigation links from the page menus. Use this to quickly find where to navigate without parsing the whole page. Args: menu_type='header' (top nav), 'footer' (bottom links), or 'all' (both). Returns formatted list of 'Link Text -> URL'. Example: call with menu_type='header' to find login/products/about links."""
    return await manager.get_menu_links(menu_type)


if __name__ == "__main__":
    logger.info("="*50)
    logger.info("Server starting (Clean Camoufox Mode)...")
    logger.info("="*50)
    try:
        mcp.run(transport="stdio")
    except Exception as e:
        logger.critical(f"CRASH: {e}", exc_info=True)
        raise