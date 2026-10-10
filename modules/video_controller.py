"""
Video controller module for Firefox/Camoufox MCP server.

Provides video player control through video_action() commands.
Supports YouTube, Twitch, Vimeo, and other common video players.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("FirefoxMCP_VideoController")


class VideoController:
    """Controls video players through JS commands."""

    _JS_SNAPSHOT = """
() => {
    const video = document.querySelector('video');
    const state = {
        has_video: !!video,
        url: window.location.href,
        title: document.title,
    };
    if (video) {
        state.src = video.src || '';
        state.currentTime = video.currentTime || 0;
        state.duration = video.duration || 0;
        state.paused = video.paused;
        // NOTE: `||` was a bug here — volume 0.0 (and muted=false) are falsy,
        // so the snapshot reported the fake default 1 instead of the real
        // value. That made every mute/zero-volume state look like "volume
        // changed automatically" in post-action verification. Use ??.
        state.volume = (video.volume != null) ? video.volume : 1;
        state.muted = (video.muted != null) ? video.muted : false;
    }
    return state;
}
"""

    _JS_YOUTUBE_STATE = """
() => {
    // Structural detection only — no i18n-dependent aria-label matching.
    const hasYtPlayer = !!(
        document.querySelector('.ytp-large-play-button, ytd-player, #movie_player')
        || document.querySelector('.ytp-chrome-bottom')
    );
    const state = {
        player_type: hasYtPlayer ? 'youtube' : 'none',
        has_controls: !!document.querySelector('.ytp-play-button'),
        theater_mode: !!document.querySelector('ytd-app.theater-mode, ytd-page-manager[page-type="watch-theater"], html.theater-mode'),
        subtitles_on: false,
        quality_menu_open: false,
        fullscreen: !!document.fullscreenElement,
        wide_mode: false,
    };
    if (hasYtPlayer) {
        // Subtitles: real signal from text tracks / captions container, not live-chat icon.
        const v = document.querySelector('video');
        if (v && v.textTracks) {
            for (let i = 0; i < v.textTracks.length; i++) {
                if (v.textTracks[i].mode === 'showing') { state.subtitles_on = true; break; }
            }
        }
        if (!state.subtitles_on) {
            state.subtitles_on = !!document.querySelector('.ytp-caption-segment');
        }
        state.quality_menu_open = !!document.querySelector('.ytp-settings-menu');
        // Wide/theater: class token is language-independent.
        state.wide_mode = !!document.querySelector('.ytp-large-width-mode, .html5-video-player.ytp-large-width-mode');
    }
    return state;
}
"""

    _JS_TWITCH_STATE = """
