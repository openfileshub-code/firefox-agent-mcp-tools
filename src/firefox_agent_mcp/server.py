import sys
import os
import logging
import asyncio
import re
import base64
import json as _json

# Настройка логирования
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

# OCR для распознавания капч и текста на картинках
try:
    import ddddocr
    _ocr_engine = ddddocr.DdddOcr(show_ad=False)
    logger.info("ddddocr OCR engine initialized.")
except ImportError:
    _ocr_engine = None
    logger.warning("ddddocr not installed. OCR tools will be unavailable. Run: pip install ddddocr")

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
    """Убивает ВСЕ процессы браузеров, чтобы снять любые lock'и."""
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
    """Удаляет parent.lock и другие мусорные файлы из профиля."""
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


def ocr_image_bytes(image_bytes: bytes) -> str:
    """Распознаёт текст на картинке (PNG/JPG) через ddddocr."""
    if _ocr_engine is None:
        return "❌ OCR unavailable. Install ddddocr: pip install ddddocr"
    try:
        result = _ocr_engine.classification(image_bytes)
        return result if result else "⚠️ No text detected in image."
    except Exception as e:
        return f"❌ OCR error: {str(e)}"


class BrowserManager:
    def __init__(self):
        self._camoufox_ctx = None
        self.page = None
        self.is_running = False
        self.engine = "none"
        self._keepalive_task = None
        self.profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playwright_profile")

    # ================================================================
    # CORE: start / stop / keepalive
    # ================================================================

    async def start(self, headless: bool = False) -> str:
        """Запускает Camoufox напрямую, без Playwright fallback-костылей."""
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
        """Фоновая задача: каждые 5 минут пингует браузер, чтобы он не уснул."""
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
        """Закрывает браузер."""
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

    # ================================================================
    # NAVIGATION & CONTENT
    # ================================================================

    async def navigate(self, url: str) -> str:
        """Переходит по URL."""
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
        """Извлекает текст страницы."""
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

    async def get_url(self) -> str:
        """Возвращает текущий URL."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        return f"📍 Current URL: {self.page.url}"

    # ================================================================
    # INTERACTION
    # ================================================================

    async def click_element(self, selector: str) -> str:
        """Кликает по элементу."""
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
        """Заполняет поле ввода посимвольно (как человек)."""
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

    async def select_option(self, selector: str, value: str) -> str:
        """Выбирает опцию в выпадающем списке <select>."""
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
        """Нажимает клавишу клавиатуры."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.keyboard.press(key)
            await asyncio.sleep(0.3)
            return f"✅ Key pressed: {key}"
        except Exception as e:
            logger.error(f"Key press error: {e}")
            return f"❌ Key press error: {str(e)}"

    async def check(self, selector: str) -> str:
        """Ставит галочку в чекбоксе."""
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
        """Снимает галочку с чекбокса."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.wait_for_selector(selector, state="visible", timeout=15000)
            await self.page.uncheck(selector)
            return f"✅ Unchecked: {selector}"
        except Exception as e:
            logger.error(f"Uncheck error: {e}")
            return f"❌ Uncheck error: {str(e)}"

    # ================================================================
    # WAITING & EVALUATION
    # ================================================================

    async def wait_for(self, selector_or_url: str, timeout: int = 15000) -> str:
        """Ждёт появления элемента или изменения URL."""
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
        """Выполняет JavaScript и возвращает результат."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            result = await self.page.evaluate(expression)
            result_str = str(result)[:2000]
            return f"✅ JS result: {result_str}"
        except Exception as e:
            logger.error(f"Evaluate error: {e}")
            return f"❌ JS error: {str(e)}"

    # ================================================================
    # SCREENSHOT
    # ================================================================

    async def screenshot(self, filename: str = "shot.png") -> str:
        """Делает скриншот."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
            await self.page.screenshot(path=full_path, full_page=False)
            return f"✅ Screenshot saved: {full_path}"
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return f"❌ Screenshot error: {str(e)}"

    # ================================================================
    # SCROLL (NEW)
    # ================================================================

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        """Скроллит страницу на N пикселей вверх/вниз."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            delta = amount if direction == "down" else -amount
            await self.page.evaluate(f"window.scrollBy(0, {delta})")
            await asyncio.sleep(0.3)
            return f"✅ Scrolled {direction} by {amount}px"
        except Exception as e:
            logger.error(f"Scroll error: {e}")
            return f"❌ Scroll error: {str(e)}"

    async def scroll_to_element(self, selector: str, full: bool = True) -> str:
        """Прокручивает страницу к элементу. full=True — элемент полностью в viewport."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            el = self.page.locator(selector)
            await el.wait_for(state="visible", timeout=15000)
            await el.scroll_into_view_if_needed()
            if full:
                box = await el.bounding_box()
                if box:
                    viewport = self.page.viewport_size
                    if viewport:
                        el_bottom = box["y"] + box["height"]
                        vp_height = viewport["height"]
                        if el_bottom > vp_height:
                            overflow = int(el_bottom - vp_height) + 20
                            await self.page.evaluate(f"window.scrollBy(0, {overflow})")
                        elif box["y"] < 0:
                            await self.page.evaluate(f"window.scrollBy(0, {int(box['y']) - 20})")
            await asyncio.sleep(0.3)
            mode = "fully" if full else "partially"
            return f"✅ Scrolled to element ({mode} visible): {selector}"
        except Exception as e:
            logger.error(f"Scroll to element error: {e}")
            return f"❌ Scroll to element error: {str(e)}"

    async def scroll_to_top(self) -> str:
        """Прокручивает страницу к самому верху."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.3)
            return "✅ Scrolled to top"
        except Exception as e:
            return f"❌ Scroll to top error: {str(e)}"

    async def scroll_to_bottom(self) -> str:
        """Прокручивает страницу к самому низу."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            await self.page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            await asyncio.sleep(0.5)
            return "✅ Scrolled to bottom"
        except Exception as e:
            return f"❌ Scroll to bottom error: {str(e)}"

    # ================================================================
    # LINKS (NEW)
    # ================================================================

    async def extract_all_links(self) -> str:
        """Извлекает ВСЕ ссылки на странице с текстом, href и типом (internal/external)."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            js_code = """
            () => {
                const links = [];
                const currentHost = window.location.host;
                const seen = new Set();
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.href;
                    const text = (a.textContent || '').trim().replace(/\\s+/g, ' ').substring(0, 120);
                    if (!href || href === '#' || seen.has(href)) return;
                    seen.add(href);
                    let type = 'internal';
                    try {
                        const url = new URL(href);
                        if (url.host !== currentHost) type = 'external';
                    } catch(e) {
                        if (href.startsWith('http')) type = 'external';
                    }
                    links.push({ text: text || '[no text]', url: href, type: type });
                });
                return links;
            }
            """
            links = await self.page.evaluate(js_code)
            if not links:
                return "⚠️ No links found on this page."
            output = [f"🔗 All links on page — found {len(links)}:\n"]
            internal = [l for l in links if l["type"] == "internal"]
            external = [l for l in links if l["type"] == "external"]
            if internal:
                output.append(f"\n[Internal] ({len(internal)})")
                for l in internal[:100]:
                    output.append(f"  • {l['text']} → {l['url']}")
                if len(internal) > 100:
                    output.append(f"  ... and {len(internal)-100} more")
            if external:
                output.append(f"\n[External] ({len(external)})")
                for l in external[:50]:
                    output.append(f"  • {l['text']} → {l['url']}")
                if len(external) > 50:
                    output.append(f"  ... and {len(external)-50} more")
            result = "\n".join(output)
            if len(result) > 12000:
                result = result[:12000] + "\n... [truncated]"
            return result
        except Exception as e:
            logger.error(f"Extract links error: {e}")
            return f"❌ Extract links error: {str(e)}"

    async def find_link(self, query: str) -> str:
        """Ищет ссылку по подстроке в тексте или URL."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            js_code = """
            (query) => {
                const results = [];
                const q = query.toLowerCase();
                document.querySelectorAll('a[href]').forEach(a => {
                    const text = (a.textContent || '').trim().replace(/\\s+/g, ' ');
                    const href = a.href;
                    if (text.toLowerCase().includes(q) || href.toLowerCase().includes(q)) {
                        results.push({ text: text.substring(0, 120), url: href });
                    }
                });
                return results;
            }
            """
            results = await self.page.evaluate(js_code, query)
            if not results:
                return f"⚠️ No link found matching '{query}'"
            output = [f"🔍 Found {len(results)} link(s) matching '{query}':\n"]
            for r in results[:30]:
                output.append(f"  • {r['text']} → {r['url']}")
            if len(results) > 30:
                output.append(f"  ... and {len(results)-30} more")
            return "\n".join(output)
        except Exception as e:
            logger.error(f"Find link error: {e}")
            return f"❌ Find link error: {str(e)}"

    # ================================================================
    # MENU LINKS (original advanced parser)
    # ================================================================

    async def get_menu_links(self, menu_type: str = "all") -> str:
        """Извлекает ссылки из навигационных меню страницы через продвинутый JS-парсер."""
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
                    } catch(e) { return 'Unknown'; }
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
                        if (isMenuContext(container)) { bestContainer = container; break; }
                        const tag = container.tagName.toLowerCase();
                        if (tag === 'ul' || tag === 'ol' || tag === 'nav') { bestContainer = container; break; }
                        container = container.parentElement;
                        depth++;
                    }
                    if (!clusters.has(bestContainer)) clusters.set(bestContainer, []);
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
                        if (!text) text = link.getAttribute('aria-label') || link.getAttribute('title') || link.getAttribute('alt') || '[no text]';
                        results.push({ section: section, text: text.substring(0, 100), url: href });
                    }
                }
                if (results.length === 0) {
                    for (const link of allLinks) {
                        if (isTrash(link.href)) continue;
                        if (seenUrls.has(link.href)) continue;
                        seenUrls.add(link.href);
                        let text = link.textContent.trim().replace(/\\s+/g, ' ') || link.getAttribute('aria-label') || '[no text]';
                        results.push({ section: 'Unclassified (Fallback)', text: text.substring(0, 100), url: link.href });
                        if (results.length >= 50) break;
                    }
                }
                return results;
            }
            """
            links = await self.page.evaluate(js_code, menu_type)
            if not links:
                return f"⚠️ No menu links found (type: {menu_type})."
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

    # ================================================================
    # OCR / CAPTCHA (DISABLED - detection only)
    # ================================================================

    async def ocr_image(self, source: str) -> str:
        """ЗАГЛУШКА: OCR отключён."""
        return "⚠️ OCR/Captcha recognition is currently DISABLED. If you encounter a captcha, please solve it manually in the browser window and then continue with form submission."

    async def read_captcha(self, selector: str = "") -> str:
        """Только детекция капчи, без распознавания."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            js_detect = """
            () => {
                const patterns = [
                    'img[src*="captcha"]', 'img[src*="recaptcha"]', 'img[alt*="captcha"]',
                    '#captcha', '.captcha', '[class*="captcha"]', '[id*="captcha"]',
                    '.g-recaptcha', '#g-recaptcha-response', '[data-sitekey]',
                    '.h-captcha', '#hcaptcha', '[data-hcaptcha-widget-id]',
                    'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
                    '.cf-challenge', '#challenge-running', '.turnstile'
                ];
                for (const sel of patterns) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            return { found: true, selector: sel, tag: el.tagName };
                        }
                    }
                }
                return { found: false };
            }
            """
            result = await self.page.evaluate(js_detect)
            if result.get('found'):
                return f"🔒 CAPTCHA DETECTED ({result.get('selector', 'unknown')}). Automatic solving is DISABLED. Please solve the captcha MANUALLY in the browser window, then call browser_submit_form() to continue."
            return "✅ No captcha detected on this page."
        except Exception as e:
            logger.error(f"Captcha detection error: {e}")
            return f"⚠️ Could not check for captcha: {str(e)}. If you see a captcha, please solve it manually."

    # ============================================================
    # Form analysis, smart fill, and submit
    # ============================================================

    async def analyze_form(self, form_selector: str) -> str:
        """Analyzes a form and returns all fields with metadata including bot-trap detection."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            js_code = """
            (formSelector) => {
                const result = { fields: [], submitButtons: [], formInfo: {} };
                let form = document.querySelector(formSelector);
                if (!form) {
                    form = document.querySelector('[role="form"]');
                    if (!form) {
                        const inputs = document.querySelectorAll('input, textarea, select');
                        if (inputs.length > 0) {
                            form = document.body;
                            result.formInfo.note = 'No <form> tag found, using body as container';
                        } else {
                            return { error: 'Form not found' };
                        }
                    }
                }
                result.formInfo.action = form.getAttribute ? (form.getAttribute('action') || '') : '';
                result.formInfo.method = form.getAttribute ? (form.getAttribute('method') || 'GET').toUpperCase() : 'GET';
                result.formInfo.id = form.id || '';
                result.formInfo.className = form.className || '';

                function isBotTrap(el) {
                    const reasons = [];
                    try {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        const cls = (el.className || '').toString().toLowerCase();
                        const id = (el.id || '').toLowerCase();
                        const name = (el.name || '').toLowerCase();
                        if (style.display === 'none') reasons.push('display:none');
                        if (style.visibility === 'hidden') reasons.push('visibility:hidden');
                        if (rect.width < 5 && rect.height < 5) reasons.push('tiny_size(' + Math.round(rect.width) + 'x' + Math.round(rect.height) + ')');
                        if (parseFloat(style.width) === 0 || parseFloat(style.height) === 0) reasons.push('zero_css_size');
                        if (parseFloat(style.opacity) === 0) reasons.push('opacity:0');
                        if (style.position === 'absolute' || style.position === 'fixed') {
                            if (rect.left < -100 || rect.top < -100) reasons.push('off_screen(left:' + Math.round(rect.left) + ',top:' + Math.round(rect.top) + ')');
                        }
                        if (el.getAttribute('aria-hidden') === 'true') reasons.push('aria-hidden');
                        const vw = window.innerWidth;
                        const vh = window.innerHeight;
                        if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) {
                            reasons.push('outside_viewport');
                        }
                        const trapWords = ['honeypot', 'hp_', '_hp', 'trap', 'ohnohoney', 'sweet_honey', 'bot_field', 'verify_', 'confirm_email'];
                        for (const w of trapWords) {
                            if (cls.includes(w) || id.includes(w) || name.includes(w)) {
                                reasons.push('trap_keyword(' + w + ')');
                                break;
                            }
                        }
                        if (el.getAttribute('tabindex') === '-1' && (style.display === 'none' || style.visibility === 'hidden')) {
                            reasons.push('tabindex_minus1_hidden');
                        }
                    } catch(e) {}
                    return reasons;
                }

                function suggestDataType(el) {
                    const type = (el.type || '').toLowerCase();
                    const name = (el.name || '').toLowerCase();
                    const id = (el.id || '').toLowerCase();
                    const placeholder = (el.placeholder || '').toLowerCase();
                    const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
                    const inputmode = (el.getAttribute('inputmode') || '').toLowerCase();
                    const pattern = el.getAttribute('pattern') || '';
                    const tagName = el.tagName.toLowerCase();
                    if (tagName === 'textarea') return 'long_text';
                    if (type === 'email' || autocomplete.includes('email') || name.includes('email') || id.includes('email')) return 'email';
                    if (type === 'tel' || inputmode === 'tel' || autocomplete.includes('tel') || name.includes('phone') || name.includes('tel') || id.includes('phone')) return 'phone';
                    if (type === 'password' || name.includes('password') || name.includes('pass') || id.includes('password')) return 'password';
                    if (type === 'number' || inputmode === 'numeric' || inputmode === 'decimal') return 'number';
                    if (type === 'url' || inputmode === 'url' || name.includes('website') || name.includes('url')) return 'url';
                    if (type === 'date' || inputmode === 'date' || name.includes('date') || name.includes('dob') || name.includes('birthday')) return 'date';
                    if (type === 'checkbox') return 'boolean';
                    if (type === 'radio') return 'choice';
                    if (tagName === 'select') return 'choice';
                    if (type === 'file') return 'file';
                    if (type === 'hidden') return 'hidden';
                    if (autocomplete.includes('name') || name.includes('name') || name.includes('first') || name.includes('last') || id.includes('name')) return 'name';
                    if (autocomplete.includes('address') || name.includes('address') || name.includes('street') || name.includes('city') || name.includes('zip') || name.includes('postal')) return 'address';
                    if (autocomplete.includes('country') || name.includes('country')) return 'country';
                    if (placeholder.includes('@')) return 'email';
                    if (pattern.includes('d') || pattern.includes('[0-9]')) return 'numeric_pattern';
                    return 'text';
                }

                function getLabelText(el) {
                    if (el.id) {
                        const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        if (label) return label.textContent.trim().substring(0, 100);
                    }
                    let parent = el.parentElement;
                    while (parent && parent !== form && parent !== document.body) {
                        if (parent.tagName.toLowerCase() === 'label') return parent.textContent.trim().substring(0, 100);
                        parent = parent.parentElement;
                    }
                    const ariaLabel = el.getAttribute('aria-label');
                    if (ariaLabel) return ariaLabel.substring(0, 100);
                    const labelledBy = el.getAttribute('aria-labelledby');
                    if (labelledBy) {
                        const refEl = document.getElementById(labelledBy);
                        if (refEl) return refEl.textContent.trim().substring(0, 100);
                    }
                    return el.placeholder || '';
                }

                function buildSelector(el) {
                    if (el.id) return '#' + CSS.escape(el.id);
                    if (el.name) {
                        const tag = el.tagName.toLowerCase();
                        const type = el.type ? '[type="' + el.type + '"]' : '';
                        return tag + '[name="' + el.name + '"]' + type;
                    }
                    const parts = [];
                    let current = el;
                    while (current && current !== document.body) {
                        let selector = current.tagName.toLowerCase();
                        if (current.id) {
                            selector = '#' + CSS.escape(current.id);
                            parts.unshift(selector);
                            break;
                        }
                        const parent = current.parentElement;
                        if (parent) {
                            const siblings = Array.from(parent.children).filter(c => c.tagName === current.tagName);
                            if (siblings.length > 1) {
                                const idx = siblings.indexOf(current) + 1;
                                selector += ':nth-of-type(' + idx + ')';
                            }
                        }
                        parts.unshift(selector);
                        current = parent;
                    }
                    return parts.join(' > ');
                }

                const selectors = 'input, textarea, select, button[type="submit"], input[type="submit"]';
                const elements = form.querySelectorAll(selectors);
                const radioGroups = {};

                for (const el of elements) {
                    const tag = el.tagName.toLowerCase();
                    const type = (el.type || '').toLowerCase();
                    if (tag === 'button' && type === 'submit') {
                        result.submitButtons.push({ selector: buildSelector(el), text: el.textContent.trim().substring(0, 50), type: 'button_submit' });
                        continue;
                    }
                    if (type === 'submit') {
                        result.submitButtons.push({ selector: buildSelector(el), value: el.value || '', type: 'input_submit' });
                        continue;
                    }
                    const trapReasons = isBotTrap(el);
                    const isTrap = trapReasons.length > 0;
                    const fieldData = {
                        tag: tag, type: type || 'text', name: el.name || '', id: el.id || '',
                        selector: buildSelector(el), label: getLabelText(el), placeholder: el.placeholder || '',
                        required: el.required || el.getAttribute('aria-required') === 'true',
                        disabled: el.disabled, readonly: el.readOnly, dataType: suggestDataType(el),
                        defaultValue: el.value || '', visible: !isTrap, botTrap: isTrap, trapReasons: trapReasons,
                    };
                    if (tag === 'select') {
                        fieldData.options = Array.from(el.options).map(opt => ({ value: opt.value, text: opt.text.substring(0, 80), selected: opt.selected }));
                    }
                    if (type === 'radio') {
                        if (!radioGroups[el.name]) radioGroups[el.name] = { options: [] };
                        radioGroups[el.name].options.push({ value: el.value, label: getLabelText(el), checked: el.checked, selector: buildSelector(el) });
                        if (radioGroups[el.name].options.length === 1) {
                            fieldData.radioGroup = el.name;
                            fieldData.options = radioGroups[el.name].options;
                            result.fields.push(fieldData);
                        } else {
                            const existing = result.fields.find(f => f.radioGroup === el.name);
                            if (existing) existing.options = radioGroups[el.name].options;
                        }
                        continue;
                    }
                    result.fields.push(fieldData);
                }

                const allButtons = form.querySelectorAll('button, [role="button"], a[href="#"]');
                const submitKeywords = ['submit', 'send', 'save', 'register', 'login', 'sign in', 'sign up', 'continue', 'next', 'apply', 'order', 'confirm'];
                for (const btn of allButtons) {
                    const text = (btn.textContent || '').trim().toLowerCase();
                    if (submitKeywords.some(kw => text.includes(kw))) {
                        const sel = buildSelector(btn);
                        if (!result.submitButtons.find(b => b.selector === sel)) {
                            result.submitButtons.push({ selector: sel, text: btn.textContent.trim().substring(0, 50), type: 'button_inferred' });
                        }
                    }
                }
                return result;
            }
            """
            data = await self.page.evaluate(js_code, form_selector)
            if not data or data.get('error'):
                return f"❌ Form not found: {data.get('error', 'unknown') if data else 'null response'}"
            lines = [f"📋 Form Analysis: {form_selector}\n"]
            info = data.get('formInfo', {})
            if info.get('action'):
                lines.append(f"Action: {info['action']} | Method: {info.get('method', 'GET')}")
            if info.get('note'):
                lines.append(f"⚠️ {info['note']}")
            lines.append("")
            fields = data.get('fields', [])
            visible_fields = [f for f in fields if f.get('visible', True)]
            trap_fields = [f for f in fields if f.get('botTrap', False)]
            lines.append(f"Fields: {len(visible_fields)} visible, {len(trap_fields)} bot-traps detected\n")
            for f in fields:
                trap_marker = " 🚫 BOT_TRAP" if f.get('botTrap') else ""
                req_marker = " *required*" if f.get('required') else ""
                dis_marker = " [disabled]" if f.get('disabled') else ""
                ro_marker = " [readonly]" if f.get('readonly') else ""
                tag_type = f"{f.get('tag', '?')}[{f.get('type', '?')}]"
                name_id = f.get('name') or f.get('id') or '(unnamed)'
                label = f.get('label', '')
                dtype = f.get('dataType', 'text')
                selector = f.get('selector', '')
                lines.append(f"  • {tag_type} name=\"{name_id}\"{req_marker}{dis_marker}{ro_marker}{trap_marker}")
                if label:
                    lines.append(f"    Label: {label[:80]}")
                if f.get('placeholder'):
                    lines.append(f"    Placeholder: {f['placeholder'][:80]}")
                lines.append(f"    DataType: {dtype}")
                lines.append(f"    Selector: {selector}")
                if f.get('botTrap'):
                    lines.append(f"    ⚠️ Trap reasons: {', '.join(f.get('trapReasons', []))}")
                if f.get('options'):
                    opts = f['options']
                    if len(opts) <= 15:
                        for o in opts:
                            val = o.get('value', '')
                            txt = o.get('text', o.get('label', ''))
                            sel_mark = " ✓" if o.get('selected') or o.get('checked') else ""
                            lines.append(f"      - \"{val}\" ({txt}){sel_mark}")
                    else:
                        lines.append(f"      [{len(opts)} options]")
                if f.get('defaultValue'):
                    lines.append(f"    Default: {f['defaultValue'][:50]}")
                lines.append("")
            buttons = data.get('submitButtons', [])
            if buttons:
                lines.append(f"Submit buttons ({len(buttons)}):")
                for b in buttons:
                    lines.append(f"  • [{b.get('type', '?')}] {b.get('text', b.get('value', ''))} → {b.get('selector', '')}")
            result_text = "\n".join(lines)
            if len(result_text) > 15000:
                result_text = result_text[:15000] + "\n... [truncated]"
            return result_text
        except Exception as e:
            logger.error(f"Analyze form error: {e}", exc_info=True)
            return f"❌ Form analysis error: {str(e)}"

    async def fill_form(self, form_selector: str, data_json: str) -> str:
        """Fills a form intelligently: skips bot traps, waits for dynamic fields, uses correct input methods per type."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            import json as _json
            try:
                fill_data = _json.loads(data_json)
            except _json.JSONDecodeError as je:
                return f"❌ Invalid JSON data: {je}"
            if not isinstance(fill_data, dict):
                return "❌ data_json must be a JSON object like {\"field_name\": \"value\"}"

            js_analyze = """
            (formSelector) => {
                let form = document.querySelector(formSelector) || document.querySelector('[role="form"]') || document.body;
                const fields = [];
                const elements = form.querySelectorAll('input, textarea, select');
                for (const el of elements) {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const cls = (el.className || '').toString().toLowerCase();
                    const name = (el.name || '').toLowerCase();
                    const id = (el.id || '').toLowerCase();
                    let isTrap = false;
                    if (style.display === 'none' || style.visibility === 'hidden') isTrap = true;
                    if (rect.width < 5 && rect.height < 5) isTrap = true;
                    if (parseFloat(style.opacity) === 0) isTrap = true;
                    if (rect.left < -100 || rect.top < -100) isTrap = true;
                    const trapWords = ['honeypot', 'hp_', 'trap', 'ohnohoney', 'bot_field'];
                    if (trapWords.some(w => cls.includes(w) || name.includes(w) || id.includes(w))) isTrap = true;
                    if (el.type === 'hidden') isTrap = false;
                    let selector = '';
                    if (el.id) selector = '#' + CSS.escape(el.id);
                    else if (el.name) selector = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                    else {
                        const parts = [];
                        let cur = el;
                        while (cur && cur !== document.body) {
                            let s = cur.tagName.toLowerCase();
                            if (cur.id) { s = '#' + CSS.escape(cur.id); parts.unshift(s); break; }
                            const p = cur.parentElement;
                            if (p) {
                                const sib = Array.from(p.children).filter(c => c.tagName === cur.tagName);
                                if (sib.length > 1) s += ':nth-of-type(' + (sib.indexOf(cur)+1) + ')';
                            }
                            parts.unshift(s);
                            cur = p;
                        }
                        selector = parts.join(' > ');
                    }
                    fields.push({
                        name: el.name || '', id: el.id || '', type: (el.type || el.tagName.toLowerCase()).toLowerCase(),
                        tag: el.tagName.toLowerCase(), selector: selector, isTrap: isTrap,
                        placeholder: (el.placeholder || '').toLowerCase(), label: ''
                    });
                    if (el.id) {
                        const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        if (lbl) fields[fields.length-1].label = lbl.textContent.trim().toLowerCase();
                    }
                }
                return fields;
            }
            """
            fields = []
            for attempt in range(5):
                fields = await self.page.evaluate(js_analyze, form_selector)
                matched = 0
                for key in fill_data.keys():
                    key_lower = key.lower()
                    for f in fields:
                        if (key_lower == f['name'].lower() or key_lower == f['id'].lower() or
                            key_lower in f['placeholder'] or key_lower in f['label']):
                            matched += 1
                            break
                if matched >= len(fill_data) * 0.5 or attempt == 4:
                    break
                await asyncio.sleep(2)

            results = []
            skipped_traps = []
            not_found = []
            import random

            for key, value in fill_data.items():
                key_lower = key.lower()
                value_str = str(value)
                matched_field = None
                for f in fields:
                    if (key_lower == f['name'].lower() or key_lower == f['id'].lower() or
                        key_lower in f['placeholder'] or key_lower in f['label']):
                        matched_field = f
                        break
                if not matched_field:
                    not_found.append(key)
                    continue
                if matched_field['isTrap']:
                    skipped_traps.append(f"{key} (bot trap: {matched_field['selector']})")
                    continue
                selector = matched_field['selector']
                ftype = matched_field['type']
                ftag = matched_field['tag']
                try:
                    await self.page.evaluate(f"document.querySelector('{selector}')?.scrollIntoView({{block:'center',behavior:'instant'}})")
                    await asyncio.sleep(0.3)
                    if ftag == 'select':
                        await self.page.select_option(selector, value_str)
                        results.append(f"✅ {key}: selected '{value_str}'")
                    elif ftype == 'checkbox':
                        is_checked = await self.page.evaluate(f"document.querySelector('{selector}').checked")
                        want_checked = value_str.lower() in ('true', '1', 'yes', 'on', 'check')
                        if is_checked != want_checked:
                            await self.page.click(selector)
                        results.append(f"✅ {key}: checkbox={'checked' if want_checked else 'unchecked'}")
                    elif ftype == 'radio':
                        radio_sel = f'{matched_field["tag"]}[name="{matched_field["name"]}"][value="{value_str}"]'
                        try:
                            await self.page.click(radio_sel)
                            results.append(f"✅ {key}: radio='{value_str}'")
                        except Exception:
                            await self.page.click(selector)
                            results.append(f"⚠️ {key}: radio clicked default (value match failed)")
                    elif ftag in ('input', 'textarea'):
                        await self.page.click(selector)
                        await asyncio.sleep(0.2)
                        await self.page.keyboard.press("Control+a")
                        await asyncio.sleep(0.05)
                        await self.page.keyboard.press("Backspace")
                        await asyncio.sleep(0.05)
                        for char in value_str:
                            await self.page.keyboard.type(char, delay=random.uniform(30, 100))
                        results.append(f"✅ {key}: filled ({len(value_str)} chars)")
                    else:
                        await self.page.fill(selector, value_str)
                        results.append(f"✅ {key}: filled (generic)")
                except Exception as fe:
                    results.append(f"❌ {key}: error - {str(fe)[:100]}")

            report = [f"📝 Form Fill Report ({form_selector}):\n"]
            report.append(f"Filled: {len([r for r in results if r.startswith('✅')])}/{len(fill_data)}")
            if results:
                report.append("\nResults:")
                report.extend(f"  {r}" for r in results)
            if skipped_traps:
                report.append(f"\n🚫 Skipped bot traps ({len(skipped_traps)}):")
                report.extend(f"  • {s}" for s in skipped_traps)
            if not_found:
                report.append(f"\n⚠️ Fields not found ({len(not_found)}):")
                report.extend(f"  • {n}" for n in not_found)
            return "\n".join(report)
        except Exception as e:
            logger.error(f"Fill form error: {e}", exc_info=True)
            return f"❌ Form fill error: {str(e)}"

    async def submit_form(self, form_selector: str, submit_selector: str = "") -> str:
        """Submits a form by clicking the submit button or pressing Enter."""
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")
        try:
            import random
            if submit_selector:
                await self.page.wait_for_selector(submit_selector, state="visible", timeout=10000)
                await self.page.evaluate(f"document.querySelector('{submit_selector}')?.scrollIntoView({{block:'center',behavior:'smooth'}})")
                await asyncio.sleep(0.5)
                await self.page.click(submit_selector)
                await asyncio.sleep(1)
                return f"✅ Form submitted via: {submit_selector}"

            js_find_submit = """
            (formSelector) => {
                let form = document.querySelector(formSelector) || document.querySelector('[role="form"]') || document.body;
                let btn = form.querySelector('input[type="submit"], button[type="submit"]');
                if (btn) {
                    const rect = btn.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) return { found: true, selector: btn.id ? '#' + CSS.escape(btn.id) : null, text: btn.textContent || btn.value || '' };
                }
                const keywords = ['submit', 'send', 'save', 'register', 'login', 'sign in', 'sign up', 'continue', 'next', 'apply', 'confirm', 'отправить', 'войти', 'сохранить'];
                const buttons = form.querySelectorAll('button, [role="button"], input[type="button"]');
                for (const b of buttons) {
                    const text = (b.textContent || b.value || '').trim().toLowerCase();
                    if (keywords.some(kw => text.includes(kw))) {
                        const rect = b.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            let sel = b.id ? '#' + CSS.escape(b.id) : '';
                            if (!sel && b.name) sel = b.tagName.toLowerCase() + '[name="' + b.name + '"]';
                            return { found: true, selector: sel, text: text };
                        }
                    }
                }
                return { found: false };
            }
            """
            result = await self.page.evaluate(js_find_submit, form_selector)
            if result.get('found') and result.get('selector'):
                sel = result['selector']
                await self.page.wait_for_selector(sel, state="visible", timeout=10000)
                await self.page.evaluate(f"document.querySelector('{sel}')?.scrollIntoView({{block:'center',behavior:'smooth'}})")
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await self.page.click(sel)
                await asyncio.sleep(1)
                return f"✅ Form submitted via auto-detected button: '{result.get('text', '')}' ({sel})"

            js_last_input = """
            (formSelector) => {
                let form = document.querySelector(formSelector) || document.body;
                const inputs = Array.from(form.querySelectorAll('input:not([type="hidden"]):not([type="submit"]), textarea, select'));
                for (let i = inputs.length - 1; i >= 0; i--) {
                    const rect = inputs[i].getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        return inputs[i].id ? '#' + CSS.escape(inputs[i].id) : inputs[i].tagName.toLowerCase() + '[name="' + (inputs[i].name || '') + '"]';
                    }
                }
                return null;
            }
            """
            last_input = await self.page.evaluate(js_last_input, form_selector)
            if last_input:
                await self.page.click(last_input)
                await asyncio.sleep(0.3)
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1)
                return "✅ Form submitted via Enter key on last input"

            await self.page.evaluate(f"document.querySelector('{form_selector}')?.submit()")
            await asyncio.sleep(1)
            return "⚠️ Form submitted via JS .submit() (may bypass validation)"
        except Exception as e:
            logger.error(f"Submit form error: {e}", exc_info=True)
            return f"❌ Form submit error: {str(e)}"


