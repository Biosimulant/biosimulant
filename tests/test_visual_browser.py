"""Optional installed Chromium acceptance; no downloads or external services.

BIOSIMULANT_PLAYWRIGHT_MODULE=/absolute/path/to/playwright python -m pytest
tests/test_visual_browser.py
"""

import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from biosim.labs_serve.server import LabServeSession, create_app
from tests.test_visual_contract import _gallery_lab


@pytest.mark.skipif(
    not os.getenv("BIOSIMULANT_PLAYWRIGHT_MODULE"),
    reason="Optional installed Playwright/Chromium acceptance",
)
def test_composed_run_renders_all_nine_types_in_real_browser(tmp_path):
    import uvicorn

    session = LabServeSession(_gallery_lab(tmp_path / "lab"), install_deps=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(session), log_level="error", access_log=False)
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "Local acceptance server did not start"
        checked = subprocess.run(
            [
                "node",
                str(Path(__file__).with_name("visual_browser_check.cjs")),
                f"http://127.0.0.1:{port}",
            ],
            check=False,
            timeout=80,
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive(), "Local acceptance server did not shut down"
