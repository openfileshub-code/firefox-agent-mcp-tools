"""
Firefox Agent MCP Server - Main entry point.

Run with: python server.py

Architecture: modular. Each feature group lives in modules/<name>.py.
To disable a feature group, set its ENABLE_* flag to False below.
Logging can be toggled via ENABLE_LOGGING and LOG_LEVEL.
"""

import sys
import os
import io
import json
import logging
import platform
import asyncio
import re
import random
from typing import Any, Dict, List, Optional

# ================================================================
# FEATURE FLAGS - Toggle groups on/off
# ================================================================
ENABLE_VIDEO_CONTROL = True
ENABLE_FORM_TOOLS = True
ENABLE_SMART_NAVIGATION = True
ENABLE_SCROLL_TOOLS = True
ENABLE_LINK_TOOLS = True
ENABLE_WINDOW_MANAGER = True
ENABLE_SCREENSHOT = True
ENABLE_JS_EVALUATE = True
ENABLE_TAB_TOOLS = True

# ================================================================
# LOGGING CONFIGURATION
# ================================================================
ENABLE_LOGGING = True
LOG_LEVEL = logging.DEBUG  # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FILE_NAME = "mcp_debug.log"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, LOG_FILE_NAME)

# Preserve original stdio for MCP JSON-RPC transport
_original_stdout = sys.stdout
_original_stderr = sys.stderr
_builtin_print = print


def _setup_logging() -> logging.Logger:
    """Configure file-only logging. Stdout/stderr reserved for MCP."""
    root_logger = logging.getLogger()
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)

    if ENABLE_LOGGING:
        fh = logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8')
        fh.setLevel(LOG_LEVEL)
        fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(funcName)s:%(lineno)d - %(message)s')
        fh.setFormatter(fmt)
        root_logger.addHandler(fh)
        root_logger.setLevel(LOG_LEVEL)

    # Intercept print() to prevent stdout pollution during MCP stdio transport
    def safe_print(*args: Any, **kwargs: Any) -> None:
        kwargs.pop('file', None)
        msg = ' '.join(str(a) for a in args)
        root_logger.info(f"PRINT_INTERCEPTED: {msg}")

    import builtins
    builtins.print = safe_print

    return logging.getLogger("FirefoxMCP")


logger = _setup_logging()

# ================================================================
# IMPORTS
# ================================================================
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("MCP library not installed. Run: pip install mcp[cli]")
    sys.exit(1)

mcp = FastMCP("FirefoxMCP")
logger.info("FastMCP instance created.")

_AsyncCamoufox = None
try:
    from camoufox.async_api import AsyncCamoufox as _AC
    _AsyncCamoufox = _AC
    logger.info("Camoufox imported successfully.")
except Exception as e:
    logger.critical(f"Failed to import AsyncCamoufox: {e}")
    sys.stdout = _original_stdout
    sys.stderr = _original_stderr
    _builtin_print(f"❌ FATAL: Could not initialize Camoufox. Error: {e}", file=sys.stderr)
    sys.exit(1)

# Import modules
from modules.link_cache import LinkCache
if ENABLE_VIDEO_CONTROL:
    from modules.video_controller import VideoController
if ENABLE_WINDOW_MANAGER:
    from modules.window_manager import WindowManager

# Load sites config
_sites_path = os.path.join(BASE_DIR, "config", "sites.json")
try:
    with open(_sites_path, "r", encoding="utf-8") as f:
        SITES_CONFIG = json.load(f)
except Exception:
    SITES_CONFIG = {"aliases": {"youtube": "https://www.youtube.com", "twitch": "https://www.twitch.tv"}}


# ================================================================
# UTILITY FUNCTIONS
# ================================================================
async def cleanup_stale_browser_processes(profile_dir: str) -> None:
    """
    Универсальная безопасная очистка.
    Убивает ТОЛЬКО процессы, которые используют нашу папку профиля.
    Не трогает системный Firefox и чужие экземпляры Camoufox.
    """
    lock_file = os.path.join(profile_dir, "parent.lock")
    
    # Шаг 1: Быстрая проверка — есть ли вообще проблема
    if not os.path.exists(lock_file):
        logger.debug("No lock file found — no cleanup needed.")
        return
    
    # Шаг 2: Пробуем удалить лок. Если получилось — процесс уже мёртв
    try:
        os.remove(lock_file)
        logger.debug("Lock file removed successfully — no stale process.")
        return
    except PermissionError:
        logger.warning("Lock file is held by a running process. Searching for it...")
    except Exception as e:
        logger.debug(f"Lock removal failed: {e}")
    
    # Шаг 3: Ищем процесс, который использует наш профиль (только Windows)
    try:
        # Экранируем путь для PowerShell
        profile_path_escaped = profile_dir.replace("\\", "\\\\")
        
        ps_command = (
            "Get-WmiObject Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{profile_path_escaped}*' }} | "
            "Select-Object ProcessId, Name | "
            "ConvertTo-Json"
        )
        
        process = await asyncio.create_subprocess_exec(
            "powershell", "-Command", ps_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10.0)
        
        killed_count = 0
        if stdout:
            try:
                import json as _json
                processes = _json.loads(stdout.decode("utf-8", errors="ignore"))
                if isinstance(processes, dict):
                    processes = [processes]
                
                for proc in processes:
                    pid = proc.get("ProcessId")
                    name = proc.get("Name", "unknown")
                    if pid:
                        logger.info(f"Killing stale process: {name} (PID: {pid})")
                        killer = await asyncio.create_subprocess_exec(
                            "taskkill", "/F", "/PID", str(pid), "/T",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await killer.wait()
                        killed_count += 1
            except Exception as parse_err:
                logger.debug(f"PowerShell output parse error: {parse_err}")
        
        if killed_count > 0:
            await asyncio.sleep(1.0)
        
        # Шаг 4: Повторная попытка удалить локи
        remove_lock_files(profile_dir)
        logger.info(f"Cleanup complete. Killed {killed_count} stale process(es).")
        
    except asyncio.TimeoutError:
        logger.warning("PowerShell search timed out. Falling back...")
        await _fallback_cleanup()
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}. Falling back...")
        await _fallback_cleanup()