manager = BrowserManager()


# ================================================================
# MCP TOOLS REGISTRATION
# ================================================================

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
    """Executes JavaScript in the browser and returns the result. Use for getting/setting values, checking login state, or interacting with page APIs. Example: 'document.title' or 'localStorage.getItem("token")'"""
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

# --- NEW: Scroll ---

@mcp.tool()
async def browser_scroll(direction: str = "down", amount: int = 500) -> str:
    """Scrolls the page up or down by N pixels. direction: 'up' or 'down'. amount: pixels (default 500). Use 'up_a_bit'/'down_a_bit' for small scrolls (100px)."""
    if direction in ("up_a_bit", "down_a_bit"):
        amount = 100
        direction = direction.replace("_a_bit", "")
    return await manager.scroll(direction, amount)

@mcp.tool()
async def browser_scroll_to_element(selector: str, full: bool = True) -> str:
    """Scrolls the page so that the element matching the CSS selector is visible. full=True: element is fully in viewport. full=False: element is partially visible (top edge at least)."""
    return await manager.scroll_to_element(selector, full)

@mcp.tool()
async def browser_scroll_to_top() -> str:
    """Scrolls the page to the very top."""
    return await manager.scroll_to_top()

@mcp.tool()
async def browser_scroll_to_bottom() -> str:
    """Scrolls the page to the very bottom (useful for lazy-loaded content)."""
    return await manager.scroll_to_bottom()

