import os
import time
import tempfile
from typing import Optional

try:
    from fastmcp import FastMCP, Context
except ImportError:
    from mcp.server.fastmcp import FastMCP, Context

# Initialize FastMCP server
mcp = FastMCP("Firefox Agent MCP")

# Global browser state
_camoufox_manager = None
_browser_context = None
_page_instance = None
_session_start_time = None
_user_data_dir = None


def get_session_uptime():
    if _session_start_time:
        return time.time() - _session_start_time
    return 0


@mcp.tool()
def browser_start(headless: bool = False) -> str:
    """Starts browser with anti-detect protection (Camoufox preferred). Kills existing instances first. Preserves session cookies between runs."""
    global _camoufox_manager, _browser_context, _page_instance, _session_start_time, _user_data_dir
    
    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        return "Error: camoufox is not installed. Run 'pip install camoufox' and 'camoufox fetch' first."

    # Stop existing instance if running
    if _camoufox_manager:
        try:
            _camoufox_manager.__exit__(None, None, None)
        except Exception:
            pass
        _camoufox_manager = None
        _browser_context = None
        _page_instance = None

    _session_start_time = time.time()
    
    # Use a persistent user data directory to preserve cookies/sessions between restarts
    if not _user_data_dir:
        _user_data_dir = os.path.join(tempfile.gettempdir(), "firefox_agent_mcp_profile")
        os.makedirs(_user_data_dir, exist_ok=True)

    try:
        # Launch Camoufox with persistent context.
        # IMPORTANT: Do NOT pass window_size, viewport, or screen directly as kwargs here.
        # Camoufox handles fingerprinting automatically. 
        # When persistent_context=True, the context manager yields a BrowserContext directly.
        _camoufox_manager = Camoufox(
            headless=headless,
            persistent_context=True,
            user_data_dir=_user_data_dir,
            os="windows",
            humanize=True,
            enable_cache=True,
        )
        
        # __enter__ returns the BrowserContext when persistent_context=True
        _browser_context = _camoufox_manager.__enter__()
        
        # Get the default page or create one
        pages = _browser_context.pages
        if pages:
            _page_instance = pages[0]
        else:
            _page_instance = _browser_context.new_page()
            
        return f"Browser started successfully. Profile dir: {_user_data_dir}"
    except Exception as e:
        _camoufox_manager = None
        _browser_context = None
        _page_instance = None
        return f"Failed to start Camoufox: {str(e)}"


@mcp.tool()
def browser_stop() -> str:
    """Stops the browser."""
    global _camoufox_manager, _browser_context, _page_instance
    if _camoufox_manager:
        try:
            _camoufox_manager.__exit__(None, None, None)
        except Exception:
            pass
        _camoufox_manager = None
        _browser_context = None
        _page_instance = None
        return "Browser stopped successfully."
    return "Browser was not running."


@mcp.tool()
def browser_navigate(url: str) -> str:
    """Navigates to URL."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started. Call browser_start first."
    try:
        _page_instance.goto(url, wait_until="domcontentloaded", timeout=30000)
        return f"Navigated to {url}"
    except Exception as e:
        return f"Navigation failed: {str(e)}"


@mcp.tool()
def browser_get_content() -> str:
    """Gets page text."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        return _page_instance.content()
    except Exception as e:
        return f"Failed to get content: {str(e)}"


@mcp.tool()
def browser_click(selector: str) -> str:
    """Clicks element."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        _page_instance.click(selector, timeout=10000)
        return f"Clicked element: {selector}"
    except Exception as e:
        return f"Click failed: {str(e)}"


@mcp.tool()
def browser_fill(selector: str, value: str) -> str:
    """Fills input field. Uses sequential typing to mimic human behavior."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        # type() instead of fill() mimics human keystrokes
        _page_instance.type(selector, value, delay=50)
        return f"Filled {selector} with: {value}"
    except Exception as e:
        return f"Fill failed: {str(e)}"