async def _fallback_cleanup() -> None:
    """
    Запасной вариант: если PowerShell не сработал,
    убиваем ТОЛЬКО camoufox.exe (не firefox.exe!).
    """
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/F", "/IM", "camoufox.exe", "/T",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
        await asyncio.sleep(0.5)
        logger.info("Fallback cleanup: camoufox.exe killed.")
    except Exception as e:
        logger.debug(f"Fallback cleanup skipped: {e}")


def remove_lock_files(profile_dir: str) -> None:
    """Remove lock files from profile directory."""
    if not os.path.exists(profile_dir):
        return
    for lf in ["parent.lock", "lock", ".parentlock"]:
        path = os.path.join(profile_dir, lf)
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"Removed lock file: {path}")
            except Exception as e:
                logger.warning(f"Could not remove {path}: {e}")


def _looks_like_url(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False
    if re.match(r"^https?://", s, re.I):
        return True
    if re.match(r"^www\.", s, re.I):
        return True
    if " " not in s and re.match(r"^[\w.-]+\.[a-z]{2,}(?:/|$)", s, re.I):
        return True
    return False


# Placeholder patterns that indicate hallucinated/template URLs
_PLACEHOLDER_PATTERNS = re.compile(
    r'(video_id|your[_-]?id|example[_-]?id|put[_-]?id[_-]?here|'
    r'\{[a-z_]+\}|<[^>]+>|REPLACE[_-]?ME|YOUR[_-]?)',
    re.I
)


def _validate_url(url: str) -> Optional[str]:
    """
    Validates URL before navigation. Returns error message if invalid, None if OK.
    Prevents agent from navigating to placeholder URLs like '?v=video_id'.
    """
    if not url or not url.strip():
        return "❌ Empty URL."
    if _PLACEHOLDER_PATTERNS.search(url):
        return (
            f"❌ BLOCKED: URL contains placeholder/template value: '{url}'. "
            "Do NOT use template variables like 'video_id', '{id}', 'YOUR_ID'. "
            "Use browser_find_link() or browser_navigate_smart() to find the actual link on the page first."
        )
    if not re.match(r"^https?://", url, re.I) and not url.startswith("/"):
        return f"❌ Invalid URL scheme: '{url}'. Must start with http:// or https://"
    return None


def _make_fingerprint(state: Dict[str, Any]) -> str:
    return f"{state.get('url', '')}|{state.get('title', '')}|{state.get('src', '')}"


# ================================================================
# BROWSER MANAGER
# ================================================================
class BrowserManager:
    """Central browser lifecycle and interaction manager."""

    def __init__(self) -> None:
        self._camoufox_ctx: Any = None
        self.context: Any = None
        self._pages: List[Any] = []
        self._active_page_index: int = 0
        self._lock = asyncio.Lock()
        self.is_running: bool = False
        self.engine: str = "none"
        self._keepalive_task: Optional[asyncio.Task] = None
        self.profile_dir: str = os.path.join(BASE_DIR, "playwright_profile")
        stale_lock = os.path.join(self.profile_dir, "parent.lock")
        if os.path.exists(stale_lock):
            logger.warning(f"⚠️ Stale lock file detected at startup: {stale_lock}")
            logger.warning("Will attempt cleanup on first browser_start() call.")
        self.link_cache = LinkCache()
        self.last_video_fingerprint: Optional[str] = None

        if ENABLE_VIDEO_CONTROL:
            self.vc = VideoController()
        else:
            self.vc = None

        if ENABLE_WINDOW_MANAGER:
            self.wm = WindowManager(os.path.join(BASE_DIR, "config", "window.json"))
        else:
            self.wm = None

        self.sites_config = SITES_CONFIG

    # --- Lifecycle ---

    async def start(self, headless: bool = False) -> str:
        if self.is_running:
            return "Browser is already running."
        try:
            logger.info("Starting browser...")
            os.makedirs(self.profile_dir, exist_ok=True)
            await cleanup_stale_browser_processes(self.profile_dir)

            actual_headless = False
            if headless:
                logger.warning("Camoufox headless=True crashes on Windows. Forcing False.")

            kwargs: Dict[str, Any] = dict(
                headless=actual_headless,
                os="windows",
                humanize=True,
                geoip=False,
                persistent_context=True,
                user_data_dir=self.profile_dir,
            )

            # Inject window args if window manager enabled
            if self.wm:
                extra_args = self.wm.get_launch_args()
                if extra_args:
                    kwargs["args"] = extra_args
                viewport = self.wm.get_viewport()
                if viewport:
                    kwargs.setdefault("viewport", viewport)

            self._camoufox_ctx = _AsyncCamoufox(**kwargs)
            self.context = await asyncio.wait_for(self._camoufox_ctx.__aenter__(), timeout=60.0)

            pages = self.context.pages
            self._pages = list(pages) if pages else []
            if not self._pages:
                self._pages.append(await self.context.new_page())
            self._active_page_index = 0
            for p in self._pages:
                p.on("close", lambda p=p: self._on_page_closed(p))
            self._setup_page_tracking(self.context)
            self.is_running = True
            self.engine = "camoufox"

            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

            logger.info(f"Browser started. Profile: {self.profile_dir}")
            return f"✅ Camoufox started.\nProfile: {self.profile_dir}\nHeadless: {actual_headless}"
        except asyncio.TimeoutError:
            logger.error("Start timeout after 60s.")
            await self.stop()
            raise RuntimeError("Failed to start Camoufox: timeout after 60s.")
        except Exception as e:
            logger.error(f"Start failed: {e}", exc_info=True)
            await self.stop()
            raise RuntimeError(f"Failed to start Camoufox: {e}")

    async def _keepalive_loop(self) -> None:
        try:
            for _ in range(12):
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
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        try:
            if self._camoufox_ctx:
                await self._camoufox_ctx.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"Error during stop: {e}")
        finally:
            self._camoufox_ctx = None
            self.context = None
            self._pages = []
            self._active_page_index = 0
            self.is_running = False
            self.engine = "none"
            logger.info("Browser stopped.")
        return "🛑 Browser stopped."

    def _require_page(self) -> None:
        if not self.is_running or not self.page:
            raise RuntimeError("Browser not started.")

    @property
    def page(self) -> Optional[Any]:
        if not self._pages:
            return None
        return self._pages[self._active_page_index]

    @property
    def tab_count(self) -> int:
        return len(self._pages)

    async def _settle(self, timeout_ms: int = 2000) -> None:
        if not self.page:
            return
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass

    async def _safe_evaluate(self, expression: str, arg: Any = None, timeout: float = 15.0) -> Any:
        if not self.page:
            raise RuntimeError("Browser not started.")
        try:
            if arg is None:
                return await asyncio.wait_for(self.page.evaluate(expression), timeout=timeout)
            return await asyncio.wait_for(self.page.evaluate(expression, arg), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("JS evaluation timed out")

    def _on_page_closed(self, page: Any) -> None:
        try:
            self._pages = [p for p in self._pages if p != page]
            if self._active_page_index >= len(self._pages):
                self._active_page_index = max(0, len(self._pages) - 1)
        except Exception:
            pass

    def _setup_page_tracking(self, context: Any) -> None:
        def on_page_created(page: Any) -> None:
            self._pages.append(page)
            page.on("close", lambda p=page: self._on_page_closed(p))

        if hasattr(context, "on"):
            try:
                context.on("page", on_page_created)
            except Exception:
                pass


# ================================================================
# MCP TOOLS REGISTRATION
# ================================================================
browser_manager = BrowserManager()


@mcp.tool()
async def browser_start(headless: bool = False) -> str:
    """Start the browser."""
    try:
        return await browser_manager.start(headless=headless)
    except Exception as e:
        return f"❌ Failed to start browser: {e}"


@mcp.tool()
async def browser_stop() -> str:
    """Stop the browser."""
    try:
        return await browser_manager.stop()
    except Exception as e:
        return f"❌ Failed to stop browser: {e}"


@mcp.tool()
async def browser_navigate(url: str) -> str:
    """Navigate to a URL."""
    try:
        browser_manager._require_page()
        error = _validate_url(url)
        if error:
            return error
        
        await browser_manager.page.goto(url)
        await browser_manager._settle()
        browser_manager.link_cache.mark_dirty()
        return f"✅ Navigated to: {url}"
    except Exception as e:
        return f"❌ Navigation error: {e}"


@mcp.tool()
async def browser_get_content() -> str:
    """Get page content."""
    try:
        browser_manager._require_page()
        content = await browser_manager.page.content()
        return f"✅ Page content:\n{content[:5000]}..." if len(content) > 5000 else content
    except Exception as e:
        return f"❌ Get content error: {e}"


# Start MCP server
if __name__ == "__main__":
    logger.info("Starting FirefoxMCP server...")
    mcp.run(transport="stdio")