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
        # L4 fix: append mode + size-based rotation instead of wiping the log
        # on every restart.
        try:
            from logging.handlers import RotatingFileHandler
            fh: logging.Handler = RotatingFileHandler(
                LOG_FILE, mode='a', maxBytes=2 * 1024 * 1024,
                backupCount=3, encoding='utf-8',
            )
        except Exception:
            fh = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
        fh.setLevel(LOG_LEVEL)
        fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(funcName)s:%(lineno)d - %(message)s')
        fh.setFormatter(fmt)
        root_logger.addHandler(fh)
        root_logger.setLevel(LOG_LEVEL)

    # M5 fix: DO NOT patch builtins.print globally — that suppressed normal
    # output of mcp/camoufox/playwright and hindered debugging. Instead we
    # expose a module-level `log_print` and this module's code uses it.
    def safe_print(*args: Any, **kwargs: Any) -> None:
        kwargs.pop('file', None)
        msg = ' '.join(str(a) for a in args)
        root_logger.info(f"PRINT: {msg}")

    return logging.getLogger("FirefoxMCP")


logger = _setup_logging()


# Module-local replacement for print(): stdout is reserved for the MCP stdio
# transport, so our own prints go to the log file. Third-party libraries keep
# the real builtins.print untouched (M5 fix — no global patching).
def _module_print(*args: Any, **kwargs: Any) -> None:
    kwargs.pop('file', None)
    logger.info("PRINT: " + ' '.join(str(a) for a in args))


print = _module_print  # noqa: A001 (intentional module-level shadow of builtins.print)

# ================================================================
# IMPORTS
# ================================================================
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error("MCP library not installed. Run: pip install mcp[cli]")
    sys.exit(1)

# ================================================================
# SESSION / KEEP-ALIVE POLICY
# ---------------------------------------------------------------
# By default the browser lifecycle is fully DECOUPLED from MCP-session
# (chat) activity: when the chat client closes the stdio session, the
# Camoufox process keeps running and its profile lock file stays in place.
# The next server start detects the orphaned browser via the lock file and
# reuses it if it is still alive (see _adopt_or_cleanup_orphan()), otherwise
# cleans it up before launching a fresh instance.
#
# Opt-in behaviour (env FIREFOX_MCP_SHUTDOWN_ON_SESSION_END=1): stop the
# browser automatically when the MCP session ends (e.g. chat idle timeout
# kills the server process). Off by default so long pauses in chat never
# terminate an actively playing video.
#
# IMPORTANT: closing the stdio session is not the only way the browser can
# die. The MCP client may also SIGTERM/SIGKILL this server process after an
# idle timeout, and Camoufox/Playwright registers its own atexit + signal
# handlers that tear the browser down on ANY normal interpreter exit. So a
# truly decoupled lifecycle needs two more guards (see below):
#   • _detach_browser_on_exit(): removes Playwright's atexit teardown hooks;
#   • _install_keepalive_signal_guard(): turns SIGINT/SIGTERM into a no-op
#     while the browser is running, so the window survives process death.
# ================================================================
SHUTDOWN_ON_SESSION_END = os.environ.get(
    "FIREFOX_MCP_SHUTDOWN_ON_SESSION_END", "0"
).strip().lower() in ("1", "true", "yes", "on")

# Env override for the keep-alive signal guard (set to 0 to restore normal
# Ctrl+C / service-stop semantics, e.g. for supervised deployments).
KEEPALIVE_SIGNAL_GUARD = os.environ.get(
    "FIREFOX_MCP_KEEPALIVE_SIGNAL_GUARD", "1"
).strip().lower() in ("1", "true", "yes", "on")


from contextlib import asynccontextmanager


def _detach_browser_on_exit() -> None:
    """Prevent Playwright's built-in atexit teardown from closing the browser.

    playwright._impl._driver registers an `_Exiter` that runs `Driver.stop()`
    (kills the node driver → closes Camoufox) on ANY normal interpreter exit,
    including the graceful end of `mcp.run()` after a chat disconnect. That
    hook is the main reason the window dies "a few minutes after the last
    chat message". Removing it keeps the browser alive; our own code always
    stops it explicitly via browser_stop()/stop() when asked.

    Implementation: wrap the exiter's `run_once` so any callback whose
    function originates from playwright/camoufox/playwright._repo_error is
    skipped at shutdown. Robust across playwright versions (no reliance on
    private list layout).
    """
    try:
        import playwright._impl._driver as _pw_driver  # noqa: PLC0415
    except Exception:
        return
    for attr in ("_exit_sync", "_exit_async"):
        exiter = getattr(_pw_driver, attr, None)
        if exiter is None or getattr(exiter, "_fm_detached", False):
            continue
        original_run_once = getattr(exiter, "run_once", None)
        if original_run_once is None:
            continue

        def _patched(cb: Any, *args: Any, _orig: Any = original_run_once, **kw: Any) -> None:
            module = getattr(cb, "__module__", "") or ""
            if module.startswith(("playwright", "camoufox")):
                logger.debug("Skipped playwright/camoufox atexit teardown (keep-alive).")
                return
            _orig(cb, *args, **kw)

        exiter.run_once = _patched
        exiter._fm_detached = True  # idempotency marker
        logger.debug(f"Neutralised Playwright atexit exiter ({attr}).")


import signal as _signal  # noqa: E402


