"""Runs a compiled virtual ESPHome device (host platform) as a subprocess, for end-to-end tests.

Adapted from the integration drafts' `ESPHomeRunner`: same idea (compiled binary + log buffer), reshaped as an
async, typed helper that can be killed and restarted mid-test. It never touches mDNS: see `mdns.py`.
"""

import asyncio
import contextlib
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SKETCHES_DIR = Path(__file__).parent / "sketches"


@dataclass(frozen=True)
class Sketch:
    yaml: str
    name: str  # `esphome.name` inside the yaml; also the build directory name
    port: int
    mac: str  # as advertised in mDNS TXT: lowercase hex, no separators
    encryption_key: str | None = None

    @property
    def binary(self) -> Path:
        return SKETCHES_DIR / ".esphome" / "build" / self.name / ".pioenvs" / self.name / "program"


ENCRYPTION_KEY = "kiO8WgmbyiIdaQ9fAFcZCdFTzRj6dzmfG/Pu3pbU9JI="  # `api.encryption.key` of encrypted.yaml

PLAIN = Sketch("virtual.yaml", "test_node", 6053, "983569abf679")
ENCRYPTED = Sketch("encrypted.yaml", "test_node_enc", 6054, "983569abf679", ENCRYPTION_KEY)


def build(sketch: Sketch) -> None:
    """Compile the sketch to a native binary (needs the `esphome` CLI on PATH). Takes a minute or two."""
    if shutil.which("esphome") is None:
        raise RuntimeError("`esphome` CLI not found: install it (e.g. `pipx install esphome`) to build virtual devices")
    subprocess.run(["esphome", "compile", str(SKETCHES_DIR / sketch.yaml)], check=True)


class VirtualDevice:
    def __init__(self, sketch: Sketch = PLAIN) -> None:
        self.sketch = sketch
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._workdir = Path(tempfile.mkdtemp(prefix="virtual-esphome-"))
        self._lines: list[str] = []

    @property
    def address(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return self.sketch.port

    @property
    def log(self) -> str:
        return "".join(self._lines)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(self, timeout: float = 10.0) -> None:
        if not self.sketch.binary.exists():
            raise FileNotFoundError(
                f"{self.sketch.binary} is not built: run `poetry run python -m tests.virtual.build`"
            )
        line_buffered = shutil.which("stdbuf") or shutil.which("gstdbuf")  # piped stdout is block-buffered otherwise
        cmd = [line_buffered, "-oL", str(self.sketch.binary)] if line_buffered else [str(self.sketch.binary)]
        self._proc = subprocess.Popen(
            cmd, cwd=self._workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        self._reader = threading.Thread(target=self._read_output, args=(self._proc,), daemon=True)
        self._reader.start()
        await self._wait_for_port(timeout)

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    await asyncio.to_thread(proc.wait, 5)
                if proc.poll() is None:
                    proc.kill()
                    await asyncio.to_thread(proc.wait)
            if self._reader is not None:
                self._reader.join(timeout=2)
                self._reader = None
            if proc.stdout is not None:
                proc.stdout.close()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def wait_log(self, text: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.log:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"{text!r} not found in device log:\n{self.log[-2000:]}")

    def _read_output(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self._lines.append(line)

    async def _wait_for_port(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                raise RuntimeError(f"virtual device exited early:\n{self.log}")
            with contextlib.suppress(OSError), socket.create_connection((self.address, self.port), timeout=0.2):
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"virtual device did not open port {self.port}:\n{self.log}")
