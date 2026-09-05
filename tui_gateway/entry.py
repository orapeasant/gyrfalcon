"""TUI Gateway entry point."""

from __future__ import annotations

import sys
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("entry")



def main():
    """Entry point for the TUI gateway process (spawned by Node TUI)."""
    logger.debug("Beginning of main")
    from gyrfalcon.config import load_env_file
    load_env_file()

    from gyrfalcon.gyrfalcon_logging import setup_logging
    setup_logging(mode="cli")

    # Pre-warm the Copilot token cache in a background thread so the first
    # chat message doesn't pay the token-exchange latency (~2-5 s via proxy).
    from gyrfalcon.config import cfg_get
    import threading
    def _prewarm_token():
        try:
            provider = cfg_get("provider.active", "copilot")
            if provider == "copilot":
                from gyrfalcon.providers.copilot import get_copilot_token
                get_copilot_token()
                logger.debug("Copilot token pre-warmed")
        except Exception:
            pass
    threading.Thread(target=_prewarm_token, daemon=True, name="token-prewarm").start()

    from tui_gateway.transport import StdioTransport
    from tui_gateway.server import TUIGatewayServer

    transport = StdioTransport()
    server = TUIGatewayServer(transport)

    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
