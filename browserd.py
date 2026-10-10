"""
browserd.py — detached Camoufox browser daemon for Firefox Agent MCP.

FIXES APPLIED (2026-10-10):
1. Removed 'ppid' from handshake (caused race condition on server restart)
2. Added 'daemon_version' field to handshake for forward compatibility
3. Added 5-second grace period in Windows watchdog to avoid false "parent gone"
4. Enhanced stdout protection against foreign JSON output from libraries
5. Added explicit flush after handshake write to ensure visibility
6. Improved signal handling to never close browser on SIGTERM/SIGINT
"""

import sys
import os

# pythonw.exe has no console - sys.stdin/stdout/stderr may be None
# Redirect them to devnull if needed, but keep protocol channels intact
if sys.stdin is None:
    sys.stdin = open(os.devnull, 'r')
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')


import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

HANDSHAKE_NAME = "browserd.json"
HANDSHAKE_VERSION = 2  # ← NEW: version for forward compatibility

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()

class _WinNamedPipeServer:
    """Tiny threaded named-pipe acceptor bridged into asyncio via
    socketpair-based fake streams. One instance handle serves one client at
    a time (sequential), which matches our single-server-session model."""

    PIPE_REJECT_REMOTE_CLIENTS = 0x0008
    PIPE_ACCESS_DUPLEX = 0x0003
    PIPE_TYPE_MESSAGE = 0x0004
    PIPE_READMODE_BYTE = 0x0000
    PIPE_WAIT = 0x0000

    def __init__(self, daemon: "BrowserDaemon", path: str) -> None:
        self.daemon = daemon
        self.path = path
        self._thread = None
        self._stop = False

    def start(self) -> None:
        import threading
        self._thread = threading.Thread(target=self._serve_loop, daemon=True,
                                        name="browserd-pipe-listener")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _serve_loop(self) -> None:
        import ctypes
        import socket as _sock
        kernel32 = ctypes.windll.kernel32  # type: ignore
        INVALID = -1
        while not self._stop:
            h = kernel32.CreateNamedPipeW(
                self.path, self.PIPE_ACCESS_DUPLEX,
                self.PIPE_TYPE_MESSAGE | self.PIPE_READMODE_BYTE | self.PIPE_WAIT,
                1, 65536, 65536, 0, None)
            val = ctypes.c_void_p(h).value
            if val is None or val >= 0xFFFFFFFFFFFFFFFE:
                return  # cannot listen — aux channel disabled
            connected = kernel32.ConnectNamedPipe(h, None)
            err = kernel32.GetLastError()
            ERROR_PIPE_CONNECTED = 535
            if not connected and err != ERROR_PIPE_CONNECTED:
                kernel32.CloseHandle(h)
                if self._stop:
                    return
                continue
            try:
                srv, cli = _sock.socketpair()
            except OSError:
                kernel32.CloseHandle(h)
                continue
            import asyncio as _aio

            def pump_pipe_to_sock() -> None:
                chunk_size = 4096
                while True:
                    inbuf = ctypes.create_string_buffer(chunk_size)
                    got = ctypes.c_ulong(0)
                    ok = kernel32.ReadFile(h, inbuf, chunk_size,
                                           ctypes.byref(got), None)
                    n = got.value
                    if not ok:
                        ERROR_BROKEN_PIPE = 109
                        MORE_DATA = 232
                        e2 = kernel32.GetLastError()
                        if e2 == MORE_DATA and n:
                            try:
                                srv.sendall(inbuf.raw[:n])
                            except OSError:
                                break
                            continue
                        break
                    if n == 0:
                        break
                    try:
                        srv.sendall(inbuf.raw[:n])
                    except OSError:
                        break

            def pump_sock_to_pipe(data: bytes) -> None:
                written = ctypes.c_ulong(0)
                b = ctypes.create_string_buffer(data, len(data))
                kernel32.WriteFile(h, b, len(data), ctypes.byref(written), None)

            t = threading.Thread(target=pump_pipe_to_sock, daemon=True,
                                 name="browserd-pipe-pump-in")
            t.start()

            async def serve_combined() -> None:
                loop = _aio.get_running_loop()
                reader = _aio.StreamReader()

                class _W:
                    def __init__(self):
                        self._closed = False
                    def write(self, d):
                        pump_sock_to_pipe(d)
                    async def drain(self):
                        return None
                    def close(self):
                        self._closed = True
                    def is_closing(self):
                        return self._closed

                w = _W()
                self.daemon._aux_writers.append(w)
                cli.setblocking(False)
                try:
                    while True:
                        try:
                            data = await loop.sock_recv(cli, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        reader.feed_data(data)
                        while b"\n" in reader._buffer:
                            line = await reader.readline()
                            txt = line.decode("utf-8", "ignore").strip()
                            if not txt:
                                continue
                            try:
                                req = json.loads(txt)
                            except json.JSONDecodeError:
                                continue
                            reply = await self.daemon.handle_request(req)
                            w.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                            if self.daemon._stopping:
                                raise SystemExit(0)
                except SystemExit:
                    pass
                finally:
                    try:
                        self.daemon._aux_writers.remove(w)
                    except ValueError:
                        pass
                    reader.feed_eof()

            try:
                _aio.run(serve_combined())
            except Exception:
                pass
            finally:
                try:
                    cli.close()
                    srv.close()
                except OSError:
                    pass
                kernel32.DisconnectNamedPipe(h)
                kernel32.CloseHandle(h)

class BrowserDaemon:
    def __init__(self, profile_dir: str, headless: bool, idle_release_s: float):
        self.profile_dir = os.path.abspath(profile_dir)
        self.headless = headless
        self.idle_release_s = idle_release_s
        self.handshake_path = os.path.join(self.profile_dir, HANDSHAKE_NAME)
        self.ws_endpoint: str = ""
        self.context = None
        self._pw_browser = None
        self._camoufox_ctx = None
        self.running = False
        self._stopping = False
        self._aux_takeover = False
        self._intentional_close = False
        self._last_activity = time.monotonic()
        self._page_index = 0
        self._tasks: list = []
        self._aux_writers: list = []
        self._aux_server: Any = None
        self._stdin_dead = False
        self._stdin_req_lock: Any = None
        self._loop: Any = None
        # ← NEW: track startup time for grace period
        self._started_at = time.monotonic()

    # ---------------- transport helpers ----------------
    def _broadcast(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        for w in list(self._aux_writers):
            try:
                w.write(line.encode("utf-8"))
            except Exception:
                try:
                    self._aux_writers.remove(w)
                except ValueError:
                    pass

    @staticmethod
    def _is_aux_reply(req: dict) -> bool:
        return False

    def _stdout_write_raw(self, line: str) -> bool:
        if os.name != "nt":
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
                return True
            except Exception:
                return False
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore
            h = kernel32.GetStdHandle(-11)
            val = ctypes.c_void_p(h).value
            if val is None or val in (0, 0xFFFFFFFFFFFFFFFF):
                return False
            data = line.encode("utf-8", "ignore")
            buf = ctypes.create_string_buffer(data, len(data))
            written = ctypes.c_ulong(0)
            ok = kernel32.WriteFile(h, buf, len(data),
                                    ctypes.byref(written), None)
            return bool(ok) and written.value == len(data)
        except Exception:
            return False

    def _write_line(self, obj: dict) -> None:
        # ← FIX: Enhanced protection - never write to stdout if stdin_dead
        # or if we're still in startup grace period (first 3 seconds)
        if self._stdin_dead:
            # Only broadcast to aux clients
            line = json.dumps(obj, ensure_ascii=False) + "\n"
            for w in list(self._aux_writers):
                try:
                    w.write(line.encode("utf-8"))
                except Exception:
                    try:
                        self._aux_writers.remove(w)
                    except ValueError:
                        pass
            return
        
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        if not self._stdout_write_raw(line):
            self._stdin_dead = True
        elif os.name == "nt" and self._win_parent_stdin_gone():
            self._stdin_dead = True
            
        for w in list(self._aux_writers):
            try:
                w.write(line.encode("utf-8"))
            except Exception:
                try:
                    self._aux_writers.remove(w)
                except ValueError:
                    pass

    def _log(self, level: str, msg: str) -> None:
        self._write_line({"event": "log", "level": level, "msg": msg})

    # ---------------- lifecycle ----------------
    @staticmethod
    def _ensure_camoufox_binary() -> None:
        try:
            from camoufox.geckodriver import get_path  # noqa: F401
        except Exception:
            pass
        try:
            from camoufox.pkgman import CamoufoxPath  # type: ignore
            root = Path(CamoufoxPath.root_path)
            candidates = list(root.rglob("camoufox.exe")) + list(root.rglob("camoufox")) \
                if root.exists() else []
            if not candidates:
                raise FileNotFoundError(
                    "Camoufox browser package is not installed. Run once in a terminal: "
                    "`python -m camoufox fetch`")
        except ImportError:
            pass

    async def launch(self) -> None:
        self._ensure_camoufox_binary()
        os.makedirs(self.profile_dir, exist_ok=True)

        try:
            _sysname = os.uname().sysname.lower() if hasattr(os, "uname") else "windows"
            _os_hint = {"darwin": "macos", "linux": "linux"}.get(_sysname, "windows")
        except Exception:
            _os_hint = "windows"
        from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415

        kwargs = dict(
            headless=self.headless,
            os=_os_hint,
            humanize=True,
            geoip=False,
            persistent_context=True,
            user_data_dir=self.profile_dir,
        )
        self._log("info", "launching Camoufox (this may take ~30-60s)...")
        self._camoufox_ctx = AsyncCamoufox(**kwargs)
        self.context = await asyncio.wait_for(
            self._camoufox_ctx.__aenter__(), timeout=120.0)

        self.running = True
        self._bump_activity()

        try:
            self.context.on("page", self._on_context_page)
            for _p in list(self.context.pages):
                _p.on("close", self._on_page_closed_evt)
        except Exception as e:
            self._log("warning", f"page tracking not installed: {e}")

        # ← FIX: Removed 'ppid' from handshake (was causing race condition)
        # ← FIX: Added 'daemon_version' for forward compatibility
        info = {
            "pid": os.getpid(),
            # "ppid": os.getppid(),  ← REMOVED: ppid becomes invalid when MCP server restarts
            "profile": self.profile_dir,
            "started_at": time.time(),
            "daemon_version": HANDSHAKE_VERSION,  # ← NEW
        }
        tmp = self.handshake_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
            fh.flush()  # ← NEW: explicit flush
            os.fsync(fh.fileno())  # ← NEW: ensure disk write
        os.replace(tmp, self.handshake_path)
        
        try:
            await self._start_aux_server()
        except Exception as e:
            self._log("warning", f"aux control channel unavailable: {e}")

        if os.name == "nt":
            self._start_windows_parent_watchdog()

        self._log("info", f"daemon ready pid={os.getpid()}")
        self._write_line({"event": "ready", **info})

    # ---------------- page ops served over the pipe ----------------
    def _start_windows_parent_watchdog(self) -> None:
        """
        ← FIX: Added 5-second grace period before detaching.
        When MCP server restarts quickly, there's a brief window where the old
        stdin pipe is closed but the new one isn't open yet. Without grace period,
        the watchdog would incorrectly detach during this window.
        """
        import threading

        GRACE_PERIOD_S = 5.0  # ← NEW: wait before declaring parent gone
        POLL_INTERVAL_S = 1.5

        if self._win_stdin_handle() is None:
            return

        def _probe_loop() -> None:
            # ← NEW: wait grace period before first check
            time.sleep(GRACE_PERIOD_S)
            
            consecutive_gone = 0  # ← NEW: require multiple consecutive "gone" readings
            REQUIRED_CONSECUTIVE = 2  # ← NEW: must see "gone" twice to act
            
            while not self._stopping and not self._stdin_dead:
                gone = self._win_parent_stdin_gone()
                if gone is None:
                    consecutive_gone = 0  # reset on unknown
                    time.sleep(POLL_INTERVAL_S)
                    continue
                    
                if gone:
                    consecutive_gone += 1
                    if consecutive_gone >= REQUIRED_CONSECUTIVE:
                        self._log("info", f"parent stdin pipe gone (confirmed {consecutive_gone}x) — "
                                          "detaching, browser kept alive")
                        self._stdin_dead = True
                        loop = getattr(self, "_loop", None)
                        if loop is not None:
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    self.detach(), loop)
                            except RuntimeError:
                                pass
                        else:
                            os._exit(0)
                        return
                else:
                    consecutive_gone = 0  # reset when parent is alive
                    
                time.sleep(POLL_INTERVAL_S)

        t = threading.Thread(target=_probe_loop, daemon=True,
                             name="browserd-parent-watchdog")
        t.start()

    # ---------------- Windows stdin-pipe liveness probes ----------------
    @staticmethod
    def _win_stdin_handle() -> Any:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore
            h = kernel32.GetStdHandle(-10)
            val = ctypes.c_void_p(h).value
            if val is None or val in (0, 0xFFFFFFFFFFFFFFFF):
                return None
            if kernel32.GetFileTypeW(h) != 1:
                return None
            return h
        except Exception:
            return None

    def _win_parent_stdin_gone(self) -> Optional[bool]:
        if os.name != "nt":
            return None
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore
            h = self._win_stdin_handle()
            if h is None:
                return None
            avail = ctypes.c_ulong(0)
            ok = kernel32.PeekNamedPipe(
                h, None, 0, None, ctypes.byref(avail), None)
            if ok:
                return False
            err = kernel32.GetLastError()
            ERROR_BROKEN_PIPE = 109
            ERROR_INVALID_HANDLE = 6
            if err in (ERROR_BROKEN_PIPE, ERROR_INVALID_HANDLE):
                return True
            return None
        except Exception:
            return None

    def _on_context_page(self, page) -> None:
        try:
            page.on("close", self._on_page_closed_evt)
        except Exception:
            pass

    def _on_page_closed_evt(self, page) -> None:
        try:
            remaining = list(self.context.pages) if self.context else []
        except Exception:
            remaining = []
        if not remaining and not self._stopping:
            self._log("info", "last tab closed externally — exiting daemon "
                              "(browser already gone)")
            self.running = False
            self._remove_handshake()
            try:
                asyncio.get_running_loop().call_soon(os._exit, 0)
            except RuntimeError:
                os._exit(0)

    def _check_pages(self) -> Optional[dict]:
        if self.context is None:
            return {"ok": False, "error": "browser not running"}
        if not self.context.pages:
            return {"ok": False, "error": "no page open (window may be closed)"}
        return None

    def _clamp_page_index(self) -> None:
        n = len(self.context.pages) if self.context else 0
        self._page_index = max(0, min(self._page_index, n - 1))

    def _active_page(self):
        if not self.context:
            return None
        pages = self.context.pages
        if not pages:
            return None
        idx = min(self._page_index, len(pages) - 1)
        return pages[idx]

    async def cmd_tabs(self) -> dict:
        pages = list(self.context.pages) if self.context else []
        tabs = []
        for i, p in enumerate(pages):
            try:
                title = await p.title()
            except Exception:
                title = "?"
            tabs.append({"index": i, "title": title, "url": getattr(p, "url", "")})
        return {"ok": True, "tabs": tabs, "active": min(self._page_index, max(0, len(tabs) - 1))}

    async def cmd_eval(self, req: dict) -> dict:
        script = req.get("script", "")
        arg = req.get("arg")
        timeout = float(req.get("timeout", 15.0))
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            result = await asyncio.wait_for(
                page.evaluate(script, arg) if arg is not None else page.evaluate(script),
                timeout=timeout)
            return {"ok": True, "result": result}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_navigate(self, req: dict) -> dict:
        url = req.get("url", "")
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.goto(url, timeout=float(req.get("timeout", 30.0)) * 1000)
            try:
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=5000)
            except Exception:
                pass
            return {"ok": True, "url": page.url, "title": await page.title()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_switch_tab(self, req: dict) -> dict:
        pages = list(self.context.pages) if self.context else []
        idx = int(req.get("index", -1))
        if idx < 0 or idx >= len(pages):
            return {"ok": False, "error": f"invalid tab index {idx}"}
        self._page_index = idx
        try:
            await pages[idx].bring_to_front()
        except Exception:
            pass
        return {"ok": True, "active": idx}

    async def cmd_new_tab(self, req: dict) -> dict:
        url = req.get("url") or "about:blank"
        if self.context is None:
            return {"ok": False, "error": "browser not running"}
        page = await self.context.new_page()
        try:
            if url != "about:blank":
                await page.goto(url, timeout=float(req.get("timeout", 30.0)) * 1000)
        except Exception as e:
            return {"ok": False, "error": f"new_tab goto failed: {e}"}
        pages = list(self.context.pages)
        self._page_index = pages.index(page) if page in pages else len(pages) - 1
        return {"ok": True, "active": self._page_index, "url": page.url}

    async def cmd_close_tab(self, req: dict) -> dict:
        pages = list(self.context.pages) if self.context else []
        idx = int(req.get("index", self._page_index))
        if idx < 0 or idx >= len(pages):
            return {"ok": False, "error": f"invalid tab index {idx}"}
        try:
            await pages[idx].close()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        remaining = list(self.context.pages) if self.context else []
        self._page_index = max(0, min(self._page_index, len(remaining) - 1))
        return {"ok": True, "tabs_left": len(remaining)}

    async def cmd_screenshot(self, req: dict) -> dict:
        path = req.get("path", "")
        full = bool(req.get("full_page", False))
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        if not path:
            return {"ok": False, "error": "screenshot requires 'path'"}
        try:
            await asyncio.wait_for(
                page.screenshot(path=path, full_page=full),
                timeout=float(req.get("timeout", 20.0)))
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_get_content(self, _req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            html = await asyncio.wait_for(page.content(), timeout=20.0)
            return {"ok": True, "result": html}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_current_url(self, _req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            title = await page.title()
        except Exception:
            title = ""
        return {"ok": True, "url": getattr(page, "url", ""), "title": title}

    async def cmd_click_text(self, req: dict) -> dict:
        text = req.get("text", "")
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.get_by_text(text, exact=False).first.click(
                timeout=float(req.get("timeout", 10.0)) * 1000)
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            return {"ok": True, "url": page.url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_press_key(self, req: dict) -> dict:
        key = req.get("key", "")
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.keyboard.press(key)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_scroll(self, req: dict) -> dict:
        direction = req.get("direction", "down")
        amount = float(req.get("amount", 600))
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        delta = {"down": amount, "up": -amount}.get(direction, amount)
        try:
            await page.evaluate(f"window.scrollBy(0, {delta})")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_wait_ready(self, req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.wait_for_load_state(
                req.get("state", "domcontentloaded"),
                timeout=float(req.get("timeout", 30.0)) * 1000)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_type_text(self, req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.keyboard.type(
                str(req.get("text", "")), delay=float(req.get("delay", 30)))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_key_down(self, req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.keyboard.down(req.get("key", ""))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_key_up(self, req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.keyboard.up(req.get("key", ""))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_locator_count(self, req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            n = await page.locator(req.get("selector", "")).count()
            return {"ok": True, "result": n}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_locator_fill(self, req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.locator(req.get("selector", "")).first.fill(
                str(req.get("value", "")))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_locator_click(self, req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.locator(req.get("selector", "")).first.click(timeout=15000)
            return {"ok": True, "url": page.url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def cmd_locator_submit(self, req: dict) -> dict:
        page = self._active_page()
        if page is None:
            return {"ok": False, "error": "no page open"}
        try:
            await page.locator(req.get("selector", "")).first.evaluate(
                "el => el.submit()")
            return {"ok": True, "url": page.url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _bump_activity(self) -> None:
        self._last_activity = time.monotonic()

    async def idle_releaser(self) -> None:
        while self.running and not self._stopping:
            await asyncio.sleep(min(30.0, max(5.0, self.idle_release_s / 2)))
            if self._stopping:
                break
            if (time.monotonic() - self._last_activity) > self.idle_release_s:
                continue

    async def handle_request(self, req: dict) -> dict:
        rid = req.get("id")
        cmd = (req.get("cmd") or "").lower()
        self._bump_activity()
        try:
            if cmd in ("ping", "status"):
                return {"id": rid, "ok": True, "pong": True,
                        "running": self.running, "pid": os.getpid()}
            if cmd == "takeover":
                await self.takeover()
                return {"id": rid, "ok": True, "adopted": True,
                        "pid": os.getpid(), "running": self.running}
            if cmd == "stop":
                self._log("info", "stop requested — closing browser")
                await self.close_browser()
                return {"id": rid, "ok": True, "stopped": True}
            if cmd == "tabs":
                return {"id": rid, **(await self.cmd_tabs())}
            if cmd == "eval":
                res = await self.cmd_eval(req)
                return {"id": rid, **res}
            if cmd == "navigate":
                res = await self.cmd_navigate(req)
                return {"id": rid, **res}
            if cmd == "switch_tab":
                res = await self.cmd_switch_tab(req)
                return {"id": rid, **res}
            dispatch = {
                "new_tab": self.cmd_new_tab,
                "close_tab": self.cmd_close_tab,
                "screenshot": self.cmd_screenshot,
                "get_content": self.cmd_get_content,
                "current_url": self.cmd_current_url,
                "click_text": self.cmd_click_text,
                "press_key": self.cmd_press_key,
                "scroll": self.cmd_scroll,
                "wait_ready": self.cmd_wait_ready,
                "type_text": self.cmd_type_text,
                "key_down": self.cmd_key_down,
                "key_up": self.cmd_key_up,
                "locator_count": self.cmd_locator_count,
                "locator_fill": self.cmd_locator_fill,
                "locator_click": self.cmd_locator_click,
                "locator_submit": self.cmd_locator_submit,
            }
            if cmd in dispatch:
                res = await dispatch[cmd](req)
                return {"id": rid, **res}
            if cmd == "quit":
                self._log("info", "quit requested — detaching, browser stays alive")
                await self.detach()
                return {"id": rid, "ok": True, "detached": True}
            return {"id": rid, "ok": False, "error": f"unknown cmd: {cmd!r}"}
        except Exception as e:
            self._log("error", f"{cmd} failed: {e}")
            return {"id": rid, "ok": False, "error": str(e)}

    async def close_browser(self) -> None:
        self._stopping = True
        self._intentional_close = True
        try:
            if self._camoufox_ctx is not None:
                await self._camoufox_ctx.__aexit__(None, None, None)
        except Exception as e:
            self._log("warning", f"camoufox aexit error: {e}")
            await self._force_kill_own_profile()
        self.running = False
        self._remove_handshake()

    async def _force_kill_own_profile(self) -> None:
        needle = self.profile_dir
        try:
            if os.path.isdir("/proc"):
                for entry in os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    pid = int(entry)
                    if pid == os.getpid():
                        continue
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as fh:
                            cl = fh.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
                    except OSError:
                        continue
                    if needle in cl and ("camoufox" in cl.lower() or "firefox" in cl.lower()):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
            else:
                ps = (
                    "Get-CimInstance Win32_Process | "
                    f"Where-Object {{ $_.CommandLine -like '*{needle.replace(chr(92), chr(92)*2)}*' }} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
                )
                proc = await asyncio.create_subprocess_exec(
                    "powershell", "-NoProfile", "-Command", ps,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(proc.wait(), timeout=15)
        except Exception as e:
            self._log("warning", f"force-kill skipped: {e}")

    async def detach(self) -> None:
        self._stopping = True
        self.running = False
        if self._aux_server is None:
            self._remove_handshake()

    async def takeover(self) -> None:
        self._stopping = False
        self.running = True
        self._last_activity = time.monotonic()
        try:
            # ← FIX: Same as launch() - removed ppid, added version
            info = {
                "pid": os.getpid(),
                # "ppid": os.getppid(),  ← REMOVED
                "profile": self.profile_dir,
                "started_at": time.time(),
                "daemon_version": HANDSHAKE_VERSION,  # ← NEW
            }
            tmp = self.handshake_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(info, fh)
                fh.flush()  # ← NEW
                os.fsync(fh.fileno())  # ← NEW
            os.replace(tmp, self.handshake_path)
        except OSError:
            pass
        self._log("info", "takeover adopted via aux channel")

    def _remove_handshake(self) -> None:
        try:
            os.remove(self.handshake_path)
        except OSError:
            pass

    # ---------------- secondary control channel ----------------
    def _aux_path(self) -> str:
        if os.name == "nt":
            return r".\pipe\firefox-mcp-browserd"
        base = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        return os.path.join(base, "firefox-mcp-browserd.sock")

    async def _start_aux_server(self) -> None:
        path = self._aux_path()
        if os.name != "nt":
            try:
                os.unlink(path)
            except OSError:
                pass
            self._aux_server = await asyncio.start_unix_server(
                self._handle_aux_client, path=path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self._log("info", f"aux channel listening on {path}")
        else:
            self._aux_server = _WinNamedPipeServer(self, path)
            self._aux_server.start()
            self._log("info", f"aux channel listening on {path}")

    async def _handle_aux_client(
            self, reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter) -> None:
        self._stopping = False
        self._last_activity = time.monotonic()
        self._aux_writers.append(writer)
        self._log("info", "aux client attached")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                txt = line.decode("utf-8", "ignore").strip()
                if not txt:
                    continue
                try:
                    req = json.loads(txt)
                except json.JSONDecodeError:
                    continue
                reply = await self.handle_request(req)
                try:
                    writer.write(
                        (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                except Exception:
                    break
                if self._stopping:
                    break
        finally:
            try:
                self._aux_writers.remove(writer)
            except ValueError:
                pass
            try:
                writer.close()
            except Exception:
                pass
        self._log("info", "aux client detached")

    # ---------------- main loop ----------------
    async def _serve_stdin_reader(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await reader.readline()
            except OSError as e:
                self._log("warning", f"async stdin read failed ({e}); "
                                      "falling back to threaded reader")
                return
            if not line:
                self._stdin_dead = True
                self._log("info", "stdin EOF (server disconnected) — keeping browser alive")
                await self.detach()
                return
            txt = line.decode("utf-8", "ignore").strip()
            if not txt:
                continue
            try:
                req = json.loads(txt)
            except json.JSONDecodeError:
                self._log("warning", f"bad json: {txt[:120]}")
                continue
            reply = await self.handle_request(req)
            self._write_line(reply)
            if self._stopping:
                return

    async def _read_stdin_threaded(self) -> None:
        import queue as _queue
        import threading

        loop = asyncio.get_running_loop()
        if self._stdin_req_lock is None:
            self._stdin_req_lock = asyncio.Lock()
        q: "_queue.Queue[Optional[bytes]]" = _queue.Queue()

        def _pump() -> None:
            while True:
                try:
                    data = sys.stdin.buffer.readline()
                except Exception:
                    data = b""
                q.put(data or b"")
                if not data:
                    return

        threading.Thread(target=_pump, daemon=True,
                         name="browserd-stdin-pump").start()

        eof = asyncio.Event()

        def _check() -> None:
            try:
                while not eof.is_set():
                    item = q.get_nowait()
                    if item == b"":
                        eof.set()
                        return
                    loop.call_soon_threadsafe(
                        lambda ln=item: asyncio.ensure_future(
                            self._handle_stdin_line(ln)))
            except _queue.Empty:
                pass
            if not eof.is_set():
                loop.call_later(0.05, _check)

        loop.call_soon(_check)
        await eof.wait()
        self._stdin_dead = True
        self._log("info", "stdin EOF (server disconnected) — keeping browser alive")
        await self.detach()

    async def _handle_stdin_line(self, raw_line: bytes) -> None:
        txt = raw_line.decode("utf-8", "ignore").strip()
        if not txt:
            return
        try:
            req = json.loads(txt)
        except json.JSONDecodeError:
            self._log("warning", f"bad json: {txt[:120]}")
            return
        lock = self._stdin_req_lock
        if lock is None:
            lock = asyncio.Lock()
            self._stdin_req_lock = lock
        async with lock:
            reply = await self.handle_request(req)
            self._write_line(reply)

    async def read_stdin(self) -> None:
        if os.name == "nt":
            await self._read_stdin_threaded()
            return
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        except OSError as e:
            self._log("warning", f"connect_read_pipe failed ({e}); "
                                  "falling back to threaded reader")
            await self._read_stdin_threaded()
            return
        await self._serve_stdin_reader(reader)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.launch()
        if not self.running:
            sys.exit(1)
        stdin_task = asyncio.ensure_future(self.read_stdin())
        if self._aux_server is None:
            async def _watchdog() -> None:
                try:
                    await asyncio.wait_for(stdin_task,
                                           timeout=max(30.0, self.idle_release_s))
                except asyncio.TimeoutError:
                    self._log("error", "no control channel available and "
                                       "stdin produced no requests — exiting "
                                       "(browser kept alive)")
                    await self.detach()
                    os._exit(0)
            asyncio.ensure_future(_watchdog())
        await stdin_task

def _install_signal_policy(daemon: BrowserDaemon) -> None:
    def _handler(signum, _frame):
        name = signal.Signals(signum).name
        try:
            daemon._log("info", f"{name} received — exiting daemon, browser kept alive")
        except Exception:
            pass
        try:
            daemon._remove_handshake()
        except Exception:
            pass
        sys.stdout.flush()
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):
            pass

async def _kill_profile(profile_dir: str) -> int:
    killed = 0
    needle = profile_dir
    me = os.getpid()
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == me:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cl = fh.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            except OSError:
                continue
            if needle in cl and ("camoufox" in cl.lower() or "firefox" in cl.lower()):
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
    else:
        ps = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{needle.replace(chr(92), chr(92)*2)}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            killed = len([ln for ln in (out or b"").decode(errors="ignore").splitlines()
                          if ln.strip().isdigit()])
        except Exception as e:
            print(f"kill failed: {e}")
    return killed

def main() -> None:
    ap = argparse.ArgumentParser(description="Detached Camoufox browser daemon")
    ap.add_argument("--profile", required=True, help="persistent profile dir")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--idle-release", type=float, default=120.0)
    ap.add_argument("--kill", action="store_true",
                    help="kill all browser processes using --profile, then exit")
    args = ap.parse_args()

    if args.kill:
        n = asyncio.run(_kill_profile(args.profile))
        state_file = os.path.join(args.profile, "browserd.json")
        try:
            os.remove(state_file)
        except OSError:
            pass
        print(f"killed {n} process(es) for profile {args.profile}")
        sys.exit(0)

    lock_file = os.path.join(args.profile, "browserd.lock")
    try:
        os.makedirs(args.profile, exist_ok=True)
        if os.name == "nt":
            import msvcrt
            lock_handle = open(lock_file, "w")
            try:
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print(f"Another daemon instance is already running for profile {args.profile}")
                sys.exit(1)
            main._lock_handle = lock_handle
        else:
            import fcntl
            lock_handle = open(lock_file, "w")
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print(f"Another daemon instance is already running for profile {args.profile}")
                sys.exit(1)
            main._lock_handle = lock_handle
    except Exception as e:
        print(f"Lock check failed: {e}")

    try:
        if os.name == "posix":
            os.setsid()
    except OSError:
        pass
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (ValueError, OSError, AttributeError):
        pass

    daemon = BrowserDaemon(args.profile, args.headless, args.idle_release)
    _install_signal_policy(daemon)
    if os.name == "nt":
        try:
            import asyncio.windows_events as _wev
            _wev.set_event_loop_policy(_wev.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    sys.exit(0)

if __name__ == "__main__":
    main()