def _make_camoufox_exit_noop_unless_intentional(mgr: "BrowserManager") -> None:
    """Guard against accidental Camoufox teardown while keep-alive is active.

    Two layers, because dunder lookup goes through the TYPE (instance
    attributes are invisible to `async with`):
      1. instance attribute — protects direct calls like
         `await ctx.__aexit__(...)` from our own code paths;
      2. per-instance subclass installed at launch (`_AsyncCamoufox` →
         generated wrapper class overriding __aexit__) — protects the
         `async with AsyncCamoufox(...)` pattern used by any external
         teardown path.
    Both only really close the browser when `mgr._intentional_stop` is True
    (set exclusively by BrowserManager.stop(), i.e. user-requested
    browser_stop()). Everything else becomes a logged no-op and the window
    stays open.
    """
    ctx = mgr._camoufox_ctx
    if ctx is None or getattr(ctx, "_fm_keepalive_wrapped", False):
        return
    original_aexit = type(ctx).__aexit__

    async def _guarded(*exc_info: Any) -> Any:
        # Bound to this instance only — no `self` param needed; the closure
        # consults mgr._intentional_stop at call time.
        if mgr._intentional_stop:
            return await original_aexit(ctx, *exc_info)
        logger.info(
            "Camoufox __aexit__ suppressed (keep-alive policy): browser stays "
            "open. Use browser_stop() to close it deliberately."
        )
        return None

    # Layer 1: instance attribute (direct-call protection). A plain function
    # in the instance dict is NOT treated as a descriptor by instance lookup,
    # so `await ctx.__aexit__(None, None, None)` calls us with just exc_info.
    try:
        ctx.__aexit__ = _guarded
    except (AttributeError, TypeError):
        pass
    # Layer 2: per-instance subclass (async-with / dunder protection). The
    # class-level slot IS a coroutine function accessed via the descriptor
    # protocol, hence the explicit self and the alias below.
    async def _guarded_cls(self: Any, *exc_info: Any) -> Any:
        return await _guarded(*exc_info)

    try:
        base = type(ctx)
        wrapper = type(f"FmKeepalive{base.__name__}", (base,), {"__aexit__": _guarded_cls})
        ctx.__class__ = wrapper
    except TypeError:
        # Some C-extension classes forbid __class__ reassignment; layer 1 +
        # the Playwright-exiter detach still cover the realistic paths.
        logger.debug("Could not install keepalive subclass; relying on instance-level guard.")
    ctx._fm_keepalive_wrapped = True


def _install_keepalive_signal_guard() -> None:
    """While a browser is running, SIGINT/SIGTERM must NOT tear it down.

    Chat hosts (Claude Desktop, Cursor, etc.) routinely kill the MCP server
    process after an idle timeout. Default Python behaviour + Playwright's
    own SIGTERM handler would then close the browser window mid-video. With
    the decoupled policy (default), receiving SIGINT/SIGTERM means: log it,
    leave the browser alive, and exit the process quietly. browser_stop()
    remains the only supported way to close the window.

    Disable with FIREFOX_MCP_KEEPALIVE_SIGNAL_GUARD=0 for supervised setups.
    """
    if not KEEPALIVE_SIGNAL_GUARD:
        return

    def _guard(signum: int, _frame: Any) -> None:
        name = _signal.Signals(signum).name
        if browser_manager.is_running and not SHUTDOWN_ON_SESSION_END:
            logger.info(
                f"{name} received while browser is running — ignoring "
                "(keep-alive policy). Browser stays open; use browser_stop()."
            )
            # Exit WITHOUT touching the browser: os._exit skips atexit
            # handlers (incl. Playwright teardown); the guarded __aexit__
            # would suppress it anyway. Flush streams first so the log line
            # reaches disk.
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)
        # No browser (or user opted into shutdown-on-exit): behave normally.
        _signal.signal(signum, _signal.SIG_DFL)
        _signal.raise_signal(signum)

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            _signal.signal(sig, _guard)
        except (ValueError, OSError, AttributeError):
            # Not in the main thread / unsupported on this build — skip.
            pass


@asynccontextmanager
async def _server_lifespan(_app: Any):
    """FastMCP lifespan hook — runs per connected session (stdio)."""
    _detach_browser_on_exit()
    yield
    # Reached when the MCP session ends: stdio EOF (client closed/quit),
    # transport error, or server shutdown.
    try:
        if SHUTDOWN_ON_SESSION_END and browser_manager.is_running:
            logger.info("MCP session ended — stopping browser (opt-in policy).")
            await browser_manager.stop()
        elif browser_manager.is_running:
            logger.info(
                "MCP session ended — browser kept alive (decoupled policy). "
                "Next server start will reuse or clean up this instance."
            )
    except Exception as e:  # never let cleanup crash the shutdown path
        logger.warning(f"Session-end browser handling error: {e}")


mcp = FastMCP("FirefoxMCP", lifespan=_server_lifespan)
logger.info(
    f"FastMCP instance created. "
    f"shutdown_on_session_end={SHUTDOWN_ON_SESSION_END}"
)

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
def _platform_os_for_camoufox() -> str:
    """Map current platform to Camoufox `os=` spoofing value."""
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        return "windows"
    if sysname == "darwin":
        return "macos"
    return "linux"


def _headless_unsafe_platform() -> bool:
    """Camoufox headless is known to crash on Windows only; POSIX is fine."""
    return platform.system().lower().startswith("win")


def _profile_process_pids(profile_dir: str) -> List[int]:
    """Return PIDs of running processes whose command line references our
    profile dir. Never matches system Firefox or foreign Camoufox profiles."""
    pids: List[int] = []
    try:
        if _headless_unsafe_platform():
            # Windows: CIM query via PowerShell (same filter as cleanup).
            pass  # handled by caller-side async path; sync adoption only POSIX
        else:
            me = os.getpid()
            for pid_dir in os.listdir("/proc"):
                if not pid_dir.isdigit():
                    continue
                pid = int(pid_dir)
                if pid == me:
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as fh:
                        cmdline = fh.read().replace(b"\x00", b" ").decode(
                            "utf-8", "replace")
                except (OSError, PermissionError):
                    continue
                if profile_dir in cmdline and (
                        "camoufox" in cmdline.lower() or "firefox" in cmdline.lower()):
                    pids.append(pid)
    except FileNotFoundError:
        pass  # /proc missing (non-Linux POSIX) — treat as "no info"
    except Exception as e:
        logger.debug(f"PID scan error: {e}")
    return pids


async def _orphan_windows_alive(profile_dir: str) -> bool:
    """Windows: ask CIM which live processes reference our profile dir."""
    try:
        profile_path_escaped = profile_dir.replace("\\", "\\\\")
        ps_command = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{profile_path_escaped}*' }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", ps_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        pids = [int(t) for t in stdout.decode(errors="replace").split() if t.strip().isdigit()]
        if pids:
            logger.info(f"Orphan browser alive on Windows (PIDs {pids}).")
            return True
    except Exception as e:
        logger.debug(f"Windows orphan-alive probe failed: {e}")
    return False


