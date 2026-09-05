"""SSH environment — execute commands on remote hosts via SSH."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from gyrfalcon.tools.environments import BaseEnvironment, ExecutionResult

logger = logging.getLogger(__name__)


@dataclass
class SSHConfig:
    """SSH connection configuration."""
    host: str
    port: int = 22
    user: str = ""
    key_file: str = ""
    password: str = ""
    connect_timeout: int = 10
    command_timeout: int = 300


class SSHEnvironment(BaseEnvironment):
    """Execute commands on remote hosts via SSH using asyncssh."""

    name = "ssh"

    def __init__(self, config: SSHConfig) -> None:
        self.config = config
        self._conn: Any = None

    async def setup(self) -> None:
        """Establish SSH connection."""
        try:
            import asyncssh
        except ImportError:
            raise RuntimeError("asyncssh not installed. Run: pip install asyncssh")

        kwargs: dict[str, Any] = {
            "host": self.config.host,
            "port": self.config.port,
            "connect_timeout": self.config.connect_timeout,
            "known_hosts": None,  # Accept all for now
        }
        if self.config.user:
            kwargs["username"] = self.config.user
        if self.config.key_file:
            kwargs["client_keys"] = [self.config.key_file]
        if self.config.password:
            kwargs["password"] = self.config.password

        self._conn = await asyncssh.connect(**kwargs)
        logger.info("SSH connected to %s:%d", self.config.host, self.config.port)

    async def teardown(self) -> None:
        """Close SSH connection."""
        if self._conn:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    async def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> ExecutionResult:
        """Execute command over SSH."""
        if not self._conn:
            await self.setup()

        full_cmd = command
        if cwd:
            full_cmd = f"cd {cwd} && {command}"
        if env:
            env_prefix = " ".join(f"{k}={v}" for k, v in env.items())
            full_cmd = f"{env_prefix} {full_cmd}"

        effective_timeout = timeout or self.config.command_timeout

        try:
            result = await asyncio.wait_for(
                self._conn.run(full_cmd, check=False),
                timeout=effective_timeout,
            )
            return ExecutionResult(
                exit_code=result.exit_status or 0,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                timed_out=False,
            )
        except asyncio.TimeoutError:
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

    async def write_file(self, path: str, content: str) -> None:
        """Write file on remote host via SFTP."""
        if not self._conn:
            await self.setup()
        async with self._conn.start_sftp_client() as sftp:
            async with sftp.open(path, "w") as f:
                await f.write(content)

    async def read_file(self, path: str) -> str:
        """Read file from remote host via SFTP."""
        if not self._conn:
            await self.setup()
        async with self._conn.start_sftp_client() as sftp:
            async with sftp.open(path, "r") as f:
                return await f.read()

    @property
    def is_connected(self) -> bool:
        return self._conn is not None