@mcp.tool()
def browser_screenshot(filename: str = "shot.png") -> str:
    """Takes screenshot."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        _page_instance.screenshot(path=filename, full_page=True)
        return f"Screenshot saved to {filename}"
    except Exception as e:
        return f"Screenshot failed: {str(e)}"


@mcp.tool()
def browser_select_option(selector: str, value: str) -> str:
    """Selects an option in a <select> dropdown by its value attribute. Use for country, role, or any dropdown form field."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        _page_instance.select_option(selector, value=value)
        return f"Selected {value} in {selector}"
    except Exception as e:
        return f"Select failed: {str(e)}"


@mcp.tool()
def browser_press_key(key: str) -> str:
    """Presses a keyboard key. Examples: 'Enter', 'Tab', 'Escape', 'ArrowDown'. Use after filling forms to submit or navigate between fields."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        _page_instance.keyboard.press(key)
        return f"Pressed key: {key}"
    except Exception as e:
        return f"Key press failed: {str(e)}"


@mcp.tool()
def browser_wait_for(selector_or_url: str, timeout: int = 15000) -> str:
    """Waits for a CSS selector to appear on page OR for URL to contain a substring. Use after login clicks to wait for redirect."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        if selector_or_url.startswith("/") or selector_or_url.startswith("http"):
            _page_instance.wait_for_url(f"**{selector_or_url}**", timeout=timeout)
            return f"URL changed to match: {selector_or_url}"
        else:
            _page_instance.wait_for_selector(selector_or_url, state="visible", timeout=timeout)
            return f"Element appeared: {selector_or_url}"
    except Exception as e:
        return f"Wait failed: {str(e)}"


@mcp.tool()
def browser_evaluate(expression: str) -> str:
    """Executes JavaScript in the browser and returns the result. Use for getting/setting values, checking login state, or interacting with page APIs."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        result = _page_instance.evaluate(expression)
        return str(result)
    except Exception as e:
        return f"Evaluation failed: {str(e)}"


@mcp.tool()
def browser_get_url() -> str:
    """Returns the current page URL. Use to verify navigation or check if login redirect happened."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        return _page_instance.url
    except Exception as e:
        return f"Failed to get URL: {str(e)}"


@mcp.tool()
def browser_check(selector: str) -> str:
    """Checks a checkbox or radio button. Use for 'I agree' checkboxes, 'Remember me', etc."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        _page_instance.check(selector)
        return f"Checked: {selector}"
    except Exception as e:
        return f"Check failed: {str(e)}"


@mcp.tool()
def browser_uncheck(selector: str) -> str:
    """Unchecks a checkbox. Use to deselect options."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    try:
        _page_instance.uncheck(selector)
        return f"Unchecked: {selector}"
    except Exception as e:
        return f"Uncheck failed: {str(e)}"


@mcp.tool()
def browser_get_menu_links(menu_type: str = "all") -> str:
    """Extracts navigation links from the page menus. Use this to quickly find where to navigate without parsing the whole page. Args: menu_type='header' (top nav), 'footer' (bottom links), or 'all' (both). Returns formatted list of 'Link Text -> URL'."""
    global _page_instance
    if not _page_instance:
        return "Error: Browser not started."
    
    js_script = """
    (menuType) => {
        const links = [];
        const selectors = [];
        
        if (menuType === 'header' || menuType === 'all') {
            selectors.push('header', 'nav', '[role="navigation"]', '.header', '.nav', '.menu');
        }
        if (menuType === 'footer' || menuType === 'all') {
            selectors.push('footer', '.footer');
        }
        
        const seenUrls = new Set();
        const garbagePatterns = ['facebook.com', 'twitter.com', 'instagram.com', 'linkedin.com', 'youtube.com', 'javascript:', '#'];
        
        document.querySelectorAll(selectors.join(',')).forEach(container => {
            container.querySelectorAll('a').forEach(a => {
                const href = a.href;
                const text = a.textContent.trim();
                
                if (!href || !text) return;
                if (seenUrls.has(href)) return;
                if (garbagePatterns.some(p => href.toLowerCase().includes(p))) return;
                
                seenUrls.add(href);
                links.push(`${text} -> ${href}`);
            });
        });
        
        return links.length > 0 ? links.join('\\n') : 'No menu links found.';
    }
    """
    try:
        result = _page_instance.evaluate(js_script, menu_type)
        return result
    except Exception as e:
        return f"Menu extraction failed: {str(e)}"


if __name__ == "__main__":
    mcp.run()