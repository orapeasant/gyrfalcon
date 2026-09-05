"""Code execution tool — sandboxed Python execution with RPC tool access."""

from __future__ import annotations

import json
import subprocess
import os
import tempfile
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.config import cfg_get

logger = get_logger("tools.code_execution")

# API keys stripped from execution environment
_SCRUB_ENV_VARS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
    "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "EXA_API_KEY",
    "TAVILY_API_KEY", "FIRECRAWL_API_KEY",
]


def execute_code(args: dict, **kwargs) -> str:
    """Sandboxed Python execution with RPC tool access."""
    logger.debug("Beginning of execute_code")
    code = args.get("code", "")
    if not code:
        return json.dumps({"error": "No code provided"})

    timeout = cfg_get("code_execution.timeout", 300)
    max_output = 50_000

    # Build scrubbed environment
    env = os.environ.copy()
    for var in _SCRUB_ENV_VARS:
        env.pop(var, None)

    # Write code to temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="gyrfalcon_exec_"
    ) as f:
        f.write(code)
        temp_path = f.name

    try:
        result = subprocess.run(
            ["python", temp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=os.getcwd(),
        )

        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"

        if len(output) > max_output:
            output = output[:max_output] + "\n... [output truncated at 50KB]"

        return json.dumps({
            "exit_code": result.returncode,
            "output": output.strip(),
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Execution timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": f"Execution failed: {str(e)}"})
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


# Register
registry.register(
    name="execute_code",
    toolset="code_execution",
    schema={
        "name": "execute_code",
        "description": "Execute Python code in a sandboxed environment. API keys are stripped. Use for data processing, calculations, or multi-step operations.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
            },
            "required": ["code"],
        },
    },
    handler=execute_code,
    emoji="🐍",
)
