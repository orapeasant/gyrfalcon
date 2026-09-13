"""Interactive REPL — GyrfalconCLI class."""

from __future__ import annotations

import sys
import time
import threading
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, CompleteEvent

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home, get_app_name
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.config import load_config, cfg_get
from gyrfalcon.run_agent import AIAgent
from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.plugins import PluginManager
from gyrfalcon.tools.mcp_tool import initialize_mcp_servers

logger = get_logger("cli")


class HistoryCompleter(Completer):
    """Tab completer that suggests from command history (bash-style).
    
    Single Tab: complete to longest common prefix
    Double Tab: show all matches (when already at common prefix)
    """
    
    def __init__(self, history: FileHistory):
        self._history = history
        # Slash commands for completion
        self._commands = [
            "/help", "/clear", "/history", "/session", "/exit", "/quit",
            "/new", "/model", "/models", "/tools", "/memory", "/sessions",
            "/resume", "/skills", "/config", "/compact", "/render",
            "/scheduler", "/scheduler list", "/scheduler add", "/scheduler remove", "/scheduler delete",
        ]
    
    def _longest_common_prefix(self, strings: list[str]) -> str:
        """Find longest common prefix of all strings."""
        logger.debug("Beginning of _longest_common_prefix")
        if not strings:
            return ""
        if len(strings) == 1:
            return strings[0]
        
        prefix = strings[0]
        for s in strings[1:]:
            while not s.startswith(prefix):
                prefix = prefix[:-1]
                if not prefix:
                    return ""
        return prefix
    
    def get_completions(self, document, complete_event: CompleteEvent):
        logger.debug("Beginning of get_completions")
        text = document.text_before_cursor
        text_lower = text.lower()
        
        if not text:
            return
        
        # Collect all matches
        matches = []
        
        # Complete slash commands
        if text.startswith("/"):
            for cmd in self._commands:
                if cmd.lower().startswith(text_lower):
                    matches.append(cmd)
        else:
            # Complete from history - match prefix
            seen = set()
            for entry in self._history.get_strings():
                if entry.lower().startswith(text_lower) and entry not in seen:
                    seen.add(entry)
                    matches.append(entry)
        
        if not matches:
            return
        
        if len(matches) == 1:
            # Single match - complete it
            yield Completion(matches[0], start_position=-len(text))
        else:
            # Multiple matches - find longest common prefix
            lcp = self._longest_common_prefix(matches)
            
            if len(lcp) > len(text):
                # Common prefix is longer than typed - complete to it
                yield Completion(lcp, start_position=-len(text))
            else:
                # Already at common prefix - show all matches
                for m in matches:
                    yield Completion(m, start_position=-len(text))


class KawaiiSpinner:
    """Animated spinner with kawaii faces."""

    FACES = ["(◕‿◕)", "(◠‿◠)", "(◕ᴗ◕)", "(≧◡≦)", "(◍•ᴗ•◍)"]
    THINKING = ["thinking", "pondering", "considering", "reflecting", "processing"]

    def __init__(self, console: Console):
        self._console = console
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._message = ""

    def start(self, message: str = "Thinking") -> None:
        logger.debug("Beginning of start")
        self._message = message
        self._running = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        logger.debug("Beginning of stop")
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._console.print("\r" + " " * 80 + "\r", end="")

    def update_message(self, message: str) -> None:
        logger.debug("Beginning of update_message")
        self._message = message

    def _spin(self) -> None:
        logger.debug("Beginning of _spin")
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        idx = 0
        while self._running:
            face = self.FACES[idx % len(self.FACES)]
            frame = frames[idx % len(frames)]
            self._console.print(
                f"\r{frame} {face} {self._message}...", end="", style="dim"
            )
            idx += 1
            time.sleep(0.1)


