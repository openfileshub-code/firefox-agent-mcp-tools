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
import time
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

# ---------------------------------------------------------------
# DETACHED BROWSER DAEMON (the robust fix for "browser dies exactly
# N minutes after the last chat message").
#
# Diagnosis: hosts like Cursor/Unbound close the stdio session and then
# SIGKILL the whole MCP server process tree on an idle timer (~5 min).
# SIGKILL is uncatchable — no in-process guard (atexit detach, signal
# handler, __aexit__ wrapper) can stop it from taking the browser with it,
# because Camoufox's Python driver kills the node driver and Firefox when
# its own process dies. The only reliable remedy is to run the browser in
# a SEPARATE OS process that is not part of our process group, so a
# host-side SIGKILL of the server cannot reach it.
#
# Architecture (default mode FIREFOX_MCP_BROWSER_MODE=daemon):
#   server.py  --(subprocess.Popen, new session)--->  browserd.py daemon
#                                                       └── Camoufox/Firefox
#   Control channel: JSON-RPC over stdin/stdout pipes + WS handshake file.
#   The daemon ALSO watches its parent pipe; if the server closes it
#   (graceful session end) the daemon keeps the browser alive by policy.
#   browser_stop() is the ONLY deliberate teardown path.
#
# Fallback mode FIREFOX_MCP_BROWSER_MODE=inprocess restores the previous
# in-process behaviour (still useful under hosts that never SIGKILL).
# ---------------------------------------------------------------
BROWSER_MODE = os.environ.get("FIREFOX_MCP_BROWSER_MODE", "daemon").strip().lower()
DAEMON_SCRIPT = os.path.join(BASE_DIR, "browserd.py")
# How long the daemon may take to bring up the browser (seconds).
DAEMON_START_TIMEOUT = float(os.environ.get("FIREFOX_MCP_DAEMON_START_TIMEOUT", "90"))
# Idle time after which the daemon releases the CDP proxy connection to the
# browser (the browser itself keeps running either way — this is cosmetic).
DAEMON_IDLE_RELEASE_S = float(os.environ.get("FIREFOX_MCP_DAEMON_IDLE_RELEASE", "120"))
# Named-pipe control channel used ONLY when a new MCP server session needs
# to re-attach to a daemon spawned by a previous (killed) session. The
# daemon always listens on this pipe IN ADDITION to its stdin/stdout pipe,
# so the normal fast path (same-session JSON-RPC over pipes) is unchanged.
if os.name == "nt":
    DAEMON_PIPE_NAME = r"\\.\pipe\firefox-mcp-browserd"
else:
    DAEMON_PIPE_NAME = os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "firefox-mcp-browserd.sock")

# Default navigation/action timeout in milliseconds. Defined at module level
# so it is available both inside BrowserManager methods (default arguments are
# evaluated at class-definition time) and in the tool functions below.
DEFAULT_TIMEOUT_MS = 30000

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
            # Drop only OUR control connection; daemon + browser survive.
            if BROWSER_MODE == "daemon":
                await browser_manager._cleanup_pw_client()
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
    # In daemon mode the browser is launched by browserd.py (a separate OS
    # process), so the server itself does not need the camoufox package at
    # all. Only hard-exit when it is actually required (in-process mode).
    if BROWSER_MODE == "inprocess":
        logger.critical(f"Failed to import AsyncCamoufox: {e}")
        sys.stdout = _original_stdout
        sys.stderr = _original_stderr
        _builtin_print(f"❌ FATAL: Could not initialize Camoufox. Error: {e}", file=sys.stderr)
        sys.exit(1)
    logger.warning(
        f"AsyncCamoufox import failed ({e}); tolerated because "
        f"BROWSER_MODE={BROWSER_MODE} (browser runs in detached daemon)."
    )

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
        # Relative paths only make sense when already on a page (origin
        # context). In daemon mode there is no local Page object, so consult
        # the tab mirror instead. Synchronous best-effort read.
        current = ""
        if browser_manager.is_daemon_mode:
            idx = min(getattr(browser_manager, "_daemon_active", 0),
                      max(0, len(getattr(browser_manager, "_daemon_urls", [])) - 1))
            urls = getattr(browser_manager, "_daemon_urls", [])
            current = urls[idx] if urls else ""
        else:
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
class _SocketProcess:
    """Minimal stand-in for asyncio.subprocess.Process backed by a unix
    socket writer (POSIX orphan adoption)."""

    def __init__(self, pid: int, writer: Any) -> None:
        self.pid = pid
        self.stdin = writer
        self.stdout = None
        self.returncode: Optional[int] = None
        self._writer = writer
        self._adopted = True   # no child handle -> explicit liveness checks

    async def wait(self) -> int:
        # Socket closed => daemon gone.
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._writer.is_closing():
                    self.returncode = -1
                    return -1
        except Exception:
            self.returncode = -1
            return -1

    def kill(self) -> None:
        try:
            os.kill(self.pid, _signal.SIGTERM)
        except Exception:
            pass


class _PipeHandleProcess:
    """Stand-in Process for an adopted Windows named-pipe daemon connection."""

    def __init__(self, pid: int, handle: Any, rd_fd: int) -> None:
        self.pid = pid
        self.handle = handle
        self.rd_fd = rd_fd
        self.returncode: Optional[int] = None
        self._adopted = True   # no child handle -> explicit liveness checks
        self.stdin = _PipeWriter(self)

    @staticmethod
    def read_from_handle(handle: Any) -> Optional[bytes]:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore
        buf = ctypes.create_string_buffer(4096)
        got = ctypes.c_ulong(0)
        ok = kernel32.ReadFile(handle, buf, 4096, ctypes.byref(got), None)
        if not ok:
            ERROR_BROKEN_PIPE = 109
            ERROR_MORE_DATA = 232
            ERROR_NO_DATA = 232          # non-blocking pipe, no pending data
            ERROR_PIPE_NOT_CONNECTED = 233
            ERROR_ACCESS_DENIED = 5      # peer closed => our handle dead
            err = kernel32.GetLastError()
            if err == ERROR_BROKEN_PIPE or err == ERROR_PIPE_NOT_CONNECTED:
                return None              # EOF
            if err == ERROR_NO_DATA:
                return b""               # nothing to read right now
            if err == ERROR_ACCESS_DENIED:
                return None
            return b""                   # transient error — retry next tick
        if got.value == 0:
            return b""
        return bytes(buf.raw[:got.value])

    async def wait(self) -> int:
        while True:
            await asyncio.sleep(1.0)
            try:
                import subprocess as _sp
                out = _sp.run(["tasklist", "/FI", f"PID eq {self.pid}", "/NH"],
                              capture_output=True, text=True, timeout=5).stdout
                if str(self.pid) not in out:
                    self.returncode = -1
                    return -1
            except Exception:
                pass

    def kill(self) -> None:
        try:
            import subprocess as _sp
            _sp.run(["taskkill", "/PID", str(self.pid), "/T", "/F"],
                    capture_output=True, timeout=10)
        except Exception:
            pass


