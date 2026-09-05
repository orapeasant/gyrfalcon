"""Network utilities — proxy-first HTTP client with automatic fallback."""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.utils import normalize_proxy_url

logger = get_logger("net")


def get_ssl_verify() -> bool | str:
    """Resolve SSL verification setting.

    Priority:
      GYRFALCON_SSL_VERIFY=false  → disable verification (corporate MiTM/inspection)
      SSL_CERT_FILE / REQUESTS_CA_BUNDLE → path to corporate CA bundle
      Default → True (standard verification)

    Set in ~/.gyrfalcon/.env:
      GYRFALCON_SSL_VERIFY=false          # disable (quick fix)
      GYRFALCON_SSL_VERIFY=/path/ca.pem   # use custom CA bundle
    """
    val = os.environ.get("GYRFALCON_SSL_VERIFY", "").strip()
    if val.lower() in ("false", "0", "no"):
        return False
    if val and val not in ("true", "1", "yes"):
        return val  # treat as CA bundle path

    # Fall back to standard CA bundle env vars
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var, "").strip()
        if path:
            return path

    return True


def get_proxy_url() -> Optional[str]:
    """Resolve proxy URL from environment or config.

    Priority: GYRFALCON_PROXY > HTTPS_PROXY > HTTP_PROXY > config.
    """
    logger.debug("Beginning of get_proxy_url")
    for var in ("GYRFALCON_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                "https_proxy", "http_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            return normalize_proxy_url(val)

    # Check config as last resort
    from gyrfalcon.config import cfg_get
    proxy = cfg_get("network.proxy", "")
    if proxy:
        return normalize_proxy_url(proxy)
    return None


# ── Proxy failure cache ────────────────────────────────────────────────────
# When a proxy fails for a given hostname, cache the failure so we skip the
# proxy attempt on subsequent calls for PROXY_FAIL_CACHE_TTL seconds.
import threading as _threading
from urllib.parse import urlparse as _urlparse

_proxy_fail_cache: dict[str, float] = {}
_proxy_fail_lock = _threading.Lock()
PROXY_FAIL_CACHE_TTL = 300  # 5 minutes
PROXY_CONNECT_TIMEOUT = 2.0  # short timeout for proxy probe


def _hostname_of(url: str) -> str:
    try:
        return _urlparse(url).hostname or ""
    except Exception:
        return ""


def _is_proxy_cached_failed(url: str) -> bool:
    """Return True if proxy recently failed for this hostname."""
    host = _hostname_of(url)
    if not host:
        return False
    with _proxy_fail_lock:
        fail_time = _proxy_fail_cache.get(host)
        if fail_time is None:
            return False
        import time
        if time.time() - fail_time < PROXY_FAIL_CACHE_TTL:
            return True
        # Expired — remove entry
        del _proxy_fail_cache[host]
        return False


def _mark_proxy_failed(url: str) -> None:
    """Record that proxy failed for this hostname."""
    host = _hostname_of(url)
    if host:
        import time
        with _proxy_fail_lock:
            _proxy_fail_cache[host] = time.time()


def httpx_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json: Any = None,
    data: Any = None,
    timeout: float = 30.0,
    follow_redirects: bool = False,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request: direct-first, proxy-fallback.

    Strategy:
    1. Try direct connection first (fastest path).
    2. If direct fails AND a proxy is configured AND proxy hasn't recently
       failed for this host, retry through the proxy with a short connect timeout.
    3. Cache proxy failures per-hostname for 5 minutes.
    """
    logger.debug("Beginning of httpx_request")

    # ── Direct attempt ─────────────────────────────────────────────────────
    try:
        return _do_request(
            method, url,
            proxy=None,
            headers=headers,
            json=json,
            data=data,
            timeout=timeout,
            follow_redirects=follow_redirects,
            **kwargs,
        )
    except Exception as direct_err:
        logger.debug(f"Direct request failed: {direct_err}")

    # ── Proxy fallback ─────────────────────────────────────────────────────
    proxy_url = get_proxy_url()
    if not proxy_url or _is_proxy_cached_failed(url):
        raise direct_err  # type: ignore[name-defined]  # noqa: F821

    try:
        response = _do_request(
            method, url,
            proxy=proxy_url,
            headers=headers,
            json=json,
            data=data,
            timeout=PROXY_CONNECT_TIMEOUT,
            follow_redirects=follow_redirects,
            **kwargs,
        )
        return response
    except Exception as proxy_err:
        _mark_proxy_failed(url)
        logger.debug(f"Proxy fallback also failed ({proxy_url}): {proxy_err}")
        raise direct_err  # type: ignore[name-defined]  # noqa: F821


def _do_request(
    method: str,
    url: str,
    *,
    proxy: str | None,
    headers: dict | None,
    json: Any,
    data: Any,
    timeout: float,
    follow_redirects: bool,
    **kwargs,
) -> httpx.Response:
    """Execute a single HTTP request with optional proxy."""
    logger.debug("Beginning of _do_request")
    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": follow_redirects,
        "verify": get_ssl_verify(),
    }
    if proxy:
        client_kwargs["proxy"] = proxy

    with httpx.Client(**client_kwargs) as client:
        return client.request(
            method, url,
            headers=headers,
            json=json,
            data=data,
            **kwargs,
        )


def get_openai_client(base_url: str | None = None, api_key: str | None = None):
    """Create an OpenAI client with proxy-first fallback."""
    logger.debug("Beginning of get_openai_client")
    from openai import OpenAI

    proxy_url = get_proxy_url()
    ssl_verify = get_ssl_verify()
    http_client = None
    if proxy_url:
        http_client = httpx.Client(proxy=proxy_url, verify=ssl_verify)
    elif ssl_verify is not True:
        http_client = httpx.Client(verify=ssl_verify)

    kwargs: dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    if http_client:
        kwargs["http_client"] = http_client

    return OpenAI(**kwargs)


def get_anthropic_client(api_key: str | None = None):
    """Create an Anthropic client with proxy support."""
    logger.debug("Beginning of get_anthropic_client")
    from anthropic import Anthropic

    proxy_url = get_proxy_url()
    ssl_verify = get_ssl_verify()
    http_client = None
    if proxy_url:
        http_client = httpx.Client(proxy=proxy_url, verify=ssl_verify)
    elif ssl_verify is not True:
        http_client = httpx.Client(verify=ssl_verify)

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if http_client:
        kwargs["http_client"] = http_client

    return Anthropic(**kwargs)


def get_openai_client_with_fallback(base_url: str | None = None, api_key: str | None = None):
    """Create OpenAI client: direct-first, proxy-fallback on connection failure."""
    logger.debug("Beginning of get_openai_client_with_fallback")
    proxy_url = get_proxy_url()
    if not proxy_url:
        return get_openai_client(base_url, api_key)

    # Return a wrapper that tries direct first, falls back to proxy
    return _OpenAIFallbackWrapper(base_url, api_key, proxy_url)


class _OpenAIFallbackWrapper:
    """OpenAI client wrapper: direct-first, proxy-fallback with cached state."""

    # Class-level flag: once proxy fails, all instances skip it until TTL expires
    _proxy_failed_at: float = 0.0
    _state_lock = _threading.Lock()

    def __init__(self, base_url: str | None, api_key: str | None, proxy_url: str):
        from openai import OpenAI

        self._base_url = base_url
        self._api_key = api_key
        self._proxy_url = proxy_url
        self._ssl_verify = get_ssl_verify()

        # Create direct client (primary)
        direct_kwargs: dict[str, Any] = {}
        if base_url:
            direct_kwargs["base_url"] = base_url
        if api_key:
            direct_kwargs["api_key"] = api_key
        if self._ssl_verify is not True:
            direct_kwargs["http_client"] = httpx.Client(verify=self._ssl_verify)
        self._direct_client = OpenAI(**direct_kwargs)

        # Proxy client created lazily only if direct fails
        self._proxy_client: Optional[Any] = None

    def _is_proxy_known_bad(self) -> bool:
        import time
        with self._state_lock:
            if self._proxy_failed_at == 0.0:
                return False
            if time.time() - self._proxy_failed_at < PROXY_FAIL_CACHE_TTL:
                return True
            self._proxy_failed_at = 0.0
            return False

    def _record_proxy_failure(self) -> None:
        import time
        with self._state_lock:
            _OpenAIFallbackWrapper._proxy_failed_at = time.time()

    def _get_proxy_client(self):
        if self._proxy_client is None:
            from openai import OpenAI
            http_client = httpx.Client(
                proxy=self._proxy_url,
                verify=self._ssl_verify,
                timeout=httpx.Timeout(PROXY_CONNECT_TIMEOUT, read=30.0),
            )
            kwargs: dict[str, Any] = {"http_client": http_client}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._proxy_client = OpenAI(**kwargs)
        return self._proxy_client

    @property
    def chat(self):
        return _ChatProxy(self)

    @property
    def models(self):
        return _ModelsProxy(self)


class _ChatProxy:
    """Proxy for client.chat that handles fallback."""

    def __init__(self, wrapper: _OpenAIFallbackWrapper):
        self._wrapper = wrapper

    @property
    def completions(self):
        return _CompletionsProxy(self._wrapper)


class _CompletionsProxy:
    """Proxy for client.chat.completions: direct-first, proxy-fallback."""

    def __init__(self, wrapper: _OpenAIFallbackWrapper):
        self._wrapper = wrapper

    def create(self, **kwargs):
        logger.debug("Beginning of create")
        import httpx as _httpx
        from openai import APIConnectionError

        # Direct attempt first
        direct_err = None
        try:
            return self._wrapper._direct_client.chat.completions.create(**kwargs)
        except (APIConnectionError, _httpx.ConnectError, _httpx.ConnectTimeout, OSError) as e:
            direct_err = e
            logger.debug(f"Direct connection failed: {e}, trying proxy")

        # Proxy fallback (skip if known bad)
        if self._wrapper._is_proxy_known_bad():
            raise direct_err

        try:
            proxy_client = self._wrapper._get_proxy_client()
            return proxy_client.chat.completions.create(**kwargs)
        except (APIConnectionError, _httpx.ConnectError, _httpx.ProxyError, _httpx.ConnectTimeout, OSError) as proxy_err:
            self._wrapper._record_proxy_failure()
            logger.debug(f"Proxy fallback also failed: {proxy_err}")
            raise direct_err from proxy_err


class _ModelsProxy:
    """Proxy for client.models: direct-first, proxy-fallback."""

    def __init__(self, wrapper: _OpenAIFallbackWrapper):
        self._wrapper = wrapper

    def list(self, **kwargs):
        logger.debug("Beginning of list")
        import httpx as _httpx
        from openai import APIConnectionError

        direct_err = None
        try:
            return self._wrapper._direct_client.models.list(**kwargs)
        except (APIConnectionError, _httpx.ConnectError, _httpx.ConnectTimeout, OSError) as e:
            direct_err = e
            logger.debug(f"Direct models.list failed: {e}, trying proxy")

        if self._wrapper._is_proxy_known_bad():
            raise direct_err

        try:
            proxy_client = self._wrapper._get_proxy_client()
            return proxy_client.models.list(**kwargs)
        except (APIConnectionError, _httpx.ConnectError, _httpx.ProxyError, _httpx.ConnectTimeout, OSError) as proxy_err:
            self._wrapper._record_proxy_failure()
            logger.debug(f"Proxy fallback also failed: {proxy_err}")
            raise direct_err from proxy_err
