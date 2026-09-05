"""Web tools — search and content extraction."""

from __future__ import annotations

import json
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.config import cfg_get, get_env_value
from gyrfalcon.net import httpx_request

logger = get_logger("tools.web")


def web_search(args: dict, **kwargs) -> str:
    """Search the web."""
    logger.debug("Beginning of web_search")
    query = args.get("query", "")
    limit = args.get("limit", 5)

    if not query:
        return json.dumps({"error": "No query provided"})

    # Try available backends
    backend = cfg_get("web.backend", "auto")

    if backend == "auto":
        # Try in order: Exa, Tavily, Firecrawl
        for try_backend in ["exa", "tavily", "firecrawl"]:
            result = _search_with_backend(try_backend, query, limit)
            if result:
                return result
        return json.dumps({"error": "No web search backend available. Set EXA_API_KEY, TAVILY_API_KEY, or FIRECRAWL_API_KEY."})

    result = _search_with_backend(backend, query, limit)
    if result:
        return result
    return json.dumps({"error": f"Web search backend '{backend}' not available"})


def _search_with_backend(backend: str, query: str, limit: int) -> Optional[str]:
    """Execute search with specific backend."""
    logger.debug("Beginning of _search_with_backend")
    if backend == "exa":
        api_key = get_env_value("EXA_API_KEY")
        if not api_key:
            return None
        return _exa_search(api_key, query, limit)
    elif backend == "tavily":
        api_key = get_env_value("TAVILY_API_KEY")
        if not api_key:
            return None
        return _tavily_search(api_key, query, limit)
    elif backend == "firecrawl":
        api_key = get_env_value("FIRECRAWL_API_KEY")
        if not api_key:
            return None
        return _firecrawl_search(api_key, query, limit)
    return None


def _exa_search(api_key: str, query: str, limit: int) -> str:
    logger.debug("Beginning of _exa_search")
    try:
        response = httpx_request(
            "POST",
            "https://api.exa.ai/search",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={"query": query, "numResults": limit, "useAutoprompt": True},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        results = [{
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("text", "")[:200],
        } for r in data.get("results", [])]
        return json.dumps({"results": results, "backend": "exa"})
    except Exception as e:
        return json.dumps({"error": f"Exa search failed: {str(e)}"})


def _tavily_search(api_key: str, query: str, limit: int) -> str:
    logger.debug("Beginning of _tavily_search")
    try:
        response = httpx_request(
            "POST",
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": limit},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        results = [{
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", "")[:200],
        } for r in data.get("results", [])]
        return json.dumps({"results": results, "backend": "tavily"})
    except Exception as e:
        return json.dumps({"error": f"Tavily search failed: {str(e)}"})


def _firecrawl_search(api_key: str, query: str, limit: int) -> str:
    logger.debug("Beginning of _firecrawl_search")
    try:
        response = httpx_request(
            "POST",
            "https://api.firecrawl.dev/v0/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": query, "limit": limit},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        results = [{
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("description", "")[:200],
        } for r in data.get("data", [])]
        return json.dumps({"results": results, "backend": "firecrawl"})
    except Exception as e:
        return json.dumps({"error": f"Firecrawl search failed: {str(e)}"})


def web_extract(args: dict, **kwargs) -> str:
    """Extract page content from URLs."""
    logger.debug("Beginning of web_extract")
    urls = args.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    url = args.get("url")
    if url:
        urls = [url]
    format_type = args.get("format", "markdown")

    if not urls:
        return json.dumps({"error": "No URLs provided"})

    results = []
    for target_url in urls[:5]:  # Cap at 5
        try:
            response = httpx_request(
                "GET",
                target_url,
                headers={"User-Agent": "Gyrfalcon/0.1 (AI Agent)"},
                follow_redirects=True,
                timeout=30,
            )
            response.raise_for_status()
            content = response.text

            # Basic HTML to text extraction
            if format_type == "markdown":
                content = _html_to_markdown(content)

            # Truncate
            if len(content) > 30_000:
                content = content[:30_000] + "\n... [truncated]"

            results.append({
                "url": target_url,
                "content": content,
                "status": response.status_code,
            })
        except Exception as e:
            results.append({
                "url": target_url,
                "error": str(e),
            })

    return json.dumps({"results": results})


def _html_to_markdown(html: str) -> str:
    """Basic HTML to text conversion."""
    logger.debug("Beginning of _html_to_markdown")
    import re
    # Remove scripts and styles
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    # Convert common tags
    text = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", r"\n# \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\n\1\n", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<a[^>]*href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", text, flags=re.DOTALL)
    # Remove remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return text.strip()


# Register tools
registry.register(
    name="web_search",
    toolset="web",
    schema={
        "name": "web_search",
        "description": "Search the web for information. Returns a list of relevant results with titles, URLs, and snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
    },
    handler=web_search,
    emoji="🌐",
    read_only=True,
)

registry.register(
    name="web_extract",
    toolset="web",
    schema={
        "name": "web_extract",
        "description": "Extract and read content from web pages. Returns the page content as markdown.",
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "description": "URLs to extract"},
                "url": {"type": "string", "description": "Single URL to extract"},
                "format": {"type": "string", "enum": ["markdown", "raw"], "default": "markdown"},
            },
        },
    },
    handler=web_extract,
    emoji="📄",
    read_only=True,
)