class _PipeWriter:
    """asyncio StreamWriter-like object writing JSON lines to a pipe handle."""

    def __init__(self, proc: "_PipeHandleProcess") -> None:
        self._proc = proc

    def write(self, data: bytes) -> None:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore
        written = ctypes.c_ulong(0)
        buf = ctypes.create_string_buffer(data, len(data))
        ok = kernel32.WriteFile(self._proc.handle, buf, len(data),
                                ctypes.byref(written), None)
        if not ok:
            raise RuntimeError("named-pipe write failed (daemon gone?)")

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._proc.handle)
        except Exception:
            pass

    def is_closing(self) -> bool:
        return False


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

        # --- Detached-daemon mode state (see browserd.py) ---
        self._daemon_proc: Optional[asyncio.subprocess.Process] = None
        self._daemon_reader: Optional[asyncio.StreamReader] = None
        # Serialises JSON-RPC round-trips over the single shared pipe.
        self._daemon_lock: asyncio.Lock = asyncio.Lock()
        self._daemon_ws: str = ""          # playwright WS endpoint of daemon
        self._daemon_handshake_path: str = os.path.join(
            self.profile_dir, "browserd.json")
        self._pw_connect: Any = None       # async_playwright instance (server side)
        self._connected_browser: Any = None  # Browser from connect(ws_endpoint)
        # Daemon-mode page mirrors: the real Page objects live in browserd;
        # we keep tab metadata here and route JS through the JSON-RPC pipe.
        self._daemon_urls: list = []
        self._daemon_active: int = 0
        # JSON objects seen on the daemon stdout that are NOT protocol frames
        # (no "event"/"id" keys) — foreign chatter from libraries printing to
        # stdout. Quarantined so they can never be mistaken for RPC replies.
        self._daemon_foreign_msgs: list = []

    @property
    def is_daemon_mode(self) -> bool:
        """True while THIS session's lifecycle owner is the detached daemon.

        Routing decision for every tool must depend ONLY on session state
        (engine + transport present), never on a live child-process handle:
          • daemons we spawned ourselves → _daemon_proc is an asyncio child;
            asyncio sets its .returncode when it exits, so a dead daemon
            stops routing automatically;
          • adopted orphan daemons (_PipeHandleProcess/_SocketProcess) carry
            a *static* returncode that never updates — their liveness is
            verified explicitly in _daemon_call via the `_adopted` flag and
            the CIM/proc/cmdline check, and the watchdog resets state if the
            pipe peer dies.
        Previous bug #1: requiring a live child handle here made
        is_daemon_mode False after successful adoption — every tool fell
        through to the in-process path ("Browser not started." although the
        daemon window was alive).
        Previous bug #2: checking only `engine == "camoufox-daemon"` without
        the transport left tools calling self.page == None right after
        browser_start() set engine (before the first tabs round-trip),
        producing the same confusing error on the FIRST tool call of a fresh
        daemon session. Both are covered by requiring the transport handles.
        """
        return (self.engine == "camoufox-daemon"
                and self._daemon_proc is not None
                and self._daemon_reader is not None
                and self._daemon_proc.returncode is None)

    async def _daemon_call(self, cmd: str, params: Optional[Dict[str, Any]] = None,
                           timeout: float = 30.0) -> Dict[str, Any]:
        """JSON-RPC round-trip to browserd with extra params.

        The pipe is a single shared stdin/stdout pair — two concurrent
        tool calls interleaving requests would scramble replies (responses
        matched to the wrong ids). A per-manager asyncio.Lock serialises
        every round-trip; daemon commands are short so contention is fine.
        """
        async with self._daemon_lock:
            return await self._daemon_call_unlocked(cmd, params, timeout)

    async def _daemon_call_unlocked(self, cmd: str,
                                    params: Optional[Dict[str, Any]] = None,
                                    timeout: float = 30.0) -> Dict[str, Any]:
        # Works both for a child we spawned ourselves (stdin/stdout pipes)
        # and for an adopted orphan daemon (aux control channel wrapped in
        # _SocketProcess/_PipeHandleProcess — same .stdin/.stdout shape).
        proc = self._daemon_proc
        reader = self._daemon_reader
        if proc is None or reader is None:
            raise RuntimeError("Browser not started.")
        # An adopted daemon (via the aux control channel) has no child handle
        # we can poll for exit — a dead pipe/peer may otherwise leave
        # returncode=None forever. Verify liveness explicitly so tools stop
        # routing through a broken transport instead of hanging on it.
        if getattr(proc, "_adopted", False):
            try:
                alive = await asyncio.wait_for(self._daemon_alive(proc.pid),
                                               timeout=10.0)
            except Exception:
                alive = True  # checker failed — be conservative
            if not alive:
                proc.returncode = -1
                raise RuntimeError("daemon process is gone")
        rid = random.randint(1, 2**31)
        payload = {"id": rid, "cmd": cmd}
        if params:
            payload.update(params)
        try:
            proc.stdin.write(
                (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise RuntimeError(
                f"daemon pipe closed ({e.__class__.__name__})") from e
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"daemon did not answer '{cmd}' within {timeout:.0f}s")
            if not line:
                raise RuntimeError("daemon pipe closed")
            raw = line.decode("utf-8", "ignore").rstrip()
            try:
                msg = json.loads(raw)
            except ValueError:
                # Foreign plain-text chatter on stdout (e.g. camoufox's
                # "Skipping unknown patch ..." print): keep it visible.
                if raw:
                    logger.warning(f"[browserd:raw] {raw[:200]}")
                continue
            if msg.get("event") == "log":
                lvl = str(msg.get("level") or "info").lower()
                text = f"[browserd:{lvl}] {msg.get('msg')}"
                if lvl in ("warning", "error"):
                    logger.warning(text)
                else:
                    logger.info(text)
                continue
            if isinstance(msg, dict) and "event" not in msg and "id" not in msg:
                # Foreign JSON printed to the daemon's stdout by some library
                # (same quarantine as in _spawn_daemon): never treat it as a
                # reply — matching on a missing id would silently swallow it,
                # but logging keeps it visible for diagnosis.
                self._daemon_foreign_msgs.append(msg)
                logger.warning(f"[browserd:foreign-json] {raw[:200]}")
                continue
            if msg.get("id") == rid:
                if not msg.get("ok"):
                    raise RuntimeError(msg.get("error", f"daemon '{cmd}' failed"))
                return msg

    async def _daemon_refresh_tabs(self) -> None:
        try:
            res = await self._daemon_call("tabs", timeout=10.0)
            self._daemon_urls = [t.get("url", "") for t in res.get("tabs", [])]
            self._daemon_active = int(res.get("active", self._daemon_active))
        except Exception as e:
            logger.debug(f"tab refresh failed: {e}")

    def _read_handshake(self) -> Optional[Dict[str, Any]]:
        """Read handshake file, validate version, ignore stale ppid."""
        try:
            with open(self._daemon_handshake_path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
                if not isinstance(info, dict):
                    return None
                if info.get("profile") != self.profile_dir:
                    return None
                version = info.get("daemon_version", 1)
                if version < 1 or version > 10:
                    logger.warning(f"Unknown handshake version {version}, proceeding cautiously")
                return info
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.debug(f"Handshake read failed: {e}")
            return None

    def _remove_handshake(self) -> None:
        try:
            os.remove(self._daemon_handshake_path)
        except OSError:
            pass

    def _pid_cmdline_is_daemon(self, pid: int) -> bool:
        """POSIX: verify the PID is really OUR browserd (cmdline check)."""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "ignore").replace("\0", " ")
            return "browserd.py" in cmd and self.profile_dir in cmd
        except OSError:
            return False

    async def _daemon_alive(self, pid: int) -> bool:
        """Is a *browserd* process with this PID still running?"""
        if pid <= 0:
            return False
        # Fast path: daemon spawned by THIS server session.
        if self._daemon_proc is not None and self._daemon_proc.pid == pid \
                and self._daemon_proc.returncode is None:
            return True
        if os.name == "nt":
            # A recycled PID could belong to any Windows process; verify the
            # command line actually references OUR daemon + profile (CIM via
            # PowerShell), mirroring the POSIX /proc/cmdline check.
            try:
                import subprocess as _sp
                ps = (
                    "Get-CimInstance Win32_Process -Filter \"ProcessId="
                    + str(pid) + "\" | Select-Object -ExpandProperty CommandLine"
                )
                out = _sp.run(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=8).stdout
                return ("browserd.py" in out) and (self.profile_dir in out)
            except Exception:
                # Cannot tell — be conservative: assume alive so we never
                # kill/replace a possibly-live daemon; stop()/cleanup can
                # still reclaim via handshake file removal.
                return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        # A recycled PID could belong to an unrelated process — make sure it
        # is actually our daemon before treating it as alive.
        return self._pid_cmdline_is_daemon(pid)

    async def _attach_context(self, context: Any) -> None:
        """Adopt a Playwright BrowserContext (from Camoufox or connect())."""
        self.context = context
        pages = context.pages
        self._pages = list(pages) if pages else []
        if not self._pages:
            self._pages.append(await context.new_page())
        self._active_page_index = 0
        for p in self._pages:
            p.on("close", lambda p=p: self._on_page_closed(p))
        self._setup_page_tracking(context)
        self.is_running = True
        self.engine = "camoufox"
        self._user_closed_event.clear()

    # ---------------- detached daemon control channel ----------------

    async def _daemon_request(self, cmd: str, timeout: float = 15.0) -> Dict[str, Any]:
        # Same locked round-trip as _daemon_call (kept for call-site clarity).
        return await self._daemon_call(cmd, None, timeout)

    async def _spawn_daemon(self, headless: bool) -> Dict[str, Any]:
        args = [DAEMON_SCRIPT, "--profile", self.profile_dir,
                "--idle-release", str(DAEMON_IDLE_RELEASE_S)]
        if headless:
            args.append("--headless")

        daemon_log = os.path.join(BASE_DIR, "browserd.log")

        env = os.environ.copy()
        env.setdefault("PLAYWRIGHT_SKIP_BROWSER_GC", "1")

        # --- FIND pythonw.exe to avoid console window ---
        python_exe = sys.executable
        python_dir = os.path.dirname(python_exe)
        pythonw_exe = os.path.join(python_dir, "pythonw.exe")
        
        use_pythonw = os.path.isfile(pythonw_exe)
        if use_pythonw:
            interpreter = pythonw_exe
            logger.info(f"Using pythonw.exe (no console): {pythonw_exe}")
        else:
            interpreter = python_exe
            logger.info(f"pythonw.exe not found, using python.exe: {python_exe}")
        
        args.insert(0, interpreter)
        # --- END pythonw selection ---

        popen_kwargs: Dict[str, Any] = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=BASE_DIR,
            env=env,
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        else:
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            
            popen_kwargs["creationflags"] = (
                DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            )
            
            # Add startupinfo for extra safety
            try:
                import subprocess as _sp
                si = _sp.STARTUPINFO()
                si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                si.wShowWindow = 0  # SW_HIDE
                popen_kwargs["startupinfo"] = si
            except Exception:
                pass

        self._daemon_proc = await asyncio.create_subprocess_exec(*args, **popen_kwargs)
        self._daemon_reader = self._daemon_proc.stdout

        # Wait for the ready event (daemon launches Camoufox before replying).
        self._daemon_foreign_msgs = []
        deadline = time.monotonic() + DAEMON_START_TIMEOUT
        exit_seen = False
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    self._daemon_reader.readline(), timeout=max(1.0, deadline-time.monotonic()))
            except asyncio.TimeoutError:
                break
            if not line:
                exit_seen = True
                break
            raw = line.decode("utf-8", "ignore").rstrip()
            try:
                msg = json.loads(raw)
            except ValueError:
                with open(daemon_log, "a", encoding="utf-8") as fh:
                    fh.write(raw+"\n")
                logger.warning(f"[browserd:raw] {raw}")
                continue
            if isinstance(msg, dict) and "event" not in msg and "id" not in msg:
                self._daemon_foreign_msgs.append(msg)
                logger.warning(f"[browserd:foreign-json] {raw[:200]}")
                continue
            if msg.get("event") == "log":
                logger.info(f"[browserd:{msg.get('level')}]: {msg.get('msg')}")
            elif msg.get("event") == "ready":
                self._daemon_ws = msg.get("ws_endpoint", "")
                return msg

        rc = self._daemon_proc.returncode
        if exit_seen or rc is not None:
            raise RuntimeError(
                f"daemon exited during startup (rc={rc}); last errors in {daemon_log}")
        raise RuntimeError(
            f"daemon did not become ready within {DAEMON_START_TIMEOUT}s; check {daemon_log}")
     

    async def _connect_over_ws(self, ws_endpoint: str) -> Any:
        """Connect to the daemon's Playwright WS server and take its default
        persistent context (the one Camoufox launched with our profile)."""
        from playwright.async_api import async_playwright  # noqa: PLC0415
        self._pw_connect = await async_playwright().start()
        # Daemon runs a FIREFOX-driver playwright ws-server (Camoufox is
        # Gecko-based), so attach with the matching driver family.
        browser = await asyncio.wait_for(
            self._pw_connect.firefox.connect(ws_endpoint), timeout=30.0)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("daemon exposes no browser context over WS")
        return contexts[0]

    async def _start_daemon_mode(self, headless: bool) -> str:
        # 1) A daemon from a previous session may still own the browser.
        info = self._read_handshake()
        if info and await self._daemon_alive(int(info.get("pid", -1))):
            pid = int(info["pid"])
            
            # ← FIX: Added small delay to allow daemon to fully initialize
            # This prevents race condition when server restarts quickly
            await asyncio.sleep(0.5)

            # Same server process (e.g. after an internal restart attempt):
            # our live pipe is still open — re-attach transparently.
            if self._daemon_proc is not None and self._daemon_proc.pid == pid \
            and self._daemon_proc.returncode is None:
                try:
                    pong = await self._daemon_request("ping", timeout=10.0)
                    if pong.get("ok"):
                        self.is_running = True
                        return (f"♻️ Re-attached to running browser daemon "
                                f"(pid={pid}). Browser window is alive.")
                except Exception as e:
                    logger.warning(f"Ping failed on existing proc: {e}")

            # ← FIX: Removed ppid check - it's no longer in handshake
            # Daemon was spawned by a PREVIOUS server session whose pipe is
            # gone. Verify liveness strictly via cmdline check.
            if os.name == "nt":
                if not await self._daemon_alive(pid):
                    self._remove_handshake()
                    info = None
                elif not self._pid_cmdline_is_daemon(pid):
                    self._remove_handshake()
                    info = None
            else:  # POSIX
                if not await self._daemon_alive(pid):
                    self._remove_handshake()
                    info = None
                elif not self._pid_cmdline_is_daemon(pid):
                    self._remove_handshake()
                    info = None

            if info is not None:
                # Try to adopt the orphaned daemon through its secondary
                # control channel (named pipe / unix socket).
                try:
                    adopted = await self._adopt_orphan_daemon(pid)
                    if adopted:
                        pong = await self._daemon_call("ping", timeout=10.0)
                        if pong.get("ok"):
                            self.is_running = True
                            self.engine = "camoufox-daemon"
                            self._intentional_stop = False
                            _install_keepalive_signal_guard()
                            if self._keepalive_task is None or self._keepalive_task.done():
                                try:
                                    self._keepalive_task = asyncio.create_task(
                                        self._keepalive_loop())
                                except RuntimeError:
                                    pass
                            await self._daemon_refresh_tabs()
                            return (f"♻️ Re-attached to running browser daemon "
                                    f"(pid={pid}) via control channel — full "
                                    f"agent control restored.")
                except Exception as e:
                    logger.debug(f"orphan adoption failed: {e}")

            # Adoption impossible: the WINDOW must still survive
            return (
                f"♻️ Browser is already running in detached daemon mode "
                f"(daemon pid={pid}, window owned by it).\n"
                "The current chat server cannot reattach Playwright to that "
                "browser (Gecko persistent contexts are not servable over an "
                "endpoint), so options:\n"
                " • Keep watching/controlling it MANUALLY — video/audio keep "
                "going regardless of chat activity.\n"
                f" • Take full control again: run `python browserd.py --kill "
                f"--profile {self.profile_dir}` (or close the window), then "
                "call browser_start() anew."
            )

        if info:
            self._remove_handshake()  # stale handshake from a dead daemon

        # 2) Fresh detached start.
        await cleanup_stale_browser_processes(self.profile_dir)
        remove_lock_files(self.profile_dir)  # daemon will create its own
        actual_headless = bool(headless)
        if headless and _headless_unsafe_platform():
            logger.warning("Camoufox headless=True crashes on Windows. Forcing False.")
            actual_headless = False
        ready = await self._spawn_daemon(actual_headless)
        self._intentional_stop = False
        _install_keepalive_signal_guard()
        # Watchdog must also run in daemon mode: it detects a daemon crash or
        # the user closing the browser window and resets state accordingly.
        if self._keepalive_task is None or self._keepalive_task.done():
            try:
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            except RuntimeError:
                pass  # no running loop (unit tests) — watchdog optional
        # Adopt whatever tabs the daemon already has (usually one) BEFORE
        # declaring success. This is also the transport smoke-test: if the
        # first round-trip fails, we must NOT leave a half-initialised
        # session behind — that was bug #3: browser_start() returned ✅ while
        # the pipe/handshake state was broken, and every following tool call
        # died with "Browser not started." with no way to recover short of
        # killing the daemon manually.
        try:
            tabs = await self._daemon_request("tabs", timeout=20.0)
            urls = [t.get("url", "") for t in tabs.get("tabs", [])]
            self._daemon_urls = urls or [""]
            self._daemon_active = int(tabs.get("active", 0))
        except Exception as e:
            logger.error(f"Daemon smoke-test failed: {e}", exc_info=True)
            # Tear down our view of the session; the daemon + window stay
            # alive (we never kill the browser on a control-channel hiccup).
            proc = self._daemon_proc
            adopted = bool(getattr(proc, "_adopted", False)) if proc else False
            # IMPORTANT: closing OUR write handle to the child's stdin makes
            # the daemon detach — but only when we spawned it ourselves and
            # its aux takeover channel is verified working.  Otherwise the
            # detached daemon would drop the handshake and then sit there
            # uncontactable, locking the profile against every future start
            # (this was the Windows "daemon pipe closed" cascade).  When in
            # doubt: keep stdin open, keep the handshake, let the user close
            # the visible window manually — cleanup_stale_browser_processes()
            # reclaims the profile afterwards.
            takeover_ok = False
            if proc is not None and not adopted:
                try:
                    pong = await self._daemon_request("ping", timeout=10.0)
                    takeover_ok = bool(pong.get("ok"))
                except Exception as pe:
                    logger.warning(
                        f"stdin takeover ping failed ({pe}); leaving the "
                        f"daemon attached to us rather than orphaning it")
            if proc is not None and not adopted and takeover_ok:
                try:
                    if getattr(proc, "stdin", None):
                        proc.stdin.close()
                except Exception:
                    pass
                # The daemon will detach and exit soon; stop routing tools
                # through this broken transport.
            self._daemon_proc = None
            self._daemon_reader = None
            self.engine = "none"
            self.is_running = False
            if adopted:
                hint = ("Next browser_start() will retry the named-pipe "
                        "control channel automatically.")
            elif takeover_ok:
                hint = ("The daemon detached cleanly; next browser_start() "
                        "will take over that window via the named-pipe "
                        "control channel automatically.")
            else:
                hint = ("Close the browser window manually, then call "
                        "browser_start() anew.")
            raise RuntimeError(
                f"daemon became ready but the first control round-trip "
                f"failed ({e}). The browser window is still open. {hint} "
                f"If it keeps failing, run `python browserd.py --kill "
                f"--profile {self.profile_dir}` and set "
                f"FIREFOX_MCP_BROWSER_MODE=inprocess as a fallback.")
        self.is_running = True
        self.engine = "camoufox-daemon"
        self._user_closed_event.clear()
        return (f"✅ Camoufox started in detached daemon mode "
                f"(daemon pid={ready.get('pid')}; survives chat idle & host "
                f"kills of this server process). Control channel verified.\n"
                f"Profile: {self.profile_dir}\nHeadless: {actual_headless}")

    async def _adopt_orphan_daemon(self, pid: int) -> bool:
        """Open the daemon's secondary control channel (named pipe on Windows,
        unix socket on POSIX) and wire it in place of the lost stdin/stdout
        pipe. Returns True on success."""
        if os.name == "nt":
            import threading
            loop = asyncio.get_running_loop()
            holder: Dict[str, Any] = {}

            def _open_blocking() -> Any:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore
                GENERIC_READ = 0x80000000
                GENERIC_WRITE = 0x40000000
                OPEN_EXISTING = 3
                h = kernel32.CreateFileW(
                    DAEMON_PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0, None,
                    OPEN_EXISTING, 0, None)
                # INVALID_HANDLE_VALUE == -1 as a signed 64-bit value
                val = ctypes.c_void_p(h).value
                if val is None or val >= 0xFFFFFFFFFFFFFFFE:
                    raise OSError(
                        f"CreateFileW failed: {kernel32.GetLastError()}")
                return h

            try:
                last_err: Optional[Exception] = None
                for _attempt in range(10):   # daemon may be mid-command
                    try:
                        holder["h"] = await loop.run_in_executor(
                            None, _open_blocking)
                        break
                    except OSError as e:
                        last_err = e
                        await asyncio.sleep(0.5)
                else:
                    raise last_err or OSError("pipe connect failed")
            except Exception as e:
                logger.debug(f"named-pipe open failed: {e}")
                return False

            # Windows named pipes opened with CreateFileW default to
            # *blocking* mode: ReadFile parks the thread until data arrives.
            # A single parked blocking read also prevents any other thread
            # from completing a write on the same handle — so in the adopted
            # orphan session every daemon reply got stuck and each tool call
            # hit its timeout ("Browser not started" after state reset).
            # Fix: create a SECOND handle to the same pipe dedicated to reads
            # and put it into non-blocking mode via SetNamedPipeHandleState
            # (fNonBlocking=1). WriteFile stays on the original handle;
            # ReadFile returns immediately when no message is pending.
            def _make_reader_handle(h_write: Any) -> Any:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore
                GENERIC_READ = 0x80000000
                OPEN_EXISTING = 3
                h_read = kernel32.CreateFileW(
                    DAEMON_PIPE_NAME, GENERIC_READ, 0, None,
                    OPEN_EXISTING, 0, None)
                val = ctypes.c_void_p(h_read).value
                if val is None or val >= 0xFFFFFFFFFFFFFFFE:
                    raise OSError(f"CreateFileW(read) failed: {kernel32.GetLastError()}")
                NONBLOCKING = 1
                ok = kernel32.SetNamedPipeHandleState(
                    h_read, ctypes.c_ulong(NONBLOCKING), None, None, None)
                if not ok:
                    logger.debug(
                        f"SetNamedPipeHandleState(nonblock): {kernel32.GetLastError()}")
                return h_read

            try:
                h_read = await loop.run_in_executor(None, _make_reader_handle, holder["h"])
            except Exception as e:
                logger.debug(f"named-pipe reader handle failed: {e}")
                try:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(holder["h"])
                except Exception:
                    pass
                return False

            proc = _PipeHandleProcess(pid, holder["h"], -1)
            reader = asyncio.StreamReader(limit=2 ** 20)

            def _pump() -> None:
                buf = b""
                try:
                    while True:
                        chunk = _PipeHandleProcess.read_from_handle(h_read)
                        if chunk is None:      # broken pipe / EOF
                            break
                        if chunk == b"":        # no data right now
                            time.sleep(0.05)
                            continue
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            try:
                                loop.call_soon_threadsafe(
                                    reader.feed_data, line + b"\n")
                            except RuntimeError:
                                return
                finally:
                    try:
                        loop.call_soon_threadsafe(reader.feed_eof)
                    except RuntimeError:
                        pass

            threading.Thread(target=_pump, daemon=True,
                             name="browserd-pipe-pump").start()
            self._daemon_proc = proc
            self._daemon_reader = reader
            return True

        # POSIX: connect to the unix socket, wrap with asyncio streams.
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(DAEMON_PIPE_NAME), timeout=5.0)
        except Exception as e:
            logger.debug(f"unix socket connect failed: {e}")
            return False
        self._daemon_proc = _SocketProcess(pid, writer)
        self._daemon_reader = reader
        return True

    async def _cleanup_pw_client(self) -> None:
        """Drop OUR side of the WS connection without touching the browser."""
        try:
            if self._connected_browser is not None:
                self._connected_browser.close()  # sync transport: fire-forget
        except Exception:
            pass
        self._connected_browser = None
        try:
            if self._pw_connect is not None:
                await self._pw_connect.stop()
        except Exception:
            pass
        self._pw_connect = None

    # --- Lifecycle ---

    async def start(self, headless: bool = False) -> str:
        if self.is_running:
            return "Browser is already running."
        # Detached-daemon mode (default): the browser lives in a separate OS
        # process so host-side idle kills of THIS server cannot take it down.
        if BROWSER_MODE == "daemon":
            try:
                return await self._start_daemon_mode(headless=headless)
            except Exception as e:
                logger.error(f"Daemon-mode start failed: {e}", exc_info=True)
                raise RuntimeError(
                    f"Failed to start browser daemon: {e}. "
                    "Set FIREFOX_MCP_BROWSER_MODE=inprocess to use the legacy "
                    "in-process mode."
                ) from e
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

        IMPORTANT: the watchdog is scoped to the lifecycle it actually owns:
          • daemon mode   → poll the daemon child-process exit code only
            (self._daemon_proc). It must NOT touch self.context / pages,
            which are managed exclusively by the daemon transport.
          • in-process    → watch the Camoufox context/pages/connection.
        A previous bug ran the in-process checks ("context is None") inside
        daemon sessions and tore down perfectly healthy daemon state after
        the first 5-minute tick.
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

                if BROWSER_MODE == "daemon":
                    # --- daemon-scoped watchdog: nothing but the child
                    # process exit status is authoritative here. ---
                    proc = self._daemon_proc
                    if proc is not None and getattr(proc, "returncode", None) is not None:
                        logger.info("Keepalive: daemon exited — resetting state.")
                        await self._cleanup_pw_client()
                        self._daemon_proc = None
                        self._daemon_reader = None
                        self._daemon_writer = None
                        self._remove_handshake()
                        self._mark_dead()
                        break
                    # Daemon still alive → we do nothing else. Never inspect
                    # self.context / pages from this task in daemon mode.
                    self._user_closed_event.clear()
                    continue

                # --- in-process watchdog below ---
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
        # ---- detached daemon mode ----
        if BROWSER_MODE == "daemon":
            return await self._stop_daemon_mode()
        # ---- legacy in-process mode ----
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

    async def _stop_daemon_mode(self) -> str:
        """Close the browser via the daemon (the deliberate-stop path)."""
        # Case A: we have a live pipe to the daemon we spawned/attached-to
        # this session — ask it to close everything.
        if self._daemon_proc is not None and self._daemon_reader is not None \
                and self._daemon_proc.returncode is None:
            try:
                reply = await self._daemon_request("stop", timeout=30.0)
                if not reply.get("ok"):
                    logger.warning(f"daemon stop error: {reply.get('error')}")
            except Exception as e:
                logger.warning(f"daemon stop request failed ({e}); forcing kill")
            try:
                await asyncio.wait_for(self._daemon_proc.wait(), timeout=10.0)
            except Exception:
                try:
                    self._daemon_proc.kill()
                except Exception:
                    pass
        else:
            # Case B: no pipe (fresh server after a host-side kill of the old
            # one, or attach-only reuse where the daemon died). The daemon is
            # unreachable → close whatever holds OUR profile directly.
            info = self._read_handshake()
            if info and await self._daemon_alive(int(info.get("pid", -1))):
                # Daemon alive but pipe lost: terminate the daemon politely;
                # its SIGTERM policy keeps the browser, so afterwards clean
                # the profile-scoped browser processes ourselves.
                pid = int(info["pid"])
                try:
                    if os.name == "posix":
                        os.kill(pid, _signal.SIGTERM)
                    else:
                        import subprocess as _sp
                        _sp.run(["taskkill", "/PID", str(pid), "/T"],
                                capture_output=True, timeout=10)
                    await asyncio.sleep(1.5)
                except Exception:
                    pass
            await cleanup_stale_browser_processes(self.profile_dir)
            remove_lock_files(self.profile_dir)
            self._remove_handshake()

        await self._cleanup_pw_client()
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except BaseException:
                pass
        self._daemon_proc = None
        self._daemon_reader = None
        self._daemon_ws = ""
        self._intentional_stop = True
        self.context = None
        self._pages = []
        self._active_page_index = 0
        self.is_running = False
        self.engine = "none"
        logger.info("Browser stopped (daemon mode).")
        return "🛑 Browser stopped (detached daemon closed)."

    def _require_page(self) -> None:
        if not self.is_running:
            raise RuntimeError("Browser not started.")
        if self.is_daemon_mode:
            return  # pages live in browserd; tools route via _daemon_call
        if not self.page:
            raise RuntimeError("Browser not started.")

    @property
    def page(self) -> Optional[Any]:
        if not self._pages:
            return None
        return self._pages[self._active_page_index]

    async def _goto(self, url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        """Navigate the active tab — in-process or through the daemon pipe."""
        if self.is_daemon_mode:
            await self._daemon_call(
                "navigate", {"url": url, "timeout": timeout_ms / 1000.0},
                timeout=timeout_ms / 1000.0 + 15.0)
            await self._daemon_refresh_tabs()
            return
        await self.page.goto(url, timeout=timeout_ms)

    async def _get_content(self) -> str:
        if self.is_daemon_mode:
            res = await self._daemon_call("get_content", timeout=30.0)
            return res.get("result", "")
        return await self.page.content()

    async def _current_url(self) -> str:
        if self.is_daemon_mode:
            if not self._daemon_urls:
                await self._daemon_refresh_tabs()
            idx = min(self._daemon_active, max(0, len(self._daemon_urls) - 1))
            return self._daemon_urls[idx] if self._daemon_urls else ""
        return self.page.url if self.page else ""

    async def _screenshot(self, fname: str, full_page: bool = False) -> str:
        if self.is_daemon_mode:
            await self._daemon_call(
                "screenshot", {"path": fname, "full_page": full_page}, timeout=40.0)
            return fname
        await self.page.screenshot(path=fname, full_page=full_page)
        return fname

    async def _press_key(self, key: str) -> None:
        if self.is_daemon_mode:
            await self._daemon_call("press_key", {"key": key})
            return
        await self.page.keyboard.press(key)

    async def _scroll_by(self, delta: float) -> None:
        if self.is_daemon_mode:
            direction = "down" if delta >= 0 else "up"
            await self._daemon_call("scroll",
                                    {"direction": direction, "amount": abs(delta)})
            return
        await self.page.evaluate(f"window.scrollBy(0, {delta})")

    async def _click_text(self, text: str) -> None:
        if self.is_daemon_mode:
            await self._daemon_call("click_text", {"text": text}, timeout=20.0)
            await self._daemon_refresh_tabs()
            return
        await self.page.get_by_text(text, exact=False).first.click(
            timeout=DEFAULT_TIMEOUT_MS)

    async def _type_text(self, text: str, delay: int = 30) -> None:
        """Type into the element that currently has DOM focus."""
        if self.is_daemon_mode:
            await self._daemon_call("type_text", {"text": text, "delay": delay})
            return
        await self.page.keyboard.type(text, delay=delay)

    async def _focus_selector(self, selector: str) -> bool:
        """Focus a visible element by CSS selector; True if found & focused."""
        js = """
        (sel) => {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                if (el && el.offsetParent !== null) {
                    el.scrollIntoView({block: 'center'});
                    el.focus();
                    return true;
                }
            }
            return false;
        }
        """
        return bool(await self._safe_evaluate(js, selector))

    async def _locator_count(self, selector: str) -> int:
        if self.is_daemon_mode:
            res = await self._daemon_call("locator_count", {"selector": selector})
            return int(res.get("result", 0))
        try:
            return await self.page.locator(selector).count()
        except Exception:
            return 0

    async def _locator_fill(self, selector: str, value: str) -> None:
        if self.is_daemon_mode:
            await self._daemon_call("locator_fill",
                                    {"selector": selector, "value": value})
            return
        await self.page.locator(selector).first.fill(value)

    async def _locator_click(self, selector: str) -> None:
        if self.is_daemon_mode:
            await self._daemon_call("locator_click", {"selector": selector})
            await self._daemon_refresh_tabs()
            return
        await self.page.locator(selector).first.click(timeout=DEFAULT_TIMEOUT_MS)

    async def _locator_submit(self, selector: str) -> None:
        if self.is_daemon_mode:
            await self._daemon_call("locator_submit", {"selector": selector})
            await self._daemon_refresh_tabs()
            return
        await self.page.locator(selector).first.evaluate("el => el.submit()")

    # --- Video-controller / link-cache routing (daemon-transparent) ---

    class _RoutedPage:
        """Lightweight stand-in for a Playwright Page that routes every JS
        call through the daemon JSON-RPC pipe. Passed to VideoController and
        LinkCache so those modules keep working UNCHANGED in daemon mode."""

        def __init__(self, mgr: "BrowserManager") -> None:
            self._mgr = mgr

        @property
        def url(self) -> str:
            m = self._mgr
            if not m._daemon_urls:
                return ""
            idx = min(m._daemon_active, len(m._daemon_urls) - 1)
            return m._daemon_urls[idx]

        async def evaluate(self, expression: str, arg: Any = None) -> Any:
            return await self._mgr._safe_evaluate(expression, arg)

        async def title(self) -> str:
            """Async title() mirroring the real Playwright Page API (used by
            link-cache extraction and snapshot fallbacks)."""
            try:
                res = await self._mgr._daemon_call(
                    "eval", {"script": "() => document.title"}, timeout=20.0)
                return str(res.get("result", "") or "")
            except Exception:
                return ""

        @property
        def keyboard(self) -> "_RoutedKeyboard":
            return BrowserManager._RoutedKeyboard(self._mgr)

    class _RoutedKeyboard:
        def __init__(self, mgr: "BrowserManager") -> None:
            self._mgr = mgr

        async def press(self, key: str) -> None:
            await self._mgr._press_key(key)

        async def down(self, key: str) -> None:
            await self._mgr._daemon_call("key_down", {"key": key})

        async def up(self, key: str) -> None:
            await self._mgr._daemon_call("key_up", {"key": key})

        async def type(self, text: str, delay: int = 30) -> None:
            await self._mgr._type_text(text, delay)

    def _page_for_modules(self) -> Any:
        """The object to hand to VideoController/LinkCache helpers."""
        if self.is_daemon_mode:
            return BrowserManager._RoutedPage(self)
        return self.page

    async def _ensure_links_fresh(self) -> None:
        page = self._page_for_modules()
        if page is None:
            raise RuntimeError("Browser not started.")
        await self.link_cache.ensure_fresh(page)

    async def _new_tab(self, url: str = "") -> None:
        if self.is_daemon_mode:
            await self._daemon_call("new_tab", {"url": url or "about:blank"})
            await self._daemon_refresh_tabs()
            return
        page = await self.context.new_page()
        if url:
            await page.goto(url, timeout=DEFAULT_TIMEOUT_MS)
        self._pages.append(page)
        self._active_page_index = len(self._pages) - 1
        page.on("close", lambda p=page: self._on_page_closed(p))

    async def _switch_tab(self, index: int) -> None:
        if self.is_daemon_mode:
            await self._daemon_call("switch_tab", {"index": index})
            await self._daemon_refresh_tabs()
            return
        if 0 <= index < len(self._pages):
            self._active_page_index = index
            await self.page.bring_to_front()

    async def _close_tab(self, index: Optional[int] = None) -> None:
        if self.is_daemon_mode:
            idx = self._daemon_active if index is None else index
            await self._daemon_call("close_tab", {"index": idx})
            await self._daemon_refresh_tabs()
            return
        idx = self._active_page_index if index is None else index
        if 0 <= idx < len(self._pages):
            await self._pages[idx].close()

    @property
    def tab_count(self) -> int:
        return len(self._pages)

    async def _settle(self, timeout_ms: int = 2000) -> None:
        if self.is_daemon_mode:
            try:
                await self._daemon_call(
                    "wait_ready", {"timeout": timeout_ms / 1000.0},
                    timeout=timeout_ms / 1000.0 + 5.0)
            except Exception:
                pass
            return
        if not self.page:
            return
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass

    async def _safe_evaluate(self, expression: str, arg: Any = None, timeout: float = 15.0) -> Any:
        if not self.is_running:
            raise RuntimeError("Browser not started.")
        if self.is_daemon_mode:
            msg = await self._daemon_call(
                "eval", {"script": expression, "arg": arg, "timeout": timeout},
                timeout=timeout + 10.0)
            return msg.get("result")
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
        if self.is_daemon_mode:
            await self._daemon_call("new_tab", {"url": url or "about:blank"})
            await self._daemon_refresh_tabs()
            self.link_cache.mark_dirty()
            idx = self._daemon_active
            if url:
                return f"✅ New tab #{idx} opened at: {url}"
            return f"✅ New empty tab #{idx} created and activated."
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
        if self.is_daemon_mode:
            await self._daemon_call("close_tab", {"index": index})
            await self._daemon_refresh_tabs()
            self.link_cache.mark_dirty()
            return (f"🛑 Tab #{index} closed. Active tab: "
                    f"#{self._daemon_active}.")
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
        if self.is_daemon_mode:
            # Titles are not mirrored to the server; URLs come from the last
            # refresh (browser_list_tabs awaits _daemon_refresh_tabs first).
            lines = []
            for i, u in enumerate(self._daemon_urls):
                marker = " ← active" if i == self._daemon_active else ""
                lines.append(f"{i}. {u!r}{marker}")
            return "\n".join(lines) if lines else "No open tabs."
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
        if self.is_daemon_mode:
            await self._daemon_call("switch_tab", {"index": index})
            await self._daemon_refresh_tabs()
            self.link_cache.mark_dirty()
            url = self._daemon_urls[index] if 0 <= index < len(self._daemon_urls) else ""
            return f"✅ Switched to tab #{index}: {url}"
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
# (DEFAULT_TIMEOUT_MS defined near the top of this module)
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

        await browser_manager._goto(url, DEFAULT_TIMEOUT_MS)
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
        content = await browser_manager._get_content()
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
                await browser_manager._goto(url, DEFAULT_TIMEOUT_MS)
                await browser_manager._settle()
                browser_manager.link_cache.mark_dirty()
                return f"✅ Opened: {url}"

            # 2) alias from sites config
            aliases = browser_manager.sites_config.get("aliases", {})
            key = t.lower()
            if key in aliases:
                url = aliases[key]
                await browser_manager._goto(url, DEFAULT_TIMEOUT_MS)
                await browser_manager._settle()
                browser_manager.link_cache.mark_dirty()
                return f"✅ Opened alias '{t}' → {url}"

            # 3) link/element text on current page (fuzzy)
            await browser_manager._ensure_links_fresh()
            results = browser_manager.link_cache.search(t)
            hrefs = [r for r in results if r.get("href")]
            if len(hrefs) == 1:
                url = hrefs[0]["href"]
                await browser_manager._goto(url, DEFAULT_TIMEOUT_MS)
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
            await browser_manager._type_text(query, delay=30)
            await asyncio.sleep(0.3)
            await browser_manager._press_key("Enter")
            await browser_manager._settle(timeout_ms=5000)
            browser_manager.link_cache.mark_dirty()
            return (f"✅ Search submitted on-page: {query!r}. "
                    f"Current URL: {await browser_manager._current_url()}")
        except Exception as e:
            return f"❌ browser_search_on_page error: {e}"

    @mcp.tool()
    async def browser_find_link(query: str) -> str:
        """Finds links, tabs, buttons, and interactive elements by substring/fuzzy match.
        Works with JS tabs and aria-labels. Returns numbered list, does NOT click."""
        try:
            browser_manager._require_page()
            await browser_manager._ensure_links_fresh()
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
            await browser_manager._ensure_links_fresh()
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
                await browser_manager._goto(url, DEFAULT_TIMEOUT_MS)
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
        if browser_manager.is_daemon_mode:
            if not browser_manager.is_running:
                return "⏳ Browser is starting up, wait 3-5 seconds and retry."
                
        browser_manager._require_page()
        snapshot = await browser_manager.vc.get_snapshot(browser_manager._page_for_modules()) \
            if browser_manager.vc else {}
        player_info = await browser_manager.vc.get_player_info(browser_manager._page_for_modules()) \
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
        snap = await browser_manager.vc.get_snapshot(browser_manager._page_for_modules())
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
            if browser_manager.is_daemon_mode:
                if not browser_manager.is_running:
                    return "⏳ Browser is starting up, wait 3-5 seconds and retry."
            
            browser_manager._require_page()
            snapshot = await browser_manager.vc.get_snapshot(browser_manager._page_for_modules())
            player_info = await browser_manager.vc.get_player_info(browser_manager._page_for_modules())
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
            if browser_manager.is_daemon_mode:
                if not browser_manager.is_running:
                    return "⏳ Browser is starting up, wait 3-5 seconds and retry."
            
            browser_manager._require_page()
            if action in ("state", "download_source", "cleanup"):
                # Read-only / housekeeping commands don't need the gate.
                return await browser_manager.vc.action(browser_manager._page_for_modules(), action, param)

            gate = await _video_fingerprint_gate()
            if gate:
                return gate

            # Snapshot BEFORE the mutation — used for honest verification of
            # volume actions (a site script may silently reset the slider).
            pre_snap = None
            if action.startswith(("volume", "mute")):
                try:
                    pre_snap = await browser_manager.vc.get_snapshot(
                        browser_manager._page_for_modules())
                except Exception:
                    pre_snap = None

            result = await browser_manager.vc.action(browser_manager._page_for_modules(), action, param)

            # Verify post-state where possible and refresh baseline after mutation
            snap = await browser_manager.vc.get_snapshot(browser_manager._page_for_modules())
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
            fname = os.path.join(shots_dir, f"shot_{int(time.time())}.png")
            await browser_manager._screenshot(fname, full_page)
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
            if browser_manager.is_daemon_mode:
                await browser_manager._daemon_refresh_tabs()
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
            await browser_manager._press_key(k)
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
            # Pure-JS field filling: identical behaviour in-process and via
            # the daemon pipe (no Playwright locator objects required).
            js_fill = """
            ([formSel, name, value]) => {
                const forms = document.querySelectorAll(formSel);
                let el = null;
                for (const form of forms) {
                    el = form.querySelector('[name="' + name + '"], #' + name)
                      || form.querySelector('[aria-label="' + name + '"]');
                    if (el) break;
                }
                if (!el) el = document.querySelector(
                    '[name="' + name + '"], #' + name + ', [aria-label="' + name + '"]');
                if (!el) return {found: false};
                if (el.offsetParent === null && !(el.type === 'hidden'))
                    return {found: true, hidden: true};
                const tag = el.tagName.toLowerCase();
                if (tag === 'select') {
                    let ok = false;
                    for (const opt of el.options) {
                        if (opt.textContent.trim() === String(value)) {
                            opt.selected = true; ok = true; break;
                        }
                    }
                    if (!ok) return {found: true, hidden: false, error: 'option not found'};
                } else {
                    el.value = String(value);
                }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return {found: true, hidden: false};
            }
            """
            for key, value in fields.items():
                try:
                    res = await browser_manager._safe_evaluate(
                        js_fill, [selector, key, str(value)])
                    if not res or not res.get("found"):
                        skipped.append(f"{key}: not found")
                    elif res.get("hidden"):
                        skipped.append(f"{key}: hidden (possible honeypot) — SKIPPED")
                    elif res.get("error"):
                        skipped.append(f"{key}: {res['error']}")
                    else:
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
            sub_sel = (f'{selector} button[type="submit"], '
                       f'{selector} input[type="submit"]')
            if await browser_manager._locator_count(sub_sel) > 0:
                await browser_manager._locator_click(sub_sel)
            else:
                await browser_manager._locator_submit(selector)
            await browser_manager._settle(timeout_ms=5000)
            browser_manager.link_cache.mark_dirty()
            return ("✅ Form submitted. Current URL: "
                    f"{await browser_manager._current_url()}")
        except Exception as e:
            return f"❌ Submit form error: {e}"


# Start MCP server
if __name__ == "__main__":
    # Windows: pin the SELECTOR event-loop policy before mcp.run() creates
    # its loop.  The default ProactorEventLoop registers inherited std pipe
    # handles with an IOCP completion port; on some systems (the user's
    # Python 3.13 install reproduced it for EVERY detached child) that
    # registration raises WinError 6 and poisons the handles — which is
    # what killed our daemon's stdin/stdout transport.  Selector loops use
    # blocking socket semantics, never touch IOCP, and are fully supported
    # for subprocess pipes in CPython.  Our own MCP stdio transport keeps
    # working: asyncio falls back to thread-based reads/writes for plain
    # console/pipe handles under the selector policy.
    if os.name == "nt":
        try:
            import asyncio.windows_events as _wev
            _wev.set_event_loop_policy(_wev.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    logger.info("Starting FirefoxMCP server...")
    mcp.run(transport="stdio")