# Firefox Agent MCP Tools

A set of Model Context Protocol (MCP) tools for browser automation using Firefox. This project includes tools for starting/stopping browsers, navigating pages, interacting with elements (clicks, fills, selections), taking screenshots, and extracting navigation menus.

## Disclaimer

**Note:** This project was developed for personal use and internal workflows. It is provided "as is" without any warranties or guarantees of universal compatibility or suitability for specific use cases. Users are encouraged to review, adapt, and modify the code to fit their specific environments and requirements. The author assumes no liability for any issues, errors, or consequences arising from the use of this software. Use at your own discretion.

## Features

- `browser_start`: Starts browser with anti-detect protection (Camoufox preferred). Kills existing instances first. Preserves session cookies between runs.
- `browser_stop`: Stops the browser.
- `browser_navigate`: Navigates to a specified URL.
- `browser_get_content`: Gets page text/content.
- `browser_click`: Clicks an element by selector.
- `browser_fill`: Fills input fields using sequential typing to mimic human behavior.
- `browser_screenshot`: Takes a screenshot of the page.
- `browser_select_option`: Selects an option in a `<select>` dropdown by its value attribute.
- `browser_press_key`: Presses a keyboard key (e.g., 'Enter', 'Tab', 'Escape').
- `browser_wait_for`: Waits for a CSS selector to appear or for URL to contain a substring.
- `browser_evaluate`: Executes JavaScript in the browser and returns the result.
- `browser_get_url`: Returns the current page URL.
- `browser_check`: Checks a checkbox or radio button.
- `browser_uncheck`: Unchecks a checkbox.
- `browser_get_menu_links`: Extracts navigation links from the page menus (header, footer, or all). Uses DOM density analysis to identify menu containers.

## Installation Manual

### Prerequisites

1. Python 3.9+ installed on your system.
2. Git installed for cloning the repository.
3. Firefox browser installed on your system.

### Step 1: Clone the Repository

```bash
git clone https://github.com/openfileshub-code/firefox-agent-mcp-tools.git
cd firefox-agent-mcp-tools
```

### Step 2: Create a Virtual Environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### Step 3: Install Dependencies

Install the required Python packages:

```bash
pip install camoufox playwright
playwright install firefox
```

*Note: Depending on your specific setup, you may also need `jsonrpc-stream`, `mcp` (Model Context Protocol server library), or other dependencies used in the MCP server implementation.*

### Step 4: Configure the MCP Server

1. Locate the MCP server configuration file in your AI agent or IDE (e.g., VS Code, Cursor, or custom MCP host).
2. Add the server configuration pointing to the Python script (e.g., the main server file with MCP tools).
3. Ensure the script has execution permissions and the Python virtual environment is activated.

### Step 5: Run the MCP Server

```bash
python your_server_script.py
```

## Usage

The MCP tools can be invoked by your AI agent or host application using the standard MCP protocol. Ensure the browser automation tools are enabled in your MCP server configuration.

## Menu Extraction Logic

The `browser_get_menu_links` tool uses a clustering approach to identify menus:

- Menus are identified as groups of links within a container.
- The tool traverses the DOM tree (up to 3 levels up) and calculates link density.
- High-density containers are identified as menus.
- Garbage links (social media like `facebook.com`, `twitter.com`, anchors like `#`, `javascript:void(0)`, empty links) are filtered out.

## License

This project is provided for personal and educational use.