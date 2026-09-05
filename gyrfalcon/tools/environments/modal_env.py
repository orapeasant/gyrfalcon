"""Modal environment — execute commands in Modal cloud sandboxes."""

from __future__ import annotations

import logging
from typing import Any, Optional

from gyrfalcon.tools.environments import BaseEnvironment, ExecutionResult

logger = logging.getLogger(__name__)


class ModalEnvironment(BaseEnvironment):
    """Execute commands in Modal.com cloud sandboxes.

    Requires `modal` package and authenticated Modal account.
    """

    name = "modal"

    def __init__(self, image: str = "debian_slim", gpu: str = "", timeout: int = 600) -> None:
        self._image_name = image
        self._gpu = gpu
        self._timeout = timeout
        self._sandbox: Any = None

    async def setup(self) -> None:
        """Initialize Modal sandbox."""
        try:
            import modal
        except ImportError:
            raise RuntimeError("modal not installed. Run: pip install modal")

        app = modal.App.lookup("gyrfalcon-sandbox", create_if_missing=True)
        image = getattr(modal.Image, self._image_name)()

        kwargs: dict[str, Any] = {"image": image, "timeout": self._timeout}
        if self._gpu:
            kwargs["gpu"] = self._gpu

        self._sandbox = modal.Sandbox.create(app=app, **kwargs)
        logger.info("Modal sandbox created: %s", self._sandbox.object_id)

    async def teardown(self) -> None:
        """Terminate Modal sandbox."""
        if self._sandbox:
            try:
                self._sandbox.terminate()
            except Exception:
                pass
            self._sandbox = None

    async def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> ExecutionResult:
        """Execute command in Modal sandbox."""
        if not self._sandbox:
            await self.setup()

        full_cmd = command
        if cwd:
            full_cmd = f"cd {cwd} && {command}"

        try:
            process = self._sandbox.exec("bash", "-c", full_cmd)
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            exit_code = process.wait()

            return ExecutionResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
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
        return self._sandbox is not None
