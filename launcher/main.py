from __future__ import annotations

import argparse
import ctypes
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Protocol, TextIO

ERROR_ALREADY_EXISTS = 183


def runtime_data_dir(root: Path, *, frozen: bool) -> Path:
    override = os.getenv("PAPERAGENT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if frozen:
        local_app_data = os.getenv("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return (base / "PaperAgent" / "data").resolve()
    return (root.parent / "paperagent-data").resolve()


def append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp} {message}\n")


def write_state(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_state(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def state_is_healthy(state: dict[str, object] | None) -> bool:
    if not state or not isinstance(state.get("port"), int):
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{state['port']}/api/health", timeout=0.75
        ) as response:
            return int(response.status) == 200
    except OSError:
        return False


class BackendProcess(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class EmbeddedBackend:
    """Uvicorn lifecycle adapter used by the frozen Windows executable."""

    def __init__(self, host: str, port: int) -> None:
        diagnostic = os.getenv("PAPERAGENT_SMOKE_TRACE")

        def trace(message: str) -> None:
            if diagnostic:
                with Path(diagnostic).open("a", encoding="utf-8") as stream:
                    stream.write(f"{time.monotonic():.3f} {message}\n")

        trace("importing uvicorn")
        import uvicorn

        trace("importing app")
        from paperagent.api.server import app

        trace("initializing database")
        app.state.databases.initialize_global()
        trace("database initialized")
        trace("creating uvicorn server")
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            log_config=None,
            access_log=False,
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="off",
        )
        trace("uvicorn config created")
        self.server = uvicorn.Server(config)
        self.returncode: int | None = None
        trace("uvicorn server created")

        def run_server() -> None:
            try:
                self.server.run()
                self.returncode = 0
            except Exception as error:
                trace(f"uvicorn failed: {type(error).__name__}: {error}")
                self.returncode = 1

        self.thread = threading.Thread(target=run_server, name="paperagent-api", daemon=True)
        trace("uvicorn thread created")
        self.thread.start()

    def poll(self) -> int | None:
        if self.thread.is_alive():
            return None
        return self.returncode if self.returncode is not None else 1

    def terminate(self) -> None:
        self.server.should_exit = True

    def wait(self, timeout: float | None = None) -> int:
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("embedded-backend", timeout or 0)
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.server.force_exit = True
        self.server.should_exit = True


class SingleInstance:
    def __init__(self, name: str = "Local\\PaperAgent") -> None:
        self.name = name
        self.handle: int | None = None

    def __enter__(self) -> SingleInstance:
        if os.name != "nt":
            return self
        kernel32 = ctypes.windll.kernel32
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle or kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            if self.handle:
                kernel32.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError("PaperAgent is already running")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(port: int, process: BackendProcess, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"PaperAgent backend exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=0.5
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("PaperAgent backend did not become healthy")


def stop_process(process: BackendProcess) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def tray_exit(icon: object, process: BackendProcess) -> None:
    stop_process(process)
    icon.stop()  # type: ignore[attr-defined]


def run_tray(port: int, process: BackendProcess, stop_file: Path, log_dir: Path) -> None:
    import pystray
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 64), "#171717")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 8, 52, 56), radius=6, fill="#10a37f")
    draw.rectangle((20, 20, 44, 24), fill="white")
    draw.rectangle((20, 31, 44, 35), fill="white")

    def open_app(_icon: object, _item: object) -> None:
        webbrowser.open(f"http://127.0.0.1:{port}")

    def exit_app(icon: pystray.Icon, _item: object) -> None:
        tray_exit(icon, process)

    def open_logs(_icon: object, _item: object) -> None:
        if os.name == "nt":
            os.startfile(log_dir)
        else:
            webbrowser.open(log_dir.as_uri())

    icon = pystray.Icon(
        "PaperAgent",
        image,
        "PaperAgent",
        menu=pystray.Menu(
            pystray.MenuItem("打开 PaperAgent", open_app, default=True),
            pystray.MenuItem("打开日志目录", open_logs),
            pystray.MenuItem("退出", exit_app),
        ),
    )

    def monitor() -> None:
        while process.poll() is None and not stop_file.exists():
            time.sleep(0.25)
        if stop_file.exists():
            stop_process(process)
        icon.stop()

    threading.Thread(
        target=monitor, name="backend-monitor", daemon=True
    ).start()
    icon.run()


def wait_for_stop(process: BackendProcess, stop_file: Path) -> int:
    while process.poll() is None:
        if stop_file.exists():
            stop_process(process)
            return 0
        time.sleep(0.25)
    return process.returncode or 0


def request_stop(state_file: Path) -> int:
    state = read_state(state_file)
    if not state_is_healthy(state):
        state_file.unlink(missing_ok=True)
        return 1
    stop_file_value = state.get("stop_file") if state else None
    if not isinstance(stop_file_value, str):
        return 1
    stop_file = Path(stop_file_value)
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.touch()
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if not state_is_healthy(state):
            return 0
        time.sleep(0.2)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--logs", action="store_true")
    args = parser.parse_args()
    diagnostic = Path(os.getenv("TEMP", ".")) / "paperagent-smoke.log"

    def trace(message: str) -> None:
        if args.smoke_test:
            with diagnostic.open("a", encoding="utf-8") as stream:
                stream.write(f"{time.monotonic():.3f} {message}\n")

    if args.smoke_test:
        diagnostic.unlink(missing_ok=True)
        os.environ["PAPERAGENT_SMOKE_TRACE"] = str(diagnostic)
    trace("arguments parsed")
    frozen = bool(getattr(sys, "frozen", False))
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])).resolve()
    data_dir = runtime_data_dir(root, frozen=frozen)
    log_dir = data_dir / "logs"
    log_path = log_dir / "launcher.log"
    state_file = data_dir / "global" / "launcher-state.json"
    stop_file = data_dir / "global" / "launcher.stop"
    if args.status:
        state = read_state(state_file)
        print(json.dumps({"running": state_is_healthy(state), "state": state}, ensure_ascii=False))
        return 0 if state_is_healthy(state) else 1
    if args.stop:
        return request_stop(state_file)
    if args.logs:
        log_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(log_dir)
        else:
            webbrowser.open(log_dir.as_uri())
        return 0
    port = available_port()
    environment = os.environ.copy()
    environment["PAPERAGENT_PORT"] = str(port)
    environment["PAPERAGENT_PROJECT_ROOT"] = str(root)
    environment["PAPERAGENT_DATA_DIR"] = str(data_dir)
    if frozen:
        os.environ["PAPERAGENT_PORT"] = str(port)
        os.environ["PAPERAGENT_PROJECT_ROOT"] = str(root)
        os.environ["PAPERAGENT_DATA_DIR"] = str(data_dir)
    trace(f"root resolved: {root}")
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    try:
        instance = SingleInstance().__enter__()
    except RuntimeError:
        existing = read_state(state_file)
        if state_is_healthy(existing):
            existing_port = existing.get("port") if existing else None
            if not isinstance(existing_port, int):
                raise RuntimeError("PaperAgent instance state is invalid") from None
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{existing_port}")
            return 0
        raise
    try:
        stop_file.unlink(missing_ok=True)
        append_log(log_path, f"launcher starting pid={os.getpid()} frozen={frozen} port={port}")
        trace("single instance acquired")
        process: BackendProcess
        backend_log: TextIO | None = None
        if frozen:
            trace("creating embedded backend")
            process = EmbeddedBackend("127.0.0.1", port)
            trace("embedded backend thread started")
        else:
            log_dir.mkdir(parents=True, exist_ok=True)
            backend_log = log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "paperagent.api.server:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=root,
                env=environment,
                creationflags=creation_flags,
                stdout=backend_log,
                stderr=subprocess.STDOUT,
            )
        try:
            wait_for_health(port, process)
            write_state(
                state_file,
                {
                    "schema_version": 1,
                    "pid": os.getpid(),
                    "backend_pid": getattr(process, "pid", None),
                    "port": port,
                    "url": f"http://127.0.0.1:{port}",
                    "data_dir": str(data_dir),
                    "log_file": str(log_path),
                    "stop_file": str(stop_file),
                    "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
            )
            trace("health passed")
            append_log(log_path, "backend health check passed")
            if args.smoke_test:
                return 0
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{port}")
            if args.no_tray:
                return wait_for_stop(process, stop_file)
            run_tray(port, process, stop_file, log_dir)
            return 0
        finally:
            trace("stopping backend")
            stop_process(process)
            if backend_log is not None:
                backend_log.close()
            current = read_state(state_file)
            if current and current.get("pid") == os.getpid():
                state_file.unlink(missing_ok=True)
            stop_file.unlink(missing_ok=True)
            append_log(log_path, "launcher stopped")
            trace("backend stopped")
    finally:
        instance.__exit__(None, None, None)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except Exception as error:
        failure_log = Path(os.getenv("TEMP", ".")) / "paperagent-launcher-error.log"
        failure_log.write_text(
            f"{type(error).__name__}: {error}\n", encoding="utf-8"
        )
        raise SystemExit(1) from None