# --- NEW: Links ---

@mcp.tool()
async def browser_extract_all_links() -> str:
    """Extracts ALL links on the current page with text, URL, and type (internal/external). Use for site-wide navigation. Returns up to 100 internal + 50 external links."""
    return await manager.extract_all_links()

@mcp.tool()
async def browser_find_link(query: str) -> str:
    """Finds links on the page matching a substring in their text or URL. Returns all matches (up to 30). Use for quick navigation to a specific page."""
    return await manager.find_link(query)

# --- NEW: OCR / Captcha (DISABLED - detection only) ---

@mcp.tool()
async def browser_ocr_image(source: str) -> str:
    """⚠️ DISABLED: OCR functionality is temporarily disabled due to accuracy issues. Please solve captchas manually."""
    return "⚠️ OCR/Captcha recognition is currently DISABLED. If you encounter a captcha, please solve it manually in the browser window and then continue with form submission."

@mcp.tool()
async def browser_read_captcha(selector: str = "") -> str:
    """⚠️ DISABLED: Captcha auto-solving is temporarily disabled. Detects captcha presence and instructs user to solve manually."""
    if not manager.is_running or not manager.page:
        return "❌ Browser not started."
    try:
        js_detect = """
        () => {
            const patterns = [
                'img[src*="captcha"]', 'img[src*="recaptcha"]', 'img[alt*="captcha"]',
                '#captcha', '.captcha', '[class*="captcha"]', '[id*="captcha"]',
                '.g-recaptcha', '#g-recaptcha-response', '[data-sitekey]',
                '.h-captcha', '#hcaptcha', '[data-hcaptcha-widget-id]',
                'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
                '.cf-challenge', '#challenge-running', '.turnstile'
            ];
            for (const sel of patterns) {
                const el = document.querySelector(sel);
                if (el) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        return { found: true, selector: sel, tag: el.tagName };
                    }
                }
            }
            return { found: false };
        }
        """
        result = await manager.page.evaluate(js_detect)
        if result.get('found'):
            return f"🔒 CAPTCHA DETECTED ({result.get('selector', 'unknown')}). Automatic solving is DISABLED. Please solve the captcha MANUALLY in the browser window, then call browser_submit_form() to continue."
        return "✅ No captcha detected on this page."
    except Exception as e:
        logger.error(f"Captcha detection error: {e}")
        return f"⚠️ Could not check for captcha: {str(e)}. If you see a captcha, please solve it manually."