async def _orphan_browser_alive(profile_dir: str) -> bool:
    """True if a still-running browser process owns this profile right now.

    Used by the decoupled-lifecycle policy: an alive orphan is NOT killed
    at startup (it may be playing video the user watches); a dead one has
    its stale lock removed so a fresh browser can launch.
    """
    lock_file = os.path.join(profile_dir, "parent.lock")
    if not os.path.exists(lock_file):
        return False

    if _headless_unsafe_platform():
        # On Windows we must NOT delete parent.lock while the owning
        # process is alive (Firefox would then allow a second instance on
        # the same profile). Probe liveness first.
        if await _orphan_windows_alive(profile_dir):
            return True
        remove_lock_files(profile_dir)
        return False

    try:
        os.remove(lock_file)
        # Lock was stale → the owning process is dead. Caller will launch
        # a fresh browser (which re-creates the lock itself).
        logger.debug("Lock file was stale — orphan process is gone.")
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    # Lock held by someone — verify that holder actually exists.
    pids = _profile_process_pids(profile_dir)
    if pids:
        logger.info(f"Orphan browser alive (PIDs {pids}) holding our profile.")
        return True
    # POSIX: lock held but no matching process found — race or permission
    # issue; be conservative and let the new launch handle ProfileInUse.
    logger.warning("Lock held but no matching process found; assuming alive.")
    return True


async def cleanup_stale_browser_processes(profile_dir: str) -> None:
    """
    Универсальная безопасная очистка / Universal safe cleanup.
    Убивает ТОЛЬКО процессы, которые используют нашу папку профиля.
    Не трогает системный Firefox и чужие экземпляры Camoufox.

    Cross-platform: PowerShell/taskkill on Windows, ps/proc scan + SIGTERM
    on POSIX. Never touches processes whose command line lacks our profile dir.
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

    if _headless_unsafe_platform():
        await _cleanup_windows(profile_dir)
    else:
        await _cleanup_posix(profile_dir)

    # Шаг 4: Повторная попытка удалить локи
    remove_lock_files(profile_dir)


async def _cleanup_windows(profile_dir: str) -> None:
    """Windows branch: find processes by command line via PowerShell."""
    try:
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
                processes = json.loads(stdout.decode("utf-8", errors="ignore"))
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
        logger.info(f"Cleanup complete. Killed {killed_count} stale process(es).")

    except asyncio.TimeoutError:
        logger.warning("PowerShell search timed out. Falling back...")
        await _fallback_cleanup_windows()
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}. Falling back...")
        await _fallback_cleanup_windows()


async def _fallback_cleanup_windows() -> None:
    """
    Запасной вариант (Windows): убиваем ТОЛЬКО camoufox.exe (не firefox.exe!).
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


