"""CLI entry point — argument parsing, profile activation, and subcommand dispatch."""

from __future__ import annotations

import argparse
import os
import sys
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.gyrfalcon_constants import get_app_name
logger = get_logger("main")



def _apply_profile_override():
    """Pre-parses --profile/-p BEFORE module imports. Sets GYRFALCON_HOME env var."""
    logger.debug("Beginning of _apply_profile_override")
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg in ("--profile", "-p") and i < len(sys.argv) - 1:
            profile_name = sys.argv[i + 1]
            from pathlib import Path
            profile_home = Path.home() / ".gyrfalcon-profiles" / profile_name
            os.environ["GYRFALCON_HOME"] = str(profile_home)
            break
        elif arg.startswith("--profile="):
            profile_name = arg.split("=", 1)[1]
            from pathlib import Path
            profile_home = Path.home() / ".gyrfalcon-profiles" / profile_name
            os.environ["GYRFALCON_HOME"] = str(profile_home)
            break


def main():
    """Main entry point for `gyrfalcon` command."""
    logger.debug("Beginning of main")
    _apply_profile_override()

    # Load env files early. Real shell env vars always win; the per-profile
    # ~/.gyrfalcon/.env is loaded first so it can override the repo-root .env,
    # which only fills in defaults (e.g. GYRFALCON_APP_NAME) for anything unset.
    from gyrfalcon.gyrfalcon_constants import load_repo_root_env_file
    from gyrfalcon.config import load_env_file
    load_env_file()
    load_repo_root_env_file()

    parser = argparse.ArgumentParser(
        prog="gyrfalcon",
        description=f"{get_app_name()} — AI Agent",
    )
    parser.add_argument("--profile", "-p", help="Use named profile")
    parser.add_argument("--model", "-m", help="Model to use")
    parser.add_argument("--provider", help="Provider name")
    parser.add_argument("--api-key", help="API key")
    parser.add_argument("--base-url", help="API base URL")
    parser.add_argument("--max-turns", type=int, help="Max tool-calling iterations")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--tui", action="store_true", help="Launch TUI mode")
    parser.add_argument("--resume", help="Resume session ID")
    parser.add_argument("--checkpoints", action="store_true", help="Enable checkpoints")
    parser.add_argument("--toolsets", nargs="*", help="Enable specific toolsets")
    parser.add_argument("--quiet", "-q", action="store_true", help="Quiet mode")

    subparsers = parser.add_subparsers(dest="command")

    # Subcommands
    subparsers.add_parser("chat", help="Interactive chat (default)")
    subparsers.add_parser("setup", help="Interactive setup wizard")
    sub_auth = subparsers.add_parser("auth", help="Manage authentication")
    sub_auth.add_argument("auth_args", nargs="*", help="Auth subcommand args")
    subparsers.add_parser("doctor", help="Run diagnostics")
    subparsers.add_parser("version", help="Print version")
    subparsers.add_parser("gateway", help="Gateway daemon management")
    sub_sched = subparsers.add_parser("scheduler", aliases=["cron"], help="Scheduler management")
    sub_sched.add_argument("scheduler_args", nargs="*", help="list | add | remove | pause | resume")
    subparsers.add_parser("skills", help="Skill management")
    subparsers.add_parser("tools", help="Tool configuration")
    subparsers.add_parser("dashboard", help="Launch web dashboard")
    sub_logs = subparsers.add_parser("logs", help="Browse log files")
    sub_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output continuously (like tail -f)")
    sub_logs.add_argument("-n", "--lines", type=int, default=50, help="Number of lines to show (default: 50)")
    sub_logs.add_argument("--file", default="agent.log", help="Log file to read (default: agent.log)")
    sub_logs.add_argument("--list", action="store_true", help="List available log files")
    sub_models = subparsers.add_parser("models", help="List or select models")
    sub_models.add_argument("--provider", help="Filter by provider name")

    args = parser.parse_args()

    # Setup logging
    from gyrfalcon.gyrfalcon_logging import setup_logging
    setup_logging(verbose=args.verbose)

    # Dispatch
    if args.command == "version":
        from gyrfalcon import __version__
        print(f"{get_app_name()} v{__version__}")
        return

    if args.command == "setup":
        from gyrfalcon_cli.setup import run_setup
        run_setup()
        return

    if args.command == "auth":
        from gyrfalcon_cli.auth import run_auth_cli
        run_auth_cli(args.auth_args or [])
        return

    if args.command == "doctor":
        from gyrfalcon_cli.doctor import run_doctor
        run_doctor()
        return

    if args.command == "gateway":
        from gyrfalcon_cli.gateway_cmd import run_gateway
        run_gateway()
        return

    if args.command in ("scheduler", "cron"):
        from gyrfalcon_cli.scheduler_cmd import run_scheduler_cli
        run_scheduler_cli(args.scheduler_args or None)
        return

    if args.command == "dashboard":
        from gyrfalcon_cli.web_server import run_dashboard
        run_dashboard()
        return

    if args.command == "skills":
        from gyrfalcon_cli.skills_cmd import run_skills_cli
        run_skills_cli()
        return

    if args.command == "logs":
        from gyrfalcon_cli.logs_cmd import run_logs, list_log_files
        if args.list:
            list_log_files()
        else:
            run_logs(follow=args.follow, lines=args.lines, log_file=args.file)
        return

    if args.command == "models":
        from gyrfalcon_cli.models_cmd import run_models_cli
        run_models_cli(provider=getattr(args, "provider", None))
        return

    # Default: interactive chat
    if args.tui:
        from gyrfalcon_cli.tui_launcher import launch_tui
        launch_tui(args)
    else:
        from gyrfalcon_cli.cli import GyrfalconCLI
        cli = GyrfalconCLI(
            model=args.model,
            provider=args.provider,
            api_key=args.api_key,
            base_url=args.base_url,
            max_turns=args.max_turns,
            verbose=args.verbose,
            resume=args.resume,
            checkpoints=args.checkpoints,
            toolsets=args.toolsets,
        )
        cli.run()


if __name__ == "__main__":
    main()
