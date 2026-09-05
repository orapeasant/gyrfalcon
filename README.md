# Gyrfalcon Agent

A self-improving AI agent with persistent memory, session management, skill creation, multi-platform messaging, and pluggable infrastructure.

## Features

- **Core Agent**: Tool-calling conversation loop with context compression and budget tracking
- **Multiple UIs**: CLI (prompt_toolkit), TUI (Ink/React), Web Dashboard (React + FastAPI)
- **Plugin System**: General plugins, memory providers, model providers
- **MCP Support**: Connect external MCP tool servers
- **Skills**: LLM instruction sets with lifecycle management
- **Cron**: Scheduled autonomous agent tasks
- **Gateway**: Multi-platform message routing framework
- **Providers**: GitHub Copilot (device auth), AWS Bedrock, OpenAI, Anthropic

## Quick Start

```bash
# Install with uv
uv sync

# Run interactive CLI
gyrfalcon

# Run TUI
gyrfalcon --tui

# Run web dashboard
gyrfalcon dashboard

# Run setup wizard
gyrfalcon setup
```

## Configuration

Config lives in `~/.gyrfalcon/config.yaml`. Use `gyrfalcon setup` for interactive configuration.

## License

MIT