async def _cleanup_posix(profile_dir: str) -> None:
    """
    POSIX branch: scan /proc (Linux) or `ps` output for processes whose
    command line references OUR profile dir; terminate them politely, then kill.
    Never matches unrelated processes.
    """
    targets: List[int] = []
    needle = os.path.abspath(profile_dir)

    # Preferred: /proc scan (Linux)
    proc_root = "/proc"
    scanned_proc = False
    if os.path.isdir(proc_root):
        scanned_proc = True
        try:
            for entry in os.listdir(proc_root):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                if pid == os.getpid():
                    continue
                cmdline_path = os.path.join(proc_root, entry, "cmdline")
                try:
                    with open(cmdline_path, "rb") as f:
                        cmdline = f.read().decode("utf-8", errors="ignore").replace("\x00", " ")
                except Exception:
                    continue
                if needle in cmdline and ("camoufox" in cmdline.lower() or "firefox" in cmdline.lower()):
                    targets.append(pid)
        except Exception as e:
            logger.debug(f"/proc scan failed: {e}")
            scanned_proc = False

    # Fallback: ps util (macOS or minimal Linux)
    if not scanned_proc:
        try:
            ps = await asyncio.create_subprocess_exec(
                "ps", "-eo", "pid,args",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(ps.communicate(), timeout=10.0)
            for line in (stdout or b"").decode("utf-8", errors="ignore").splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    continue
                pid, args = int(parts[0]), parts[1]
                if pid == os.getpid():
                    continue
                if needle in args and ("camoufox" in args.lower() or "firefox" in args.lower()):
                    targets.append(pid)
        except Exception as e:
            logger.warning(f"POSIX process scan failed: {e}")
            return

    if not targets:
        logger.debug("POSIX cleanup: no stale processes referencing our profile.")
        return

    for pid in targets:
        logger.info(f"Terminating stale browser process PID {pid} (profile-scoped)")
        try:
            os.kill(pid, 15)  # SIGTERM
        except (ProcessLookupError, PermissionError):
            continue

    await asyncio.sleep(1.0)

    for pid in targets:  # escalate survivors to SIGKILL
        try:
            os.kill(pid, 0)  # still alive?
            os.kill(pid, 9)  # SIGKILL
            logger.info(f"SIGKILL sent to PID {pid}")
        except (ProcessLookupError, PermissionError):
            pass


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
    if url.startswith("/"):
        # Relative paths only make sense when already on a page (origin context).
        page = browser_manager.page
        current = getattr(page, "url", "") if page else ""
        if not current or current == "about:blank":
            return (
                f"❌ Cannot navigate to relative path '{url}': no page is open yet. "
                "Navigate to a full http(s):// URL first."
            )
        return None
    if not re.match(r"^https?://", url, re.I):
        return f"❌ Invalid URL scheme: '{url}'. Must start with http:// or https://"
    return None


# JS control-flow patterns that indicate an attempt to drive a media player
# directly — forbidden by SKILL.md rule #3 (only video_action may do this).
_JS_VIDEO_CONTROL_PATTERNS = re.compile(
    r"(querySelector(?:All)?\s*\(\s*['\"]video|\.play\s*\(\s*\)|\.pause\s*\(\s*\)"
    r"|currentTime\s*=|setPlaybackQuality|playbackRate\s*=|textTracks|requestFullscreen"
    r"|exitFullscreen|webkitRequestPresentationMode)",
    re.I,
)


def _check_js_video_control(script: str) -> Optional[str]:
    """Return an error message if the script tries to control a video player."""
    m = _JS_VIDEO_CONTROL_PATTERNS.search(script or "")
    if m:
        return (
            f"❌ BLOCKED: browser_evaluate must not control video players "
            f"(matched `{m.group(0)[:60]}`). Per skill rules, use video_action(action, param) "
            "instead — it handles state verification and fingerprint checks."
        )
    return None


def _make_fingerprint(state: Dict[str, Any]) -> str:
    return f"{state.get('url', '')}|{state.get('title', '')}|{state.get('src', '')}"


def _format_link_results(results: List[Dict[str, Any]], limit: int = 15) -> str:
    """Format LinkCache search results as a numbered list for the agent."""
    if not results:
        return "⚠️ No elements found matching the query. Try a shorter substring."
    lines = []
    for i, r in enumerate(results[:limit], 1):
        href = r.get("href") or "(no href — JS element, use browser_click_text)"
        kind = r.get("type", "link")
        mt = r.get("match_type", "?")
        text = (r.get("text") or "")[:120]
        lines.append(f"{i}. [{kind}/{mt}] {text!r} → {href}")
    more = f"\n... ({len(results)} total, showing top {min(limit, len(results))})" \
        if len(results) > limit else ""
    return "\n".join(lines) + more


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
        # Keep-alive lifecycle flags (see SESSION / KEEP-ALIVE POLICY above):
        # _intentional_stop is set only by stop(); the guarded __aexit__ and
        # the signal handler consult it to decide whether closing the browser
        # is user-requested or an accidental teardown we must suppress.
        # Default False: while a browser is up, any stray teardown path
        # (session end, __aexit__, SIGTERM) must NOT close it. start() flips
        # it to True only on launch-failure cleanup; stop() flips it to True
        # right before the deliberate real close.
        self._intentional_stop: bool = False
        # Set when the user closes the browser window manually — lets the
        # keepalive loop notice and reset our state instead of reporting a
        # dead page on the next tool call.
        self._user_closed_event: asyncio.Event = asyncio.Event()
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
            # Orphan-aware startup (decoupled browser lifecycle):
            # if a previous chat session ended but its Camoufox is still
            # alive and holding our profile, do NOT kill it — report the
            # situation so the user can keep watching / close it manually.
            if await _orphan_browser_alive(self.profile_dir):
                return (
                    "⚠️ A browser from the previous session is still running "
                    "and holds this profile (video may still be playing).\n"
                    "Options:\n"
                    "  • Keep it: just continue using that window — no tool "
                    "call needed.\n"
                    "  • Replace it: call browser_stop() (kills the orphan "
                    "process safely), then browser_start() again."
                )
            await cleanup_stale_browser_processes(self.profile_dir)

            # Headless is only unsafe on Windows (known Camoufox crash there);
            # honour the caller's request on POSIX platforms.
            actual_headless = bool(headless)
            if headless and _headless_unsafe_platform():
                logger.warning("Camoufox headless=True crashes on Windows. Forcing False.")
                actual_headless = False

            kwargs: Dict[str, Any] = dict(
                headless=actual_headless,
                os=_platform_os_for_camoufox(),
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
            # Belt-and-braces: Camoufox's own context-manager __exit__ also
            # stops the browser; wrap it so a stray call (or an MCP-session
            # teardown path that reaches it) cannot close the window while
            # the decoupled keep-alive policy is active. Explicit
            # browser_stop() sets _intentional_stop first and closes for real.
            if not SHUTDOWN_ON_SESSION_END:
                _make_camoufox_exit_noop_unless_intentional(self)
            try:
                self.context = await asyncio.wait_for(
                    self._camoufox_ctx.__aenter__(), timeout=60.0)
            except BaseException:
                # Launch failed/timed out — always allow real teardown now.
                self._intentional_stop = True
                raise

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
            self._user_closed_event.clear()
            _install_keepalive_signal_guard()
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
        """Watchdog for the decoupled browser lifecycle.

        The browser must survive arbitrary chat inactivity, so this loop has
        exactly ONE job: detect that the browser is really gone (user closed
        the window / process crashed) and reset our state so the next tool
        call gives a clean "not started" answer instead of acting on a dead
        page. It NEVER closes or restarts the browser by itself.
        """
        try:
            while self.is_running:
                # Wait up to 300s; early wake-up if the user closed a page.
                try:
                    await asyncio.wait_for(
                        self._user_closed_event.wait(), timeout=300.0)
                except asyncio.TimeoutError:
                    pass
                if not self.is_running:
                    break
                if self.context is None:
                    logger.info("Keepalive: context vanished — resetting state.")
                    self._mark_dead()
                    break
                # Did the browser process go away? (connection closed event
                # fires when Playwright loses the browser.)
                if getattr(self.context, "_browser_closed", False):
                    logger.info("Keepalive: browser connection closed — resetting state.")
                    self._mark_dead()
                    break
                # Cheap liveness probe on the active page.
                page = self.page
                if page is not None:
                    try:
                        await asyncio.wait_for(
                            page.evaluate("() => document.readyState"), timeout=10.0)
                    except Exception as e:
                        logger.debug(f"Keepalive probe failed: {e}")
                        if not self._pages_alive():
                            logger.info("Keepalive: no live pages left — resetting state.")
                            self._mark_dead()
                            break
                # Reset the flag only after a successful sweep so a close
                # event during the probe is handled on the next iteration.
                self._user_closed_event.clear()
        except asyncio.CancelledError:
            pass

    def _pages_alive(self) -> bool:
        for p in list(self._pages):
            try:
                if not p.is_closed():
                    return True
            except Exception:
                continue
        return False

    def _mark_dead(self) -> None:
        """Browser disappeared externally — drop our handles WITHOUT trying
        to tear anything down (there is nothing left to tear down)."""
        self._camoufox_ctx = None
        self.context = None
        self._pages = []
        self._active_page_index = 0
        self.is_running = False
        self.engine = "none"
        self.last_video_fingerprint = None

    async def stop(self) -> str:
        if not self.is_running and not self._camoufox_ctx:
            # No in-process browser — but an orphan from a previous (chat)
            # session may still hold our profile. Kill ONLY processes that
            # reference this exact profile dir (never foreign browsers).
            if await _orphan_browser_alive(self.profile_dir):
                await cleanup_stale_browser_processes(self.profile_dir)
                return ("🛑 Orphan browser from a previous session stopped "
                        "(profile released).")
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
                # This is the ONLY path allowed to really close the browser:
                # flip the flag so the guarded __aexit__ passes through.
                self._intentional_stop = True
                await self._camoufox_ctx.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"Error during stop: {e}")
        finally:
            self._intentional_stop = True
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
            # No pages left → the user probably closed the whole window.
            # Wake the keepalive loop so it can confirm and reset state.
            if not self._pages:
                self._user_closed_event.set()
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

    # --- Tab management (backing for browser_*_tab tools) ---

    async def new_tab(self, url: Optional[str] = None) -> str:
        self._require_page()
        page = await self.context.new_page()
        # _setup_page_tracking also appends via the 'page' event; guard against
        # double-append races by checking membership.
        if page not in self._pages:
            self._pages.append(page)
            page.on("close", lambda p=page: self._on_page_closed(p))
        self._active_page_index = self._pages.index(page)
        self.link_cache.mark_dirty()
        if url:
            error = _validate_url(url)
            if error:
                return error
            await page.goto(url, timeout=DEFAULT_TIMEOUT_MS)
            await self._settle()
            return f"✅ New tab #{self._active_page_index} opened at: {url}"
        return f"✅ New empty tab #{self._active_page_index} created and activated."

    async def close_tab(self, index: int) -> str:
        self._require_page()
        if index < 0 or index >= len(self._pages):
            return f"❌ Invalid tab index {index}. Valid: 0..{len(self._pages) - 1}"
        page = self._pages[index]
        try:
            await page.close()
        except Exception as e:
            return f"❌ Close tab error: {e}"
        self._on_page_closed(page)
        self.link_cache.mark_dirty()
        return f"🛑 Tab #{index} closed. Active tab: #{self._active_page_index}."

    def list_tabs(self) -> str:
        if not self.is_running:
            return "Browser is not running."
        lines = []
        for i, p in enumerate(self._pages):
            marker = " ← active" if i == self._active_page_index else ""
            try:
                title = p.title()
            except Exception:
                title = "?"
            lines.append(f"{i}. {title!r} [{getattr(p, 'url', '?')}]{marker}")
        return "\n".join(lines) if lines else "No open tabs."

    async def switch_tab(self, index: int) -> str:
        self._require_page()
        if index < 0 or index >= len(self._pages):
            return f"❌ Invalid tab index {index}. Valid: 0..{len(self._pages) - 1}"
        self._active_page_index = index
        self.link_cache.mark_dirty()
        page = self._pages[index]
        try:
            await page.bring_to_front()
        except Exception:
            pass
        return f"✅ Switched to tab #{index}: {getattr(page, 'url', '')}"


# ================================================================
# MCP TOOLS REGISTRATION
# ================================================================
DEFAULT_TIMEOUT_MS = 30000
# L5 fix: named constant instead of magic number; truncation is announced.
MAX_CONTENT_CHARS = 5000

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
    """Navigate to a URL. ONLY for complete, verified URLs — placeholders are blocked by server."""
    try:
        browser_manager._require_page()
        error = _validate_url(url)
        if error:
            return error

        await browser_manager.page.goto(url, timeout=DEFAULT_TIMEOUT_MS)
        await browser_manager._settle()
        browser_manager.link_cache.mark_dirty()
        return f"✅ Navigated to: {url}"
    except Exception as e:
        return f"❌ Navigation error: {e}"


@mcp.tool()
async def browser_get_content() -> str:
    """Get page content (HTML). Truncated to MAX_CONTENT_CHARS with a notice."""
    try:
        browser_manager._require_page()
        content = await browser_manager.page.content()
        if len(content) > MAX_CONTENT_CHARS:
            return (
                f"✅ Page content (TRUNCATED: showing {MAX_CONTENT_CHARS} of "
                f"{len(content)} chars — use browser_evaluate or "
                f"browser_search_on_page to inspect the rest):\n"
                f"{content[:MAX_CONTENT_CHARS]}..."
            )
        return f"✅ Page content:\n{content}"
    except Exception as e:
        return f"❌ Get content error: {e}"


if ENABLE_SMART_NAVIGATION and ENABLE_LINK_TOOLS:

    @mcp.tool()
    async def browser_open(target: str) -> str:
        """Opens sites by URL, alias (e.g. 'ютуб'), or link text on current page.
        NEVER construct search URLs manually — use this or browser_search_on_page."""
        try:
            browser_manager._require_page()
            t = (target or "").strip()
            if not t:
                return "❌ Empty target."

            # 1) direct URL / bare domain
            if _looks_like_url(t):
                url = t if re.match(r"^https?://", t, re.I) else "https://" + t
                error = _validate_url(url)
                if error:
                    return error
                await browser_manager.page.goto(url, timeout=DEFAULT_TIMEOUT_MS)
                await browser_manager._settle()
                browser_manager.link_cache.mark_dirty()
                return f"✅ Opened: {url}"

            # 2) alias from sites config
            aliases = browser_manager.sites_config.get("aliases", {})
            key = t.lower()
            if key in aliases:
                url = aliases[key]
                await browser_manager.page.goto(url, timeout=DEFAULT_TIMEOUT_MS)
                await browser_manager._settle()
                browser_manager.link_cache.mark_dirty()
                return f"✅ Opened alias '{t}' → {url}"

            # 3) link/element text on current page (fuzzy)
            await browser_manager.link_cache.ensure_fresh(browser_manager.page)
            results = browser_manager.link_cache.search(t)
            hrefs = [r for r in results if r.get("href")]
            if len(hrefs) == 1:
                url = hrefs[0]["href"]
                await browser_manager.page.goto(url, timeout=DEFAULT_TIMEOUT_MS)
                await browser_manager._settle()
                browser_manager.link_cache.mark_dirty()
                return f"✅ Opened page link: {url}"
            if hrefs:
                return "Multiple matches, pick one:\n" + _format_link_results(hrefs)
            return ("⚠️ No element found matching target. Try browser_find_link() "
                    "with a shorter substring.")
        except Exception as e:
            return f"❌ browser_open error: {e}"

    @mcp.tool()
    async def browser_search_on_page(query: str) -> str:
        """Types query into the page's native search box and presses Enter.
        Does NOT navigate away or construct search URLs. Use INSTEAD of manual search URLs."""
        try:
            browser_manager._require_page()
            if not query or not query.strip():
                return "❌ Empty query."
            js_find_box = """
            () => {
                const selectors = [
                    'input[name="search_query"]', 'input[id="search-box"]',
                    'yt-search-box input#search-input', 'input[title="Search"]',
                    'input[aria-label*="earch"]', 'input[name="q"]',
                    'input[type="search"]'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        el.scrollIntoView({block: 'center'});
                        el.focus();
                        return true;
                    }
                }
                return false;
            }
            """
            found = await browser_manager._safe_evaluate(js_find_box)
            if not found:
                return ("⚠️ No visible search input found on this page. "
                        "Open the site first (browser_open) or use browser_navigate with a full URL.")
            # Type via keyboard so site handlers (YouTube suggestions etc.) fire natively
            await browser_manager.page.keyboard.type(query, delay=30)
            await asyncio.sleep(0.3)
            await browser_manager.page.keyboard.press("Enter")
            await browser_manager._settle(timeout_ms=5000)
            browser_manager.link_cache.mark_dirty()
            return f"✅ Search submitted on-page: {query!r}. Current URL: {browser_manager.page.url}"
        except Exception as e:
            return f"❌ browser_search_on_page error: {e}"

    @mcp.tool()
    async def browser_find_link(query: str) -> str:
        """Finds links, tabs, buttons, and interactive elements by substring/fuzzy match.
        Works with JS tabs and aria-labels. Returns numbered list, does NOT click."""
        try:
            browser_manager._require_page()
            await browser_manager.link_cache.ensure_fresh(browser_manager.page)
            results = browser_manager.link_cache.search(query)
            if not results:
                return "⚠️ No elements found matching the query. Try a shorter substring."
            return (f"Found {len(results)} element(s) (ranked):\n"
                    + _format_link_results(results))
        except Exception as e:
            return f"❌ browser_find_link error: {e}"

    @mcp.tool()
    async def browser_navigate_smart(query: str) -> str:
        """Fuzzy search over page links. Supports abbreviated queries.
        1 match → navigate. Multiple → list for user choice."""
        try:
            browser_manager._require_page()
            await browser_manager.link_cache.ensure_fresh(browser_manager.page)
            results = [r for r in browser_manager.link_cache.search(query) if r.get("href")]
            if not results:
                return ("⚠️ No matching links found. Try a shorter query or "
                        "browser_find_link() to inspect candidates.")
            if len(results) == 1 or (
                len(results) > 1
                and results[0]["match_type"] in ("exact", "contains", "token_subset")
                and results[0]["score"] < results[1]["score"]
            ):
                url = results[0]["href"]
                await browser_manager.page.goto(url, timeout=DEFAULT_TIMEOUT_MS)
                await browser_manager._settle()
                browser_manager.link_cache.mark_dirty()
                return f"✅ Navigated to best match: {results[0]['text'][:80]!r} → {url}"
            return ("Multiple comparable matches, pick one:\n"
                    + _format_link_results(results))
        except Exception as e:
            return f"❌ browser_navigate_smart error: {e}"

    @mcp.tool()
    async def browser_click_text(text_query: str, tag: str = "*") -> str:
        """Finds and clicks ANY visible element (button, tab, link, icon) by partial
        text/aria-label match. Case-insensitive, ignores punctuation.
        Returns numbered list if multiple matches (click nothing)."""
        try:
            browser_manager._require_page()
            if not text_query or not text_query.strip():
                return "❌ Empty text query."
            q_norm = LinkCache._normalize(text_query)
            q_words = LinkCache._get_meaningful_words(text_query)
            tag_sel = (tag or "*").strip() or "*"

            js_collect = """
            (sel) => {
                const out = [];
                const isVisible = (el) => {
                    try {
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    } catch(e) { return false; }
                };
                const base = sel === '*'
                    ? document.querySelectorAll('a, button, [role="button"], [role="tab"], [onclick], yt-tab-shape, summary')
                    : document.querySelectorAll(sel + ', ' + sel + ' *');
                let idx = 0;
                base.forEach(el => {
                    if (!isVisible(el)) return;
                    const text = ((el.textContent || '').trim() + ' | ' +
                        (el.getAttribute('aria-label') || '') + ' | ' +
                        (el.getAttribute('title') || '')).replace(/\\s+/g, ' ').substring(0, 200);
                    if (!text.trim() || text.trim() === '| |') return;
                    el.setAttribute('data-mcp-click-id', String(idx));
                    out.push({idx: idx, text: text});
                    idx++;
                });
                return out;
            }
            """
            elements = await browser_manager._safe_evaluate(js_collect, tag_sel)
            scored = []
            for el in elements or []:
                t_norm = LinkCache._normalize(el["text"])
                if q_norm and q_norm in t_norm:
                    scored.append((abs(len(t_norm) - len(q_norm)), el))
                elif q_words:
                    t_words = LinkCache._get_meaningful_words(el["text"])
                    if q_words.issubset(t_words):
                        scored.append((1000 + len(t_words) - len(q_words), el))
            scored.sort(key=lambda x: x[0])
            matches = [el for _, el in scored[:10]]

            if not matches:
                return "⚠️ No element found matching text. Try a shorter substring."
            if len(scored) > 1 and scored[0][0] == scored[1][0]:
                listing = "\n".join(f"{i+1}. {m['text'][:100]!r}" for i, m in enumerate(matches))
                return f"Multiple equal matches, refine query:\n{listing}"

            chosen = matches[0]
            js_click = """
            (id) => {
                const el = document.querySelector('[data-mcp-click-id="' + id + '"]');
                if (!el) return 'gone';
                el.scrollIntoView({block: 'center'});
                el.click();
                return 'clicked';
            }
            """
            result = await browser_manager._safe_evaluate(js_click, str(chosen["idx"]))
            browser_manager.link_cache.mark_dirty()
            if result != "clicked":
                return "⚠️ Element disappeared before click. Re-run browser_find_link()."
            note = ""
            if len(matches) > 1:
                others = "\n".join(f"  {i+2}. {m['text'][:80]!r}" for i, m in enumerate(matches[1:5]))
                note = f"\nOther candidates (not clicked):\n{others}"
            return f"✅ Clicked: {chosen['text'][:100]!r}{note}"
        except Exception as e:
            return f"❌ browser_click_text error: {e}"


@mcp.tool()
async def browser_snapshot() -> str:
    """Returns JSON: URL, title, player info, video state, fingerprint.
    MANDATORY before any video action series."""
    try:
        browser_manager._require_page()
        snapshot = await browser_manager.vc.get_snapshot(browser_manager.page) \
            if browser_manager.vc else {}
        player_info = await browser_manager.vc.get_player_info(browser_manager.page) \
            if browser_manager.vc else {}
        data = {"url": snapshot.get("url"), "title": snapshot.get("title"),
                "video": snapshot, "player": player_info,
                "fingerprint": snapshot.get("fingerprint")}
        browser_manager.last_video_fingerprint = snapshot.get("fingerprint")
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"❌ Snapshot error: {e}"


if ENABLE_VIDEO_CONTROL and browser_manager.vc is not None:

    async def _video_fingerprint_gate() -> Optional[str]:
        """C4: compare current video fingerprint against the last one seen.
        Returns warning string when the video changed mid-series, else None.
        Also refreshes the stored fingerprint."""
        snap = await browser_manager.vc.get_snapshot(browser_manager.page)
        fp = snap.get("fingerprint", "")
        prev = browser_manager.last_video_fingerprint
        browser_manager.last_video_fingerprint = fp
        if prev is not None and snap.get("has_video") and fp != prev:
            return (f"⚠️ Page/video fingerprint changed (autoplay?). "
                    f"Previous: {prev[:100]} → Current: {fp[:100]}. "
                    "STOP the action series and notify the user (skill rule #5).")
        return None

    @mcp.tool()
    async def video_check() -> str:
        """Check video state BEFORE a series of video actions. Sets the fingerprint
        baseline that video_action() enforces during the series."""
        try:
            browser_manager._require_page()
            snapshot = await browser_manager.vc.get_snapshot(browser_manager.page)
            player_info = await browser_manager.vc.get_player_info(browser_manager.page)
            browser_manager.last_video_fingerprint = snapshot.get("fingerprint")
            return json.dumps({"video": snapshot, "player": player_info},
                              indent=2, ensure_ascii=False)
        except Exception as e:
            return f"❌ Video check error: {e}"

    @mcp.tool()
    async def video_action(action: str, param: Optional[str] = None) -> str:
        """Execute a video player action (play/pause/volume/seek/subtitles/etc.).
        FINGERPRINT GATE: if the underlying video changed since video_check(),
        returns ⚠️ and you MUST stop the series immediately (skill rule #5).
        After menu interactions call video_action('cleanup','menu')."""
        try:
            browser_manager._require_page()
            if action in ("state", "download_source", "cleanup"):
                # Read-only / housekeeping commands don't need the gate.
                return await browser_manager.vc.action(browser_manager.page, action, param)

            gate = await _video_fingerprint_gate()
            if gate:
                return gate

            # Snapshot BEFORE the mutation — used for honest verification of
            # volume actions (a site script may silently reset the slider).
            pre_snap = None
            if action.startswith(("volume", "mute")):
                try:
                    pre_snap = await browser_manager.vc.get_snapshot(
                        browser_manager.page)
                except Exception:
                    pre_snap = None

            result = await browser_manager.vc.action(browser_manager.page, action, param)

            # Verify post-state where possible and refresh baseline after mutation
            snap = await browser_manager.vc.get_snapshot(browser_manager.page)
            browser_manager.last_video_fingerprint = snap.get("fingerprint")
            if result.startswith("✅"):
                state_bits = (f" [paused={snap.get('paused')}, "
                              f"t={round(snap.get('currentTime', 0), 1)}s, "
                              f"vol={round(snap.get('volume', 1), 2)}]")
                # Honest volume verification: compare intended vs actual.
                if action.startswith("volume") and pre_snap is not None:
                    v_pre = pre_snap.get("volume", 1)
                    v_post = snap.get("volume", 1)
                    if action == "volume_set":
                        expected = 0.5
                        if param:
                            p = str(param).strip()
                            expected = (float(p[:-1]) / 100.0
                                        if p.endswith("%") else float(p))
                            expected = max(0.0, min(1.0, expected))
                        if abs(v_post - expected) > 0.03:
                            result = (f"⚠️ Volume set to {expected:.2f} was NOT applied "
                                      f"(actual={v_post:.2f}). The player UI overrode it. "
                                      "Prefer keyboard shortcuts via browser_press_key.")
                            return result + state_bits
                    elif action in ("volume_up", "volume_down"):
                        step = float(param) if param else 0.1
                        if abs(v_post - v_pre) < 0.01:
                            result = (f"⚠️ Volume did not change ({v_pre:.2f} → "
                                      f"{v_post:.2f}); the player ignored the JS API. "
                                      "Prefer keyboard shortcuts via browser_press_key.")
                            return result + state_bits
                if action == "mute" and pre_snap is not None:
                    if snap.get("muted") == pre_snap.get("muted"):
                        result = ("⚠️ Mute state unchanged — the player ignored the "
                                  "JS API. Prefer keyboard shortcut 'm'.")
                        return result + state_bits
                return result + state_bits
            return result
        except Exception as e:
            return f"❌ Video action error: {e}"


if ENABLE_SCREENSHOT:

    @mcp.tool()
    async def browser_screenshot(full_page: bool = False) -> str:
        """Save a PNG screenshot of the current page to the server's screenshots dir.
        Returns the file path."""
        try:
            browser_manager._require_page()
            shots_dir = os.path.join(BASE_DIR, "screenshots")
            os.makedirs(shots_dir, exist_ok=True)
            fname = os.path.join(shots_dir, f"shot_{int(time_module.time())}.png")
            await browser_manager.page.screenshot(path=fname, full_page=full_page)
            return f"✅ Screenshot saved: {fname}"
        except Exception as e:
            return f"❌ Screenshot error: {e}"


if ENABLE_JS_EVALUATE:

    @mcp.tool()
    async def browser_evaluate(script: str) -> str:
        """Evaluate a JS expression on the page and return its JSON result.
        FORBIDDEN for video player control — the server blocks play/pause/seek/
        fullscreen/textTracks manipulation (use video_action instead)."""
        try:
            browser_manager._require_page()
            blocked = _check_js_video_control(script)
            if blocked:
                return blocked
            result = await browser_manager._safe_evaluate(script)
            return json.dumps(result, ensure_ascii=False, default=str)[:8000]
        except Exception as e:
            return f"❌ Evaluate error: {e}"


if ENABLE_TAB_TOOLS:

    @mcp.tool()
    async def browser_new_tab(url: Optional[str] = None) -> str:
        """Open a new tab (optionally at a validated URL) and make it active."""
        try:
            return await browser_manager.new_tab(url)
        except Exception as e:
            return f"❌ New tab error: {e}"

    @mcp.tool()
    async def browser_list_tabs() -> str:
        """List open tabs with indices, titles and URLs; marks the active one."""
        try:
            return browser_manager.list_tabs()
        except Exception as e:
            return f"❌ List tabs error: {e}"

    @mcp.tool()
    async def browser_switch_tab(index: int) -> str:
        """Switch the active tab by index (see browser_list_tabs)."""
        try:
            return await browser_manager.switch_tab(index)
        except Exception as e:
            return f"❌ Switch tab error: {e}"

    @mcp.tool()
    async def browser_close_tab(index: int) -> str:
        """Close a tab by index. The next tab becomes active."""
        try:
            return await browser_manager.close_tab(index)
        except Exception as e:
            return f"❌ Close tab error: {e}"


if ENABLE_SCROLL_TOOLS:

    @mcp.tool()
    async def browser_press_key(key: str, hold_ms: int = 0) -> str:
        """Press a keyboard key on the focused page (Playwright key names:
        'm', 'ArrowUp', 'Space', 'k', 'j', 'l', 'f', 't' ...).
        This is the RELIABLE way to drive players that ignore the JS video
        API — YouTube/Twitch react to real key events (volume up/down are
        ArrowUp/ArrowDown, mute is 'm', pause is 'k'/'Space')."""
        try:
            browser_manager._require_page()
            k = (key or "").strip()
            if not k:
                return "❌ Empty key name."
            await browser_manager.page.keyboard.press(k)
            return f"✅ Key pressed: {k}"
        except Exception as e:
            return f"❌ Press key error: {e}"

    @mcp.tool()
    async def browser_scroll(direction: str = "down", amount: int = 800) -> str:
        """Scroll the page. direction: up|down|top|bottom|left|right. amount in px."""
        try:
            browser_manager._require_page()
            d = (direction or "down").lower()
            if d in ("top", "bottom"):
                js = f"() => {{ window.scrollTo(0, {'document.body.scrollHeight' if d == 'bottom' else '0'}); }}"
            elif d in ("left", "right"):
                sign = "-1" if d == "left" else "1"
                js = f"() => {{ window.scrollBy({sign} * {int(amount)}, 0); }}"
            else:
                sign = "-1" if d == "up" else "1"
                js = f"() => {{ window.scrollBy(0, {sign} * {int(amount)}); }}"
            await browser_manager._safe_evaluate(js)
            browser_manager.link_cache.mark_dirty()
            pos = await browser_manager._safe_evaluate("() => Math.round(window.scrollY)")
            return f"✅ Scrolled {d}. scrollY={pos}px"
        except Exception as e:
            return f"❌ Scroll error: {e}"


if ENABLE_FORM_TOOLS:

    @mcp.tool()
    async def browser_analyze_form(selector: str = "form") -> str:
        """Analyze form fields under selector: names, types, required, honeypots."""
        try:
            browser_manager._require_page()
            js = """
            (sel) => {
                const forms = document.querySelectorAll(sel || 'form');
                const out = [];
                forms.forEach((f, fi) => {
                    const fields = [];
                    f.querySelectorAll('input, select, textarea').forEach(el => {
                        const type = (el.type || el.tagName.toLowerCase());
                        const hidden = el.type === 'hidden'
                            || (el.offsetParent === null && type !== 'submit');
                        fields.push({
                            name: el.name || el.id || '',
                            type: type,
                            required: !!el.required,
                            label: (el.labels && el.labels[0]) ? el.labels[0].textContent.trim() : (el.getAttribute('aria-label') || ''),
                            likely_honeypot: hidden && type === 'text' && !el.required,
                        });
                    });
                    out.push({form_index: fi, action: f.getAttribute('action') || '', method: (f.method || 'get'), fields: fields});
                });
                return out;
            }
            """
            result = await browser_manager._safe_evaluate(js, selector)
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)[:8000]
        except Exception as e:
            return f"❌ Form analysis error: {e}"

    @mcp.tool()
    async def browser_fill_form(selector: str, fields_json: str) -> str:
        """Fill form fields. fields_json: {"name_or_css": "value", ...}.
        Skips honeypot (hidden decoy) fields automatically."""
        try:
            browser_manager._require_page()
            try:
                fields = json.loads(fields_json)
            except Exception:
                return "❌ fields_json is not valid JSON."
            filled, skipped = [], []
            for key, value in fields.items():
                try:
                    loc = browser_manager.page.locator(selector).locator(
                        f'[name="{key}"], [id="{key}"], [aria-label="{key}"], label:has-text("{key}") ~ *'
                    ).first
                    alt = browser_manager.page.locator(f'{selector} >> nth=0')
                    target = loc if await loc.count() > 0 else browser_manager.page.locator(key).first
                    if await target.count() == 0:
                        skipped.append(f"{key}: not found")
                        continue
                    if not await target.is_visible():
                        skipped.append(f"{key}: hidden (possible honeypot) — SKIPPED")
                        continue
                    tag_name = (await target.evaluate("el => el.tagName.toLowerCase()")) 
                    if tag_name == "select":
                        await target.select_option(label=str(value))
                    else:
                        await target.fill(str(value))
                    filled.append(key)
                except Exception as ie:
                    skipped.append(f"{key}: {ie}")
            summary = f"✅ Filled {len(filled)} field(s): {filled}"
            if skipped:
                summary += f"\n⚠️ Skipped: {skipped}"
            return summary
        except Exception as e:
            return f"❌ Fill form error: {e}"

    @mcp.tool()
    async def browser_submit_form(selector: str = "form") -> str:
        """Submit a form. NOTE: for payments/deletions you MUST get explicit user confirmation first."""
        try:
            browser_manager._require_page()
            btn = browser_manager.page.locator(
                f'{selector} button[type="submit"], {selector} input[type="submit"]'
            ).first
            if await btn.count() > 0:
                await btn.click(timeout=DEFAULT_TIMEOUT_MS)
            else:
                await browser_manager.page.locator(selector).first.evaluate("el => el.submit()")
            await browser_manager._settle(timeout_ms=5000)
            browser_manager.link_cache.mark_dirty()
            return f"✅ Form submitted. Current URL: {browser_manager.page.url}"
        except Exception as e:
            return f"❌ Submit form error: {e}"


# Start MCP server
if __name__ == "__main__":
    logger.info("Starting FirefoxMCP server...")
    mcp.run(transport="stdio")