# --- NEW: Form Analysis, Smart Fill, Submit ---

@mcp.tool()
async def browser_analyze_form(form_selector: str) -> str:
    """Analyzes a form and returns all fields with metadata: type, selector, label, visibility, required status, data type suggestion, bot-trap detection, and submit buttons. Use this BEFORE filling a form to understand its structure. form_selector: CSS selector for the <form> element (e.g. '#login-form', '.contact-form', 'form'). Returns detailed field analysis."""
    return await manager.analyze_form(form_selector)

@mcp.tool()
async def browser_fill_form(form_selector: str, data_json: str) -> str:
    """Fills a form intelligently using JSON data. Automatically skips bot-trap/honeypot fields, waits for dynamically loaded fields (up to 10s), matches keys by name/id/placeholder/label, uses correct input method per field type (human-like typing for text, select_option for dropdowns, click for checkboxes/radios). data_json: JSON string like '{"email": "user@test.com", "password": "secret", "country": "US"}'. Returns detailed fill report."""
    return await manager.fill_form(form_selector, data_json)

@mcp.tool()
async def browser_submit_form(form_selector: str, submit_selector: str = "") -> str:
    """Submits a form by clicking the submit button. If submit_selector is empty, auto-detects the submit button by type='submit' or by common keywords (Submit, Send, Login, Register, etc). Falls back to pressing Enter in the last visible input, or JS form.submit(). form_selector: CSS selector for the form. submit_selector: optional CSS selector for specific submit button."""
    return await manager.submit_form(form_selector, submit_selector)


if __name__ == "__main__":
    logger.info("="*50)
    logger.info("Server starting (Clean Camoufox Mode + OCR/Scroll/Links/Forms)...")
    logger.info("="*50)
    try:
        mcp.run(transport="stdio")
    except Exception as e:
        logger.critical(f"CRASH: {e}", exc_info=True)
        raise