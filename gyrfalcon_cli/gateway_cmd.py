"""Gateway CLI command handlers."""

from __future__ import annotations

import asyncio

from rich.console import Console
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.gyrfalcon_constants import get_app_name
logger = get_logger("gateway_cmd")



def run_gateway():
    """Gateway daemon management CLI."""
    logger.debug("Beginning of run_gateway")
    console = Console()

    from gyrfalcon.gateway import GatewayRunner, GatewayConfig

    config = GatewayConfig()

    if config.platforms:
        console.print(f"[bold]{get_app_name()} Gateway[/bold] — {len(config.platforms)} platform(s) configured\n")
    else:
        console.print(f"[bold]{get_app_name()} Gateway[/bold] — scheduler mode (no platforms configured)\n")
        console.print("[dim]Tip: add platforms under gateway.platforms in config.yaml[/dim]\n")

    runner = GatewayRunner(config)

    async def _run():
        """Run gateway; scheduler.stop() is guaranteed via finally."""
        try:
            await runner.start()
        finally:
            await runner.stop()

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        pass

    console.print("\n[dim]Gateway stopped[/dim]")
