"""TUI launcher — spawns the Node.js Ink TUI process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("tui_launcher")



def launch_tui(args) -> None:
    """Launch the TUI (Ink/React terminal UI)."""
    # Find ui-tui entry point
    logger.debug("Beginning of launch_tui")
    tui_dir = Path(__file__).parent.parent / "ui-tui"
    entry = tui_dir / "dist" / "entry.js"

    if not entry.exists():
        # Try development mode
        entry = tui_dir / "src" / "entry.tsx"
        if not entry.exists():
            print("TUI not built. Run: cd ui-tui && npm install && npm run build")
            sys.exit(1)
        # Use tsx for development
        cmd = ["npx", "tsx", str(entry)]
    else:
        cmd = ["node", str(entry)]

    # Pass config via environment
    env = os.environ.copy()
    if args.model:
        env["GYRFALCON_MODEL"] = args.model
    if args.resume:
        env["GYRFALCON_RESUME"] = args.resume
    if args.verbose:
        env["GYRFALCON_VERBOSE"] = "1"

    try:
        subprocess.run(cmd, env=env)
    except FileNotFoundError:
        print("Node.js is required for TUI mode. Install Node.js 20+ and try again.")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
