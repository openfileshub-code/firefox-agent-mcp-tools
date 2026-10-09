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
        state.volume = video.volume || 1;
        state.muted = video.muted || false;
    }
    return state;
}
"""

    _JS_YOUTUBE_STATE = """
() => {
    const state = {
        player_type: 'youtube',
        has_controls: !!document.querySelector('.ytp-play-button'),
        theater_mode: !!document.querySelector('ytd-app.theater-mode'),
        subtitles_on: false,
        quality_menu_open: false,
        fullscreen: !!document.fullscreenElement,
    };
    const ytPlayer = document.querySelector('ytd-player') || document.querySelector('video');
    if (ytPlayer) {
        state.subtitles_on = !!document.querySelector('.ytp-live-chat-button-paper-icon.ytp-button.ytp-pause-mode');
        state.quality_menu_open = !!document.querySelector('.ytp-prompt-display-container');
    }
    const c = document.querySelector('.ytp-size-button');
    if (c) {
        state.wide_mode = c.getAttribute('aria-label') === 'Воспроизводить в широком формате';
    }
    return state;
}
"""

    _JS_TWITCH_STATE = """
() => {
    const state = {
        player_type: 'twitch',
        has_controls: !!document.querySelector('.volume-control-icon-container'),
        theater_mode: !!document.querySelector('body.twitch-player--theater'),
        subtitles_on: false,
        quality_menu_open: false,
        fullscreen: !!document.fullscreenElement,
    };
    const video = document.querySelector('video');
    if (video) {
        state.subtitles_on = !!document.querySelector('.chat-room');
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
            return {
                'url': page.url,
                'title': page.title,
                'has_video': False,
                'fingerprint': f"{page.url}|{page.title}|",
            }

    async def get_player_info(self, page: Any) -> Dict[str, Any]:
        """Get detailed player info including type and capabilities."""
        try:
            yt_state = await page.evaluate(self._JS_YOUTUBE_STATE)
            if yt_state.get('player_type') == 'youtube':
                return yt_state
            tw_state = await page.evaluate(self._JS_TWITCH_STATE)
            if tw_state.get('player_type') == 'twitch':
                return tw_state
            return {
                'player_type': 'unknown',
                'has_controls': False,
                'theater_mode': False,
                'subtitles_on': False,
                'quality_menu_open': False,
                'fullscreen': False,
            }
        except Exception:
            return {
                'player_type': 'unknown',
                'has_controls': False,
                'theater_mode': False,
                'subtitles_on': False,
                'quality_menu_open': False,
                'fullscreen': False,
            }

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
                js_vol_change = f"""
                () => {{
                    const v=document.querySelector('video');
                    if(v){{
                        v.volume = {action == 'volume_up'} ? Math.min(1, v.volume + {step}) : Math.max(0, v.volume - {step});
                    }}
                }}
                """
                await page.evaluate(js_vol_change)
                return f"✅ Volume {action} executed."

            if action == 'mute':
                js_mute = "() => { const v=document.querySelector('video'); if(v)v.muted=!v.muted; }"
                await page.evaluate(js_mute)
                return "✅ Mute toggle executed."

            if action in ['seek_forward', 'seek_backward']:
                seconds = float(param) if param else 10
                js_seek = f"""
                () => {{
                    const v=document.querySelector('video');
                    if(v){{
                        v.currentTime = {action == 'seek_forward'} ? v.currentTime + {seconds} : Math.max(0, v.currentTime - {seconds});
                    }}
                }}
                """
                await page.evaluate(js_seek)
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
                () => {
                    if (!document.fullscreenElement && {is_enter}) {
                        document.documentElement.requestFullscreen().catch(e=>{});
                    } else if (document.fullscreenElement && !{is_enter}) {
                        document.exitFullscreen().catch(e=>{});
                    }
                }
                """.format(is_enter='true' if is_enter else 'false')
                await page.evaluate(js_fs)
                return f"✅ Fullscreen {action} executed."

            if action in ['theater_on', 'theater_off']:
                is_on = action == 'theater_on'
                # Try YouTube theater mode
                js_theater = f"""
                () => {{
                    const app = document.querySelector('ytd-app');
                    if (app) {{
                        if ({is_on}) {{
                            app.classList.add('theater-mode');
                        }} else {{
                            app.classList.remove('theater-mode');
                        }}
                    }}
                }}
                """
                await page.evaluate(js_theater)
                return f"✅ Theater mode {action} executed."

            if action == 'wide_mode':
                js_wide = """
                () => {
                    const btn = document.querySelector('.ytp-size-button');
                    if (btn) btn.click();
                }
                """
                await page.evaluate(js_wide)
                return "✅ Wide mode toggle executed."

            if action == 'default_size':
                js_default = """
                () => {
                    const app = document.querySelector('ytd-app');
                    if (app) app.classList.remove('theater-mode');
                }
                """
                await page.evaluate(js_default)
                return "✅ Default size executed."

            if action in ['subtitles_on', 'subtitles_off', 'subtitles_toggle']:
                is_on = action == 'subtitles_on'
                is_toggle = action == 'subtitles_toggle'
                js_subs = f"""
                () => {{
                    const video = document.querySelector('video');
                    if (video) {{
                        const tracks = video.textTracks;
                        for (let i = 0; i < tracks.length; i++) {{
                            tracks[i].mode = '{is_toggle or is_on and "showing" or "disabled"}';
                        }}
                    }}
                }}
                """
                await page.evaluate(js_subs)
                mode = "on" if is_on else ("off" if not is_toggle else "toggled")
                return f"✅ Subtitles {mode} executed."

            if action == 'quality_set':
                quality = param or 'auto'
                # For YouTube, we simulate quality menu selection
                js_quality = f"""
                () => {{
                    const btn = document.querySelector('.ytp-quality-selector-button');
                    if (btn) {{
                        // Try to set quality via API if available
                        const player = document.querySelector('ytd-player')?.player_;
                        if (player && player.getAvailableVideoQualityLevels) {{
                            const levels = player.getAvailableVideoQualityLevels();
                            if (levels.includes('{quality}')) {{
                                player.setPlaybackQualityRange('{quality}', '{quality}');
                            }}
                        }}
                    }}
                }}
                """
                await page.evaluate(js_quality)
                return f"✅ Quality set to {quality}."

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