() => {
    // Structural detection: Twitch player shell elements.
    const hasTwPlayer = !!(
        document.querySelector('.twilight-player')
        || document.querySelector('[data-a-target="player"]')
        || document.querySelector('.volume-control-icon-container')
    );
    const state = {
        player_type: hasTwPlayer ? 'twitch' : 'none',
        has_controls: !!document.querySelector('.volume-control-icon-container'),
        theater_mode: !!document.querySelector('body.twitch-player--theater, .threaded-chat--theater, .channel-main--theater'),
        subtitles_on: false,
        quality_menu_open: !!document.querySelector('.quality-selection, [data-a-target*="quality"]'),
        fullscreen: !!document.fullscreenElement,
    };
    // NOTE: chat presence (.chat-room) is NOT a subtitles signal — removed as false positive.
    const v = document.querySelector('video');
    if (v && v.textTracks) {
        for (let i = 0; i < v.textTracks.length; i++) {
            if (v.textTracks[i].mode === 'showing') { state.subtitles_on = true; break; }
        }
    }
    return state;
}
"""

    def __init__(self) -> None:
        pass

    async def get_snapshot(self, page: Any) -> Dict[str, Any]:
        """Get current video player state and fingerprint."""
        try:
            state = await page.evaluate(self._JS_SNAPSHOT)
            fingerprint = f"{state.get('url', '')}|{state.get('title', '')}|{state.get('src', '')}"
            return {
                'url': state.get('url', ''),
                'title': state.get('title', ''),
                'has_video': state.get('has_video', False),
                'src': state.get('src', ''),
                'currentTime': state.get('currentTime', 0),
                'duration': state.get('duration', 0),
                'paused': state.get('paused', True),
                'volume': state.get('volume', 1),
                'muted': state.get('muted', False),
                'fingerprint': fingerprint,
            }
        except Exception as e:
            logger.warning(f"Get snapshot error: {e}")
            # NOTE: Playwright Page.title() is an async method — calling it
            # synchronously here previously produced a coroutine repr inside
            # the fingerprint, making every fingerprint unique and breaking
            # the autoplay-change detection. Use safe sync attributes only.
            try:
                url = page.url
            except Exception:
                url = ""
            return {
                'url': url,
                'title': '',
                'has_video': False,
                'fingerprint': f"{url}||",
            }

    _UNKNOWN_PLAYER: Dict[str, Any] = {
        'player_type': 'unknown',
        'has_controls': False,
        'theater_mode': False,
        'subtitles_on': False,
        'quality_menu_open': False,
        'fullscreen': False,
    }

    async def get_player_info(self, page: Any) -> Dict[str, Any]:
        """Get detailed player info including type and capabilities.

        Detection is structural now: each state script returns
        ``player_type: '<name>'`` only when its real player shell exists,
        otherwise ``'none'`` — so a Twitch page can no longer be misreported
        as YouTube (previously the JS constant made every page 'youtube').
        """
        for js in (self._JS_YOUTUBE_STATE, self._JS_TWITCH_STATE):
            try:
                state = await page.evaluate(js)
                ptype = state.get('player_type', 'none')
                if ptype and ptype != 'none':
                    return state
            except Exception as e:
                logger.debug(f"Player state probe error: {e}")
        return dict(self._UNKNOWN_PLAYER)

    async def action(self, page: Any, action: str, param: Optional[str] = None) -> str:
        """Execute video action command."""
        try:
            # Handle cleanup first
            if action == 'cleanup' and param == 'menu':
                cleanup_js = """
                () => {
                    try {
                        const menus = document.querySelectorAll('.ytp-prompt-display-container, .ytp-settings-menu, .ytp-menu-content');
                        menus.forEach(m => m.remove());
                        const btn = document.querySelector('.ytp-settings-button');
                        if (btn && btn.getAttribute('aria-expanded') === 'true') {
                            btn.click();
                        }
                        document.body.focus();
                    } catch(e) {}
                }
                """
                await page.evaluate(cleanup_js)
                return "✅ Menu cleanup completed."

            if action == 'state':
                snapshot = await self.get_snapshot(page)
                player_info = await self.get_player_info(page)
                return json.dumps({**snapshot, **player_info}, indent=2)

            if action == 'download_source':
                js_dl = """
                () => {
                    const video = document.querySelector('video');
                    if (video && video.src) return video.src;
                    return null;
                }
                """
                src = await page.evaluate(js_dl)
                return f"Source URL: {src}" if src else "No source URL found."

            # Video control actions
            js_actions = {
                'play': "() => { const v=document.querySelector('video'); if(v){v.play();} }",
                'pause': "() => { const v=document.querySelector('video'); if(v){v.pause();} }",
                'stop': "() => { const v=document.querySelector('video'); if(v){v.pause();v.currentTime=0;} }",
                'restart': "() => { const v=document.querySelector('video'); if(v){v.currentTime=0;v.play();} }",
            }

            if action in js_actions:
                await page.evaluate(js_actions[action])
                return f"✅ Action '{action}' executed."

            # Parameterized actions
            if action == 'volume_set':
                vol = 0.5
                if param:
                    if param.endswith('%'):
                        vol = float(param[:-1]) / 100.0
                    else:
                        vol = float(param)
                    vol = max(0.0, min(1.0, vol))
                js_vol = f"() => {{ const v=document.querySelector('video'); if(v)v.volume={vol}; }}"
                await page.evaluate(js_vol)
                return f"✅ Volume set to {vol*100}%."

            if action in ['volume_up', 'volume_down']:
                step = float(param) if param else 0.1
                direction = 1 if action == 'volume_up' else -1
                # Parameters passed via page.evaluate arg — never interpolated
                # as Python literals (previous f-string produced `True/False` JS).
                js_vol_change = """
                (opts) => {
                    const v = document.querySelector('video');
                    if (v) {
                        v.volume = opts.dir > 0
                            ? Math.min(1, v.volume + opts.step)
                            : Math.max(0, v.volume - opts.step);
                    }
                }
                """
                await page.evaluate(js_vol_change, {"dir": direction, "step": step})
                return f"✅ Volume {action} executed."

            if action == 'mute':
                js_mute = "() => { const v=document.querySelector('video'); if(v)v.muted=!v.muted; }"
                await page.evaluate(js_mute)
                return "✅ Mute toggle executed."

            if action in ['seek_forward', 'seek_backward']:
                seconds = float(param) if param else 10
                direction = 1 if action == 'seek_forward' else -1
                js_seek = """
                (opts) => {
                    const v = document.querySelector('video');
                    if (v) {
                        v.currentTime = opts.dir > 0
                            ? v.currentTime + opts.seconds
                            : Math.max(0, v.currentTime - opts.seconds);
                    }
                }
                """
                await page.evaluate(js_seek, {"dir": direction, "seconds": seconds})
                return f"✅ Seek {action} by {seconds}s executed."

            if action == 'seek_to':
                if not param:
                    return "❌ seek_to requires a parameter (seconds)."
                js_seek_to = f"() => {{ const v=document.querySelector('video'); if(v)v.currentTime={float(param)}; }}"
                await page.evaluate(js_seek_to)
                return f"✅ Seek to {param}s executed."

            if action == 'jump_minutes':
                if not param:
                    return "❌ jump_minutes requires a parameter (minutes)."
                minutes = int(param)
                js_jump = f"() => {{ const v=document.querySelector('video'); if(v)v.currentTime=v.currentTime+{minutes}*60; }}"
                await page.evaluate(js_jump)
                return f"✅ Jumped {minutes} minutes forward."

            if action in ['fullscreen_enter', 'fullscreen_exit']:
                is_enter = action == 'fullscreen_enter'
                js_fs = """
                (enter) => {
                    if (!document.fullscreenElement && enter) {
                        document.documentElement.requestFullscreen().catch(e=>{});
                    } else if (document.fullscreenElement && !enter) {
                        document.exitFullscreen().catch(e=>{});
                    }
                }
                """
                await page.evaluate(js_fs, is_enter)
                return f"✅ Fullscreen {action} executed."

            if action in ['theater_on', 'theater_off']:
                is_on = action == 'theater_on'
                # Toggle the real YouTube theater button (structural selector,
                # i18n-safe) instead of only adding a CSS class, which left the
                # player visually unchanged until the next user interaction.
                js_theater = """
                (wantOn) => {
                    const app = document.querySelector('ytd-app');
                    const btn = document.querySelector('.ytp-size-button, button.yypToggleButtonSizeButton');
                    const isTheater = !!(app && (app.classList.contains('theater-mode')
                        || document.querySelector('ytd-page-manager[page-type="watch-theater"]')));
                    if (isTheater === wantOn) return 'already';
                    if (btn) { btn.click(); return 'clicked'; }
                    // Fallback: class toggle only when no button found.
                    if (app) {
                        if (wantOn) app.classList.add('theater-mode');
                        else app.classList.remove('theater-mode');
                        return 'class-toggled';
                    }
                    return 'no-player';
                }
                """
                result = await page.evaluate(js_theater, is_on)
                if result == 'no-player':
                    return f"⚠️ Theater mode: no supported player found on this page."
                if result == 'already':
                    return f"✅ Theater mode already {'on' if is_on else 'off'}."
                return f"✅ Theater mode {action} executed ({result})."

            if action == 'wide_mode':
                # F4: real interaction instead of blind clicks. YouTube wide
                # mode is toggled with the Shift+T keyboard shortcut; we focus
                # the player first, press it, then VERIFY via class tokens
                # (i18n-safe) and report the actual resulting state.
                js_focus = """
                () => {
                    const p = document.querySelector('#movie_player');
                    if (p) { try { p.focus(); } catch(e) {} return true; }
                    return false;
                }
                """
                focused = await page.evaluate(js_focus)
                before = await page.evaluate(
                    "() => !!document.querySelector('.ytp-large-width-mode')"
                )
                if focused and hasattr(page, "keyboard"):
                    try:
                        await page.keyboard.down("Shift")
                        await page.keyboard.press("T")
                        await page.keyboard.up("Shift")
                    except Exception as e:
                        logger.warning(f"wide_mode shortcut failed: {e}")
                after = await page.evaluate(
                    "() => !!document.querySelector('.ytp-large-width-mode')"
                )
                if before != after:
                    return f"✅ Wide mode {'on' if after else 'off'} (verified)."
                # Shortcut didn't change state — fall back to button click.
                clicked = await page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('.ytp-button[aria-pressed], .ytp-wide-button')];
                    for (const b of btns) { b.click(); return true; }
                    return false;
                }
                """)
                after2 = await page.evaluate(
                    "() => !!document.querySelector('.ytp-large-width-mode')"
                )
                if after2 != before:
                    return f"✅ Wide mode {'on' if after2 else 'off'} (verified via button)."
                return ("⚠️ Wide mode: state unchanged after attempts "
                        f"(focused={focused}, clicked={clicked}). "
                        "The player may not support wide mode on this page.")

            if action == 'default_size':
                # F4: reset size via verified interactions: exit theater/wide
                # states using structural class tokens only (no i18n labels).
                changed = False
                focused = await page.evaluate("""
                () => {
                    const p = document.querySelector('#movie_player');
                    if (p) { try { p.focus(); } catch(e) {} return true; }
                    return false;
                }
                """)
                if focused and hasattr(page, "keyboard"):
                    try:
                        # 't' resets theater->default; Shift+T toggles wide off
                        await page.keyboard.press("t")
                        if await page.evaluate(
                            "() => !!document.querySelector('.ytp-large-width-mode')"
                        ):
                            await page.keyboard.down("Shift")
                            await page.keyboard.press("T")
                            await page.keyboard.up("Shift")
                    except Exception as e:
                        logger.warning(f"default_size shortcuts failed: {e}")
                theater = await page.evaluate("""
                () => !!(document.querySelector('ytd-app.theater-mode')
                      || document.querySelector('ytd-page-manager[page-type=\"watch-theater\"]'))
                """)
                wide = await page.evaluate(
                    "() => !!document.querySelector('.ytp-large-width-mode')"
                )
                changed = theater or wide
                if changed:
                    # Last-resort class cleanup so UI converges to default.
                    await page.evaluate("""
                    () => {
                        const app = document.querySelector('ytd-app');
                        if (app) app.classList.remove('theater-mode');
                    }
                    """)
                return ("✅ Default size restored." if not changed else
                        "⚠️ Default size: attempted reset; some modes were still active.")

            if action in ['subtitles_on', 'subtitles_off', 'subtitles_toggle']:
                # Single JS pass: toggle reads the CURRENT track mode and inverts it.
                # Previous f-string produced `tracks[i].mode = 'disabled'`-style
                # constants (and once a Python str), which is invalid TextTrackMode.
                wanted = {'subtitles_on': 'showing', 'subtitles_off': 'disabled'}.get(action, 'toggle')
                js_subs = """
                (wanted) => {
                    const video = document.querySelector('video');
                    if (!video) return 'no-video';
                    const tracks = video.textTracks;
                    if (!tracks || tracks.length === 0) return 'no-tracks';
                    // Determine current state: any track showing?
                    let anyShowing = false;
                    for (let i = 0; i < tracks.length; i++) {
                        if (tracks[i].mode === 'showing') { anyShowing = true; break; }
                    }
                    const target = (wanted === 'toggle')
                        ? (anyShowing ? 'disabled' : 'showing')
                        : wanted;
                    let changed = 0;
                    for (let i = 0; i < tracks.length; i++) {
                        if (target === 'showing') {
                            // Enable first available track only; hide others.
                            tracks[i].mode = (i === 0) ? 'showing' : 'hidden';
                        } else {
                            tracks[i].mode = target;
                        }
                        changed++;
                    }
                    return target + ':' + changed;
                }
                """
                result = await page.evaluate(js_subs, wanted)
                if not isinstance(result, str):
                    return "❌ Subtitles action failed: unexpected response from page."
                if result == 'no-video':
                    return "⚠️ No <video> element found on this page."
                if result == 'no-tracks':
                    return ("⚠️ Video has no text tracks loaded. For YouTube captions, "
                            "the CC may need to be enabled via the player's captions button first.")
                mode_name = result.split(':')[0]
                verb = {'subtitles_on': 'on', 'subtitles_off': 'off'}.get(action, mode_name)
                return f"✅ Subtitles {verb} executed."

            if action == 'quality_set':
                quality = param or 'auto'
                # F4/M6: drive the real settings menu with language-neutral
                # clicks (quality labels like '1080p' are not translated),
                # then VERIFY by re-reading the checked/current item.
                # Previously this called nonexistent private APIs and always
                # answered "✅ Quality set" — a false success report.
                js_open_menu = """
                () => {
                    const btn = document.querySelector('.ytp-settings-button');
                    if (!btn) return 'no-player';
                    if (btn.getAttribute('aria-expanded') !== 'true') btn.click();
                    return 'opened';
                }
                """
                opened = await page.evaluate(js_open_menu)
                if opened == 'no-player':
                    return "⚠️ Quality: no supported player/settings button found on this page."

                js_pick = """
                (wanted) => {
                    const norm = s => (s || '').toLowerCase().replace(/\\s+/g, '');
                    const items = [...document.querySelectorAll(
                        '.ytp-quality-menu .ytp-menuitem, .ytp-settings-menu .ytp-menuitem')];
                    if (!items.length) return 'menu-not-ready';
                    // Exclusion rule (SKILL.md): never pick Premium/Premium Trail
                    // unless explicitly requested.
                    const allowPremium = /premium/i.test(wanted);
                    let target = null;
                    for (const it of items) {
                        const label = (it.querySelector('.ytp-menuitem-label')?.textContent || '') + ' ' +
                                      (it.getAttribute('aria-label') || '');
                        if (/premium/i.test(label) && !allowPremium) continue;
                        const l = norm(label);
                        const w = norm(wanted);
                        if (l === w || l.startsWith(w) || l.includes(w)) { target = it; break; }
                    }
                    if (!target) return 'not-found';
                    target.click();
                    return 'clicked';
                }
                """
                picked = await page.evaluate(js_pick, quality)
                if picked == 'menu-not-ready':
                    return ("⚠️ Quality: settings menu did not render a quality list. "
                            "Retry after video_check(), or interact manually.")
                if picked == 'not-found':
                    options = await page.evaluate("""
                    () => [...document.querySelectorAll(
                        '.ytp-quality-menu .ytp-menuitem .ytp-menuitem-label, ' +
                        '.ytp-settings-menu .ytp-menuitem .ytp-menuitem-label')]
                        .map(e => e.textContent.trim()).filter(Boolean)
                    """)
                    return (f"⚠️ Quality '{quality}' not available. Available options: "
                            f"{', '.join(options) if options else '(none listed)'}. "
                            "Premium options are skipped unless requested explicitly.")

                # Verification pass: read back the currently selected quality.
                verified = await page.evaluate("""
                () => {
                    const sel = document.querySelector(
                        '.ytp-quality-menu .ytp-menuitem[aria-checked="true"] .ytp-menuitem-label, ' +
                        '.ytp-settings-menu .ytp-menuitem[aria-checked="true"] .ytp-menuitem-label');
                    const shown = document.querySelector('.ytp-quality-label, .ytp-settings-button[aria-label]');
                    return sel ? sel.textContent.trim() : (shown ? shown.textContent.trim() : null);
                }
                """)
                if verified and quality.lower().replace(" ", "") in verified.lower().replace(" ", ""):
                    return f"✅ Quality set to '{verified}' (verified from menu state)."
                return (f"⚠️ Quality click executed, but verification shows "
                        f"'{verified or 'unknown'}' instead of '{quality}'. "
                        "Confirm with video_check() before reporting success.")

            if action == 'set_speed':
                if not param:
                    return "❌ set_speed requires a parameter (e.g., 1.5, 2.0)."
                speed = float(param)
                js_speed = f"() => {{ const v=document.querySelector('video'); if(v)v.playbackRate={speed}; }}"
                await page.evaluate(js_speed)
                return f"✅ Speed set to {speed}x."

            return f"❌ Unknown action: {action}"

        except Exception as e:
            logger.error(f"Video action error: {e}", exc_info=True)
            return f"❌ Video action error: {e}"