import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from launcher.main import SingleInstance, available_port, runtime_data_dir, tray_exit


def test_available_port_is_local_and_bindable() -> None:
    port = available_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def test_runtime_data_dir_is_stable_for_frozen_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source" / "paperagent"
    root.mkdir(parents=True)
    monkeypatch.delenv("PAPERAGENT_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local App Data"))
    assert runtime_data_dir(root, frozen=False) == tmp_path / "source" / "paperagent-data"
    assert runtime_data_dir(root, frozen=True) == (
        tmp_path / "Local App Data" / "PaperAgent" / "data"
    )
    override = tmp_path / "用户数据"
    monkeypatch.setenv("PAPERAGENT_DATA_DIR", str(override))
    assert runtime_data_dir(root, frozen=True) == override


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows mutex")
def test_single_instance_mutex_rejects_second_owner() -> None:
    name = f"Local\\PaperAgentTest-{uuid4()}"
    with SingleInstance(name), pytest.raises(RuntimeError), SingleInstance(name):
        pass


def test_tray_exit_stops_backend_without_orphan() -> None:
    class FakeIcon:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    icon = FakeIcon()
    tray_exit(icon, process)
    assert process.poll() is not None
    assert icon.stopped
