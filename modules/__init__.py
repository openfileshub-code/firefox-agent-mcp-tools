"""
Firefox Agent MCP - Modules Package.

Each module handles a specific group of browser functions.
To disable a feature group, simply comment out its import below
and remove the corresponding tool registration in server.py.
"""

from modules.video_controller import VideoController
from modules.window_manager import WindowManager
from modules.link_cache import LinkCache

__all__ = [
    "VideoController",
    "WindowManager",
    "LinkCache",
]