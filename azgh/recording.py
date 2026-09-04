from __future__ import annotations

import base64
from contextlib import contextmanager
import datetime as dt
import errno
import json
import os
from pathlib import Path
import sys
import time
import uuid
from typing import BinaryIO, Callable, Iterator


WRAPPER_PATH = Path(__file__).resolve().parents[1] / "gh"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def locked(lock_file: BinaryIO) -> Iterator[None]:
    """Lock a one-byte sidecar file on both POSIX and Windows."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class JsonlLogger:
    """Append complete, process-safe JSON records to one JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8", buffering=1)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_file = self._lock_path.open("a+b")
        if self._lock_path.stat().st_size == 0:
            self._lock_file.write(b"0")
            self._lock_file.flush()
        self._broken = False
        try:
            os.chmod(self.path, 0o600)
            os.chmod(self._lock_path, 0o600)
        except OSError:
            pass

    def write(self, record: dict[str, object]) -> None:
        if self._broken:
            return
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        try:
            with locked(self._lock_file):
                self._file.seek(0, os.SEEK_END)
                self._file.write(line + "\n")
                self._file.flush()
                os.fsync(self._file.fileno())
        except OSError as exc:
            self._broken = True
            print(f"az-gh: logging disabled: {exc}", file=sys.stderr)

    def close(self) -> None:
        self._file.close()
        self._lock_file.close()


def log_path() -> Path:
    configured = os.environ.get("AZ_GH_LOG_FILE")
    if configured:
        return Path(configured).expanduser()
    return WRAPPER_PATH.parent / "commands.jsonl"


def write_bytes(stream: BinaryIO, data: bytes) -> None:
    try:
        stream.write(data)
        stream.flush()
    except (BrokenPipeError, OSError) as exc:
        if isinstance(exc, BrokenPipeError) or exc.errno == errno.EPIPE:
            return
        raise


class Recorder:
    """Run one logical gh command while preserving the existing event schema."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.command_id = str(uuid.uuid4())
        self.sequence = 0
        self.started = time.monotonic()
        self.logger: JsonlLogger | None = None

    def emit(self, stream: str, data: bytes) -> None:
        destination = sys.stdout.buffer if stream == "stdout" else sys.stderr.buffer
        write_bytes(destination, data)
        self.sequence += 1
        if self.logger is not None:
            self.logger.write(
                {
                    "event": "output",
                    "id": self.command_id,
                    "sequence": self.sequence,
                    "timestamp": utc_now(),
                    "stream": stream,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                }
            )

    def text(self, stream: str, value: str) -> None:
        self.emit(stream, value.encode("utf-8"))

    def run(self, handler: Callable[[Callable[[str, bytes], None]], int]) -> int:
        try:
            try:
                self.logger = JsonlLogger(log_path())
                self.logger.write(
                    {
                        "event": "start",
                        "id": self.command_id,
                        "sequence": 0,
                        "timestamp": utc_now(),
                        "argv": self.argv,
                        "cwd": os.getcwd(),
                    }
                )
            except OSError as exc:
                print(f"az-gh: cannot open log file: {exc}", file=sys.stderr)

            exit_code = handler(self.emit)
        except BrokenPipeError:
            exit_code = 141
        except Exception as exc:
            message = str(exc)
            if not message.startswith("az-gh:"):
                message = f"az-gh: {message}"
            self.text("stderr", message + "\n")
            exit_code = getattr(exc, "exit_code", 1)
        finally:
            if self.logger is not None:
                self.logger.write(
                    {
                        "event": "result",
                        "id": self.command_id,
                        "sequence": self.sequence + 1,
                        "timestamp": utc_now(),
                        "exit_code": exit_code,
                        "duration_ms": round((time.monotonic() - self.started) * 1000, 3),
                    }
                )
                self.logger.close()
        return exit_code
