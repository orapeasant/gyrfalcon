"""Daytona environment — execute commands in Daytona cloud workspaces."""

from __future__ import annotations

import logging
from typing import Any, Optional

from gyrfalcon.tools.environments import BaseEnvironment, ExecutionResult

logger = logging.getLogger(__name__)


class DaytonaEnvironment(BaseEnvironment):
    """Execute commands in Daytona cloud development workspaces.

    Requires Daytona CLI or SDK configured and a running workspace.
    """

    name = "daytona"

    def __init__(self, workspace_id: str = "", project: str = "", api_url: str = "") -> None:
        self._workspace_id = workspace_id
        self._project = project
        self._api_url = api_url or "http://localhost:3986"
        self._ready = False

    async def setup(self) -> None:
        """Connect to Daytona workspace."""
        import asyncio
        import subprocess

        # Verify daytona CLI is available
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["daytona", "workspace", "info", self._workspace_id],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                self._ready = True
                logger.info("Connected to Daytona workspace: %s", self._workspace_id)
            else:
                raise RuntimeError(f"Daytona workspace not available: {result.stderr}")
        except FileNotFoundError:
            raise RuntimeError("daytona CLI not found. Install from https://daytona.io")

    async def teardown(self) -> None:
        """Disconnect from workspace (workspace keeps running)."""
        self._ready = False

    async def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> ExecutionResult:
        """Execute command in Daytona workspace via CLI."""
        import asyncio
        import subprocess

        if not self._ready:
            await self.setup()

        cmd_parts = ["daytona", "workspace", "exec", self._workspace_id, "--"]
        if cwd:
            full_cmd = f"cd {cwd} && {command}"
        else:
            full_cmd = command

        cmd_parts.extend(["bash", "-c", full_cmd])

        effective_timeout = timeout or 300

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    cmd_parts,
                    capture_output=True, text=True,
                    timeout=effective_timeout,
                    env=env,
                ),
                timeout=effective_timeout + 5,
            )
            return ExecutionResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                timed_out=False,
            )
        except (asyncio.TimeoutError, subprocess.TimeoutExpired):
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {effective_timeout}s",
                timed_out=True,
            )
        except Exception as e:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                timed_out=False,
            )

    @property
    def is_connected(self) -> bool:
        return self._ready