class GyrfalconCLI:
    """Interactive REPL built on prompt_toolkit with Rich display."""

    def __init__(
        self,
        model: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_turns: int | None = None,
        verbose: bool = False,
        resume: str | None = None,
        checkpoints: bool = False,
        toolsets: list[str] | None = None,
    ):
        self.console = Console()
        self.config = load_config()

        self.model = model or cfg_get("model.name", "")
        self.provider = provider or cfg_get("provider.name")
        self.api_key = api_key
        self.base_url = base_url
        self.max_turns = max_turns or cfg_get("agent.max_turns", 90)
        self.verbose = verbose
        self.resume = resume
        self.checkpoints = checkpoints
        self.toolsets = toolsets

        self._session_db = SessionDB()
        self._agent: Optional[AIAgent] = None
        self._spinner = KawaiiSpinner(self.console)
        self._streaming = cfg_get("display.streaming", True)
        self._last_response: str = ""  # stored for /render

        # Plugin manager
        self._plugin_manager = PluginManager()
        self._plugin_manager.discover_and_load()

        # User flow files (~/.gyrfalcon/flows/) — a separate, manifest-free
        # mechanism from plugins; see gyrfalcon/flow/registry.py.
        from gyrfalcon.flow.registry import discover_flows
        discover_flows()

        # Initialize MCP servers
        initialize_mcp_servers()

        # Resolve provider credentials
        self._resolve_credentials()

    def _resolve_credentials(self) -> None:
        """Resolve API credentials from provider or environment."""
        logger.debug("Beginning of _resolve_credentials")
        if self.provider:
            from gyrfalcon.providers import get_provider_profile
            profile = get_provider_profile(self.provider)
            if profile:
                if not self.base_url:
                    self.base_url = profile.base_url
                if not self.model:
                    self.model = profile.default_model or ""

        if self.provider == "copilot" or (not self.api_key and not self.base_url):
            from gyrfalcon.providers.copilot import get_copilot_credentials, is_authenticated, device_code_flow
            if is_authenticated():
                self.provider = "copilot"
                self.base_url, self.api_key = get_copilot_credentials()
                if not self.api_key:
                    # Token exchange failed — try re-authenticating
                    self.console.print(
                        "[bold yellow]⚠ Copilot token expired.[/bold yellow] "
                        "Re-authenticating..."
                    )
                    token = device_code_flow(print_fn=lambda msg: self.console.print(msg))
                    if token:
                        self.base_url, self.api_key = get_copilot_credentials()
                    if not self.api_key:
                        self.console.print(
                            "[bold red]✗ Re-authentication failed.[/bold red]\n"
                            "  Run [bold]gyrfalcon setup[/bold] to configure a provider."
                        )
                        raise SystemExit(1)
                if not self.model:
                    self.model = "gpt-4o"

        # Final fallback: check OPENAI_API_KEY from environment
        if not self.api_key:
            import os
            from gyrfalcon.config import get_env_value
            env_key = os.environ.get("OPENAI_API_KEY", "") or get_env_value("OPENAI_API_KEY") or ""
            if env_key:
                self.api_key = env_key
                if not self.base_url:
                    self.base_url = "https://api.openai.com/v1"
                if not self.model:
                    self.model = "gpt-4o"
            else:
                self.console.print(
                    "[bold red]✗ No API credentials found.[/bold red]\n"
                    "  Run [bold]gyrfalcon setup[/bold] to configure a provider,\n"
                    "  or set OPENAI_API_KEY in ~/.gyrfalcon/.env"
                )
                raise SystemExit(1)

    def _create_agent(self) -> AIAgent:
        """Create or reuse the AIAgent instance."""
        logger.debug("Beginning of _create_agent")
        if self._agent:
            return self._agent

        self._agent = AIAgent(
            base_url=self.base_url,
            api_key=self.api_key,
            provider=self.provider,
            model=self.model,
            max_iterations=self.max_turns,
            enabled_toolsets=self.toolsets,
            session_id=self.resume,
            session_db=self._session_db,
            checkpoints_enabled=self.checkpoints,
            tool_progress_callback=self._on_tool_progress if self.verbose else None,
            stream_delta_callback=self._on_stream_delta if self._streaming else None,
            plugin_manager=self._plugin_manager,
        )
        return self._agent

    def run(self) -> None:
        """Main REPL loop."""
        logger.debug("Beginning of run")
        self._print_banner()

        history_file = get_gyrfalcon_home() / ".cli_history"
        history = FileHistory(str(history_file))
        session = PromptSession(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=HistoryCompleter(history),
            complete_while_typing=False,  # Only complete on Tab
        )

        while True:
            try:
                user_input = session.prompt("\n❯ ").strip()
                if not user_input:
                    continue

                # Handle slash commands
                if user_input.startswith("/"):
                    if self._process_command(user_input):
                        continue

                # Process with agent
                self._handle_message(user_input)

            except KeyboardInterrupt:
                if self._agent:
                    self._agent.interrupt()
                    self.console.print("\n[dim]Interrupted[/dim]")
                continue
            except EOFError:
                self.console.print("\n[dim]Goodbye![/dim]")
                break

        self._cleanup()

    def _handle_message(self, message: str) -> None:
        """Send message to agent and display response."""
        logger.debug("Beginning of _handle_message")
        agent = self._create_agent()

        if not self._streaming:
            self._spinner.start("Thinking")

        try:
            result = agent.run_conversation(user_message=message)
            response = result.get("final_response", "")
        except Exception as e:
            response = f"Error: {str(e)}"
            logger.error(f"Agent error: {e}", exc_info=True)
        finally:
            if not self._streaming:
                self._spinner.stop()

        if self._streaming:
            self.console.print()  # newline after streamed tokens — text already printed by delta callback
        elif response:
            # Non-streaming: print plain text, no markdown rendering
            self.console.print()
            self.console.print(response, highlight=False)

        # Store for /render
        self._last_response = response

    def _process_command(self, command: str) -> bool:
        """Slash command dispatcher. Returns True if handled."""
        logger.debug("Beginning of _process_command")
        parts = command[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            self._cleanup()
            sys.exit(0)

        elif cmd == "help":
            self._print_help()
            return True

        elif cmd == "new":
            # Capture previous session ID for display before clearing
            prev_session = None
            if self._agent and self._agent.session_id:
                prev_session = self._agent.session_id

            # Full reset — drop agent, history, and resume pointer
            self._agent = None
            self.resume = None

            # Clear screen and print a fresh divider
            self.console.clear()
            self.console.rule("[dim]New Session[/dim]", style="dim")
            if prev_session:
                self.console.print(
                    f"[dim]Previous session {prev_session[:8]} ended. "
                    "All history cleared. Starting fresh.[/dim]\n"
                )
            else:
                self.console.print("[dim]Session cleared. Starting fresh.[/dim]\n")
            return True

        elif cmd == "model":
            if arg:
                self.model = arg
                self._agent = None
                from gyrfalcon.config import load_config, save_config
                config = load_config()
                config["model"]["name"] = arg
                save_config(config)
                self.console.print(f"[dim]Model set to: {arg}[/dim]")
            else:
                self.console.print(f"[dim]Current model: {self.model}[/dim]")
            return True

        elif cmd == "models":
            self._interactive_model_select()
            return True

        elif cmd == "sessions":
            sessions = self._session_db.list_sessions(limit=10)
            for s in sessions:
                title = s.get("title") or s["id"][:8]
                self.console.print(f"  {s['id'][:8]} | {title}")
            return True

        elif cmd == "resume":
            if arg:
                self.resume = arg
                self._agent = None
                self.console.print(f"[dim]Resuming session: {arg}[/dim]")
            return True

        elif cmd == "compact":
            self._streaming = not self._streaming
            mode = "streaming" if self._streaming else "compact"
            self.console.print(f"[dim]Display: {mode}[/dim]")
            return True

        elif cmd == "tools":
            from gyrfalcon.tools import registry
            tools = registry.get_tool_names()
            self.console.print(f"[dim]Available tools ({len(tools)}):[/dim]")
            for name in sorted(tools):
                entry = registry.get_entry(name)
                self.console.print(f"  {entry.emoji} {name} [{entry.toolset}]")
            return True

        elif cmd == "skills":
            from gyrfalcon.tools.skills_tool import discover_skills
            skills = discover_skills()
            self.console.print(f"[dim]Skills ({len(skills)}):[/dim]")
            for s in skills:
                self.console.print(f"  📚 {s['name']}: {s.get('description', '')}")
            return True

        elif cmd == "config":
            from gyrfalcon.gyrfalcon_constants import display_gyrfalcon_home
            self.console.print(f"[dim]Home: {display_gyrfalcon_home()}[/dim]")
            self.console.print(f"[dim]Model: {self.model}[/dim]")
            self.console.print(f"[dim]Provider: {self.provider or 'auto'}[/dim]")
            return True

        elif cmd in ("scheduler", "cron"):
            self._cmd_scheduler(arg)
            return True

        elif cmd == "render":
            if self._last_response:
                self.console.print()
                try:
                    md = Markdown(self._last_response)
                    self.console.print(Panel(md, border_style="dim", padding=(0, 1)))
                except Exception:
                    self.console.print(self._last_response)
            else:
                self.console.print("[dim]No response to render yet.[/dim]")
            return True

        elif cmd == "clear":
            self.console.clear()
            return True

        self.console.print(f"[dim]Unknown command: /{cmd}. Type /help for list.[/dim]")
        return True

    def _on_stream_delta(self, delta: str) -> None:
        """Streaming token callback."""
        logger.debug("Beginning of _on_stream_delta")
        self.console.print(delta, end="", highlight=False)

    def _on_tool_progress(self, tool_name: str, args: dict, status: str) -> None:
        """Tool progress callback."""
        logger.debug("Beginning of _on_tool_progress")
        if status == "start":
            preview = self._build_tool_preview(tool_name, args)
            self.console.print(f"\n[dim]🔧 {tool_name}: {preview}[/dim]")
        elif status == "complete":
            self.console.print(f"[dim]  ✓ {tool_name} done[/dim]")

    def _build_tool_preview(self, tool_name: str, args: dict) -> str:
        """One-line preview of tool call."""
        logger.debug("Beginning of _build_tool_preview")
        if tool_name == "terminal":
            return args.get("command", "")[:60]
        elif tool_name in ("read_file", "write_file"):
            return args.get("file_path", "")[:60]
        elif tool_name == "web_search":
            return args.get("query", "")[:60]
        return str(args)[:60]

    def _print_banner(self) -> None:
        """Print welcome banner."""
        logger.debug("Beginning of _print_banner")
        from gyrfalcon import __version__
        self.console.print(
            Panel(
                f"[bold]{get_app_name()}[/bold] v{__version__}\n"
                f"[dim]Model: {self.model or 'not configured'} | "
                f"Type /help for commands[/dim]",
                border_style="bright_yellow",
                padding=(0, 1),
            )
        )

    def _interactive_model_select(self) -> None:
        """Interactive model selection — shows models for current provider first."""
        logger.debug("Beginning of _interactive_model_select")
        from gyrfalcon.providers import list_providers, get_provider_profile

        providers = list_providers()
        if not providers:
            self.console.print("[red]No providers registered.[/red]")
            return

        # Determine current provider
        current_provider = self.provider
        if not current_provider:
            # Infer from base_url
            for name in providers:
                profile = get_provider_profile(name)
                if profile and profile.base_url and self.base_url and profile.base_url in self.base_url:
                    current_provider = name
                    break
            if not current_provider:
                current_provider = providers[0]

        profile = get_provider_profile(current_provider)
        if not profile:
            self.console.print("[red]Provider not found.[/red]")
            return

        # Fetch and show models for current provider
        self.console.print(f"\n[bold]Models for {profile.display_name}:[/bold]")
        models = profile.fetch_models()
        if not models:
            self.console.print("[dim]No models available.[/dim]")
            return

        for i, model_name in enumerate(models, 1):
            marker = " [green](current)[/green]" if model_name == self.model else ""
            self.console.print(f"  [cyan]{i}[/cyan]. {model_name}{marker}")
        self.console.print(f"  [cyan]p[/cyan]. Switch provider")
        self.console.print(f"  [cyan]0[/cyan]. Cancel")

        # Pick model
        try:
            choice = input("\nSelect model [number]: ").strip()
        except (EOFError, KeyboardInterrupt):
            self.console.print("")
            return

        if not choice or choice == "0":
            return

        if choice.lower() == "p":
            self._switch_provider()
            return

        try:
            midx = int(choice) - 1
            if midx < 0 or midx >= len(models):
                self.console.print("[red]Invalid selection.[/red]")
                return
        except ValueError:
            if choice in models:
                midx = models.index(choice)
            else:
                self.console.print("[red]Invalid selection.[/red]")
                return

        new_model = models[midx]
        self.model = new_model
        self.provider = current_provider
        self.base_url = profile.base_url
        self._agent = None

        # Persist to config.yaml
        from gyrfalcon.config import load_config, save_config
        config = load_config()
        config["model"]["name"] = new_model
        save_config(config)

        self.console.print(
            f"[green]✓[/green] Switched to [bold]{new_model}[/bold] "
            f"via {profile.display_name}"
        )

    def _switch_provider(self) -> None:
        """Switch provider, then select model."""
        logger.debug("Beginning of _switch_provider")
        from gyrfalcon.providers import list_providers, get_provider_profile

        providers = list_providers()
        self.console.print("\n[bold]Available providers:[/bold]")
        for i, name in enumerate(providers, 1):
            profile = get_provider_profile(name)
            marker = " [green](active)[/green]" if name == self.provider else ""
            display = profile.display_name if profile else name
            self.console.print(f"  [cyan]{i}[/cyan]. {display}{marker}")
        self.console.print(f"  [cyan]0[/cyan]. Cancel")

        try:
            choice = input("\nSelect provider [number]: ").strip()
        except (EOFError, KeyboardInterrupt):
            self.console.print("")
            return

        if not choice or choice == "0":
            return

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(providers):
                self.console.print("[red]Invalid selection.[/red]")
                return
        except ValueError:
            if choice in providers:
                idx = providers.index(choice)
            else:
                self.console.print("[red]Invalid selection.[/red]")
                return

        selected_provider = providers[idx]
        profile = get_provider_profile(selected_provider)
        if not profile:
            self.console.print("[red]Provider profile not found.[/red]")
            return

        # Now show models for the selected provider
        self.provider = selected_provider
        self.base_url = profile.base_url
        self._interactive_model_select()

    def _cmd_scheduler(self, arg: str) -> None:
        """Handle /scheduler [list|add|remove|delete] from the interactive REPL."""
        logger.debug("Beginning of _cmd_scheduler")
        import shlex
        from gyrfalcon.scheduler import job_store
        from gyrfalcon_cli.scheduler_cmd import _build_add_parser, _table_for_jobs, _fmt_dt

        parts = shlex.split(arg) if arg else []
        action = parts[0].lower() if parts else "list"

        # ── list ──────────────────────────────────────────────────────────────
        if action == "list":
            jobs = job_store.list_all()
            if not jobs:
                self.console.print("[dim]No scheduled jobs configured.[/dim]")
                return
            self.console.print(_table_for_jobs(jobs))

        # ── add ───────────────────────────────────────────────────────────────
        elif action == "add":
            parser = _build_add_parser()
            try:
                parsed = parser.parse_args(parts[1:])
            except SystemExit:
                return

            if not parsed.prompt and not parsed.skill:
                self.console.print("[red]Error: supply --prompt or --skill[/red]")
                return

            use_llm = not parsed.no_llm
            repeat_times = 1 if parsed.once else parsed.repeat

            # Detect NL schedule and warn user
            from gyrfalcon.scheduler import _try_regex_parse, _build_job
            is_nl = _try_regex_parse(parsed.schedule) is None
            if is_nl and use_llm:
                self.console.print(f"[dim]⟳ Translating schedule via LLM: '{parsed.schedule}'[/dim]")

            job: dict = {
                "name":             parsed.name,
                "schedule":         parsed.schedule,
                "prompt":           parsed.prompt,
                "skill":            parsed.skill,
                "model":            parsed.model or None,
                "provider":         parsed.provider or None,
                "repeat_times":     repeat_times,
                "once":             parsed.once,
                "workdir":          parsed.workdir or None,
                "deliver":          parsed.deliver,
                "enabled_toolsets": parsed.toolsets.split(",") if parsed.toolsets else None,
            }

            # Build first so we can show translation before persisting
            built = _build_job(job, use_llm=use_llm)
            sched = built["schedule"]
            if is_nl and use_llm:
                if sched.get("kind") == "unparsed":
                    self.console.print(
                        f"[yellow]⚠ Could not translate '{parsed.schedule}' — "
                        f"job saved but will not run until schedule is fixed.[/yellow]"
                    )
                else:
                    self.console.print(f"[dim]✓ Resolved to: {sched.get('display', '')}[/dim]")

            job_id = job_store.add(job)
            self.console.print(f"[green]✓ Created scheduled job: {job_id}[/green]")

            stored = job_store.get(job_id)
            if stored:
                raw = stored.get("schedule_raw", "")
                display = stored.get("schedule_display", "")
                if raw and raw != display:
                    self.console.print(f"  Schedule  : {raw}")
                    self.console.print(f"  Resolved  : {display}")
                else:
                    self.console.print(f"  Schedule  : {display}")
                if stored.get("next_run_at"):
                    self.console.print(f"  Next run  : {_fmt_dt(stored['next_run_at'])}")
                times = stored.get("repeat", {}).get("times")
                self.console.print(f"  Repeat    : {'once' if times == 1 else ('unlimited' if times is None else f'{times}x')}")
                if stored.get("skill"):
                    self.console.print(f"  Skill     : {stored['skill']}")
                elif stored.get("prompt"):
                    self.console.print(f"  Prompt    : {stored['prompt'][:60]}")

        # ── remove / delete ───────────────────────────────────────────────────
        elif action in ("remove", "delete"):
            if len(parts) < 2:
                self.console.print(f"[red]Usage: /scheduler {action} <job_id>[/red]")
                return
            job_id = parts[1]
            if job_store.remove(job_id):
                self.console.print(f"[green]✓ Removed job: {job_id}[/green]")
            else:
                self.console.print(f"[red]Job not found: {job_id}[/red]")

        # ── help / unknown ────────────────────────────────────────────────────
        else:
            self.console.print(
                "[bold]Scheduler commands:[/bold]\n"
                "  /scheduler [list]                             List all jobs\n"
                "  /scheduler add -s <sched> -p <prompt>        Add a prompt job\n"
                "  /scheduler add -s <sched> --skill <skill>    Add a skill job\n"
                "  /scheduler remove <id>                        Remove a job\n"
                "  /scheduler delete <id>                        Alias for remove\n\n"
                "[dim]Schedule: 30m  2h  1d  every 30m  '0 * * * *'  ISO datetime[/dim]\n"
                "[dim]Natural : 'every weekday at 9am'  'twice a week'  'daily at noon'[/dim]\n"
                "[dim]Options : --name --model --provider --repeat N --once --no-llm[/dim]"
            )


    def _print_help(self) -> None:
        """Print help text."""
        logger.debug("Beginning of _print_help")
        commands = [
            ("/help", "Show this help"),
            ("/new", "Start new session"),
            ("/model <name>", "Switch model directly"),
            ("/models", "Interactive model selection"),
            ("/sessions", "List recent sessions"),
            ("/resume <id>", "Resume session"),
            ("/tools", "List available tools"),
            ("/skills", "List available skills"),
            ("/config", "Show configuration"),
            ("/compact", "Toggle streaming/compact"),
            ("/scheduler", "Manage scheduled jobs"),
            ("/render", "Render last response as markdown"),
            ("/clear", "Clear screen"),
            ("/exit", "Exit"),
        ]
        self.console.print("\n[bold]Commands:[/bold]")
        for cmd, desc in commands:
            self.console.print(f"  {cmd:<36} {desc}")

    def _cleanup(self) -> None:
        """Cleanup on exit."""
        logger.debug("Beginning of _cleanup")
        from gyrfalcon.tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
        self._session_db.close()
