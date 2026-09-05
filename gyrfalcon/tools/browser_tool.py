"""Browser automation tool — playwright-based navigation and interaction."""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.browser")

# Lazy browser state
_browser_instance: Any = None
_browser_context: Any = None
_page: Any = None


def _check_playwright_available() -> bool:
    """Check if playwright is installed."""
    logger.debug("Beginning of _check_playwright_available")
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _get_page():
    """Lazily initialize browser and return the active page."""
    global _browser_instance, _browser_context, _page

    if _page is not None:
        return _page

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright is not installed. Install with: pip install playwright && playwright install chromium"
        )

    pw = await async_playwright().start()
    _browser_instance = await pw.chromium.launch(headless=True)
    _browser_context = await _browser_instance.new_context(
        user_agent="Gyrfalcon/0.1 (AI Agent Browser)"
    )
    _page = await _browser_context.new_page()
    return _page


async def _cleanup_browser() -> None:
    """Close browser resources."""
    global _browser_instance, _browser_context, _page
    if _page:
        await _page.close()
        _page = None
    if _browser_context:
        await _browser_context.close()
        _browser_context = None
    if _browser_instance:
        await _browser_instance.close()
        _browser_instance = None


async def browser_navigate(args: dict, **kwargs) -> str:
    """Navigate to a URL and return page content."""
    url = args.get("url", "")
    wait_for = args.get("wait_for", "networkidle")

    if not url:
        return json.dumps({"error": "No url provided"})

    try:
        page = await _get_page()
        response = await page.goto(url, wait_until=wait_for, timeout=30000)

        title = await page.title()
        content = await page.content()

        # Extract text content for a cleaner result
        text_content = await page.evaluate("() => document.body?.innerText || ''")

        # Truncate if too long
        if len(text_content) > 50_000:
            text_content = text_content[:50_000] + "\n... [truncated]"

        return json.dumps({
            "url": page.url,
            "title": title,
            "status": response.status if response else None,
            "content": text_content,
        })
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"browser_navigate failed: {e}", exc_info=True)
        return json.dumps({"error": f"Navigation failed: {str(e)}"})


async def browser_screenshot(args: dict, **kwargs) -> str:
    """Take a screenshot of a URL and return as base64."""
    url = args.get("url", "")

    if not url:
        return json.dumps({"error": "No url provided"})

    try:
        page = await _get_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)

        screenshot_bytes = await page.screenshot(full_page=False)
        b64_image = base64.b64encode(screenshot_bytes).decode("utf-8")

        return json.dumps({
            "url": page.url,
            "screenshot_base64": b64_image,
            "format": "png",
        })
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"browser_screenshot failed: {e}", exc_info=True)
        return json.dumps({"error": f"Screenshot failed: {str(e)}"})


async def browser_click(args: dict, **kwargs) -> str:
    """Click an element on the current page."""
    selector = args.get("selector", "")

    if not selector:
        return json.dumps({"error": "No selector provided"})

    try:
        page = await _get_page()
        await page.click(selector, timeout=10000)

        # Wait for any navigation or updates
        await page.wait_for_load_state("domcontentloaded")

        return json.dumps({
            "status": "success",
            "selector": selector,
            "url": page.url,
        })
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"browser_click failed: {e}", exc_info=True)
        return json.dumps({"error": f"Click failed: {str(e)}"})


async def browser_type(args: dict, **kwargs) -> str:
    """Type text into an element on the current page."""
    selector = args.get("selector", "")
    text = args.get("text", "")

    if not selector:
        return json.dumps({"error": "No selector provided"})
    if not text:
        return json.dumps({"error": "No text provided"})

    try:
        page = await _get_page()
        await page.fill(selector, text, timeout=10000)

        return json.dumps({
            "status": "success",
            "selector": selector,
            "text_length": len(text),
        })
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"browser_type failed: {e}", exc_info=True)
        return json.dumps({"error": f"Type failed: {str(e)}"})


# Register tools
registry.register(
    name="browser_navigate",
    toolset="browser",
    schema={
        "name": "browser_navigate",
        "description": "Navigate to a URL using a headless browser and return the page content. Supports JavaScript-rendered pages.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
                "wait_for": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                    "default": "networkidle",
                    "description": "When to consider navigation complete",
                },
            },
            "required": ["url"],
        },
    },
    handler=browser_navigate,
    check_fn=_check_playwright_available,
    is_async=True,
    emoji="🌍",
)

registry.register(
    name="browser_screenshot",
    toolset="browser",
    schema={
        "name": "browser_screenshot",
        "description": "Take a screenshot of a web page and return it as a base64-encoded PNG image.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to screenshot"},
            },
            "required": ["url"],
        },
    },
    handler=browser_screenshot,
    check_fn=_check_playwright_available,
    is_async=True,
    emoji="📸",
)

registry.register(
    name="browser_click",
    toolset="browser",
    schema={
        "name": "browser_click",
        "description": "Click an element on the current browser page using a CSS selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of element to click"},
            },
            "required": ["selector"],
        },
    },
    handler=browser_click,
    check_fn=_check_playwright_available,
    is_async=True,
    emoji="👆",
)

registry.register(
    name="browser_type",
    toolset="browser",
    schema={
        "name": "browser_type",
        "description": "Type text into an input element on the current browser page.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of input element"},
                "text": {"type": "string", "description": "Text to type into the element"},
            },
            "required": ["selector", "text"],
        },
    },
    handler=browser_type,
    check_fn=_check_playwright_available,
    is_async=True,
    emoji="⌨️",
)
