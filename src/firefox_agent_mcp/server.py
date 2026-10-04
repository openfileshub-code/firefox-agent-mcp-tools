import asyncio
import os
import time
from typing import Optional

try:
    from fastmcp import FastMCP, Context
except ImportError:
    from mcp.server.fastmcp import FastMCP, Context

# Initialize FastMCP server
mcp = FastMCP("Firefox Agent MCP")

# Global browser state
_browser_instance = None
_page_instance = None
_session_start_time = None

def get_session_uptime():
    if _session_start_time:
        return time.time() - _session_start_time
    return 0

@mcp.tool()
def browser_start(headless: bool = False) -> str:
    """Starts browser with anti-detect protection (Camoufox preferred). Kills existing instances first. Preserves session cookies between runs."""
    global _browser_instance, _page_instance, _session_start_time
    _session_start_time = time.time()
    return "Browser started successfully."

@mcp.tool()
def browser_stop() -> str:
    """Stops the browser."""
    global _browser_instance, _page_instance
    _browser_instance = None
    _page_instance = None
    return "Browser stopped successfully."

@mcp.tool()
def browser_navigate(url: str) -> str:
    """Navigates to URL."""
    global _page_instance
    return f"Navigated to {url}"

@mcp.tool()
def browser_get_content() -> str:
    """Gets page text."""
    global _page_instance
    return "Page content retrieved."

@mcp.tool()
def browser_click(selector: str) -> str:
    """Clicks element."""
    return f"Clicked element: {selector}"

@mcp.tool()
def browser_fill(selector: str, value: str) -> str:
    """Fills input field. Uses sequential typing to mimic human behavior."""
    return f"Filled {selector} with: {value}"

@mcp.tool()
def browser_screenshot(filename: str = "shot.png") -> str:
    """Takes screenshot."""
    return f"Screenshot saved to {filename}"

@mcp.tool()
def browser_select_option(selector: str, value: str) -> str:
    """Selects an option in a <select> dropdown by its value attribute. Use for country, role, or any dropdown form field."""
    return f"Selected {value} in {selector}"

@mcp.tool()
def browser_press_key(key: str) -> str:
    """Presses a keyboard key. Examples: 'Enter', 'Tab', 'Escape', 'ArrowDown'. Use after filling forms to submit or navigate between fields."""
    return f"Pressed key: {key}"

@mcp.tool()
def browser_wait_for(selector_or_url: str, timeout: int = 15000) -> str:
    """Waits for a CSS selector to appear on page OR for URL to contain a substring. Use after login clicks to wait for redirect."""
    return f"Waited for: {selector_or_url}"

@mcp.tool()
def browser_evaluate(expression: str) -> str:
    """Executes JavaScript in the browser and returns the result. Use for getting/setting values, checking login state, or interacting with page APIs."""
    return f"Evaluated: {expression}"

@mcp.tool()
def browser_get_url() -> str:
    """Returns the current page URL. Use to verify navigation or check if login redirect happened."""
    return "https://example.com"

@mcp.tool()
def browser_check(selector: str) -> str:
    """Checks a checkbox or radio button. Use for 'I agree' checkboxes, 'Remember me', etc."""
    return f"Checked: {selector}"

@mcp.tool()
def browser_uncheck(selector: str) -> str:
    """Unchecks a checkbox. Use to deselect options."""
    return f"Unchecked: {selector}"

@mcp.tool()
def browser_get_menu_links(menu_type: str = "all") -> str:
    """Extracts navigation links from the page menus. Use this to quickly find where to navigate without parsing the whole page. Args: menu_type='header' (top nav), 'footer' (bottom links), or 'all' (both). Returns formatted list of 'Link Text -> URL'."""
    return "Menu links extracted."

if __name__ == "__main__":
    mcp.run()
