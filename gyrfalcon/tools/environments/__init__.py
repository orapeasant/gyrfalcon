"""Terminal execution environments — base ABC and local backend."""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home
from gyrfalcon.config import cfg_get


@dataclass
class ExecutionResult:
    """Structured result from command execution."""
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def output(self) -> str:
        """Combined output (stdout + stderr)."""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts)

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def get_sandbox_dir() -> Path:
    """Host-side root for all sandbox storage."""
    configured = cfg_get("terminal.sandbox_dir")
    if configured:
        p = Path(configured).expanduser()
    else:
        p = get_gyrfalcon_home() / "sandboxes"
    p.mkdir(parents=True, exist_ok=True)
    return p


class BaseEnvironment(ABC):
    """Abstract base class for terminal execution backends."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def execute(
        self,
        command: str,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> str:
        """Execute a command and return output."""
        return self._run_bash(command, timeout=timeout, cwd=cwd, env=env)

    @abstractmethod
    def _run_bash(
        self,
        cmd_string: str,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> str:
        ...

    @abstractmethod
    def cleanup(self) -> None:
        ...

    @property
    def name(self) -> str:
        return "base"


class LocalEnvironment(BaseEnvironment):
    """Direct host execution via subprocess."""

    def _run_bash(
        self,
        cmd_string: str,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> str:
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        result = subprocess.run(
            cmd_string,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout or 120,
            cwd=cwd or os.getcwd(),
            env=proc_env,
        )

        output = result.stdout
        if result.stderr:
            output += f"\n{result.stderr}"
        return output.strip()

    def cleanup(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "local"


class DockerEnvironment(BaseEnvironment):
    """Security-hardened container execution (cap-drop ALL, no-new-privileges)."""

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.image = config.get("image", "python:3.12-slim") if config else "python:3.12-slim"
        self.container_id: Optional[str] = None

    def _run_bash(
        self,
        cmd_string: str,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> str:
        docker_cmd = [
            "docker", "run", "--rm",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--network", "none",
        ]

        # Resource limits
        cpu = self.config.get("container_cpu", 1)
        memory = self.config.get("container_memory", 5120)
        docker_cmd.extend([f"--cpus={cpu}", f"--memory={memory}m"])

        if cwd:
            docker_cmd.extend(["-w", cwd])

        if env:
            for k, v in env.items():
                docker_cmd.extend(["-e", f"{k}={v}"])

        docker_cmd.extend([self.image, "bash", "-c", cmd_string])

        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout or 120,
        )

        output = result.stdout
        if result.stderr:
            output += f"\n{result.stderr}"
        return output.strip()

    def cleanup(self) -> None:
        if self.container_id:
            subprocess.run(["docker", "rm", "-f", self.container_id], capture_output=True)
            self.container_id = None

    @property
    def name(self) -> str:
        return "docker"


def get_environment(backend: str | None = None) -> BaseEnvironment:
    """Factory function for execution environments."""
    backend = backend or cfg_get("terminal.backend", "local")

    if backend == "local":
        return LocalEnvironment()
    elif backend == "docker":
        return DockerEnvironment(cfg_get("terminal", {}))
    else:
        # Fallback to local
        return LocalEnvironment()
