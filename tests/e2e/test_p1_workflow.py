from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_health(port: int, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=0.5
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("test server did not become healthy")


def create_workspace(page: Page, project_name: str, conversation_name: str = "主会话") -> None:
    page.once("dialog", lambda dialog: dialog.accept(project_name))
    page.get_by_role("button", name=re.compile("新建项目")).click()
    expect(page.get_by_text(re.compile("项目已创建"))).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept(conversation_name))
    page.get_by_role("button", name=re.compile("新建会话")).click()
    expect(page.get_by_text("新会话已创建")).to_be_visible()


@contextmanager
def running_app(tmp_path: Path) -> Iterator[str]:
    root = Path(__file__).resolve().parents[2]
    port = free_port()
    environment = os.environ.copy()
    environment["PAPERAGENT_PORT"] = str(port)
    environment["PAPERAGENT_PROJECT_ROOT"] = str(root)
    environment["PAPERAGENT_DATA_DIR"] = str(tmp_path / "data")
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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_health(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def test_launcher_equivalent_create_reload_history(tmp_path: Path) -> None:
    with running_app(tmp_path) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url)
        prompt = "P1 端到端持久化项目"
        create_workspace(page, prompt)
        page.get_by_label("描述你的论文或报告需求").fill(prompt)
        page.get_by_role("button", name="发送").click()
        expect(page.get_by_text(re.compile("已保存"))).to_be_visible(timeout=10_000)
        page.reload()
        expect(page.get_by_role("button", name=prompt)).to_be_visible(timeout=10_000)
        browser.close()
