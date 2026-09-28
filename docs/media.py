"""Record the README's dashboard recording from the shipped baseline.

Serves `baselines/gpt-6-sol` from a scratch logs folder, opens the dashboard in Chrome, and
writes `docs/images/dashboard.webp`: the run table, one task opened, its pop-up scrolled
through, and the event timeline filtered to the agent's messages.

Run it again after a change to the dashboard, so the recording keeps matching the code:

    uv run --with playwright python docs/media.py

It needs Google Chrome, `ffmpeg`, and `img2webp` from libwebp (`brew install ffmpeg webp`).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import Page, sync_playwright

from daml_agent_benchmark.locations import configure
from daml_agent_benchmark.server.app import create_app, ensure_frontend_built

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "baselines" / "gpt-6-sol"
IMAGES = ROOT / "docs" / "images"
# The pop-up shows the task file's path, so the scratch folder gets a name that says what it is.
SCRATCH = Path("/tmp/daml-agent-benchmark-media")
# A real Canton Network task: splice's token-standard delivery-versus-payment test.
TASK = "splice/token-standard/splice-token-standard-test/daml/Splice/Tests/TestAmuletTokenDvP.daml"
VIEWPORT = {"width": 1440, "height": 900}


def serve(logs_dir: Path) -> str:
    """Start the dashboard on a free port, reading runs from `logs_dir`, and return its URL."""
    configure(logs_dir=logs_dir)
    ensure_frontend_built(rebuild=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    return f"http://127.0.0.1:{port}/"


def task_cell(page: Page):
    """The matrix cell of the example task, found by the label the cell announces."""
    return page.locator(f'[aria-label$="{TASK}"]').first


def scroll_modal(page: Page, steps: int, step_px: int, pause_ms: int) -> None:
    for _ in range(steps):
        page.locator(".ant-modal-wrap").evaluate(f"el => el.scrollBy({{top: {step_px}, behavior: 'smooth'}})")
        page.wait_for_timeout(pause_ms)


def recording(page: Page) -> None:
    page.wait_for_timeout(1500)
    task_cell(page).hover()
    page.wait_for_timeout(1000)
    task_cell(page).click()
    page.wait_for_selector(".ant-modal-content")
    page.wait_for_timeout(2000)
    scroll_modal(page, steps=5, step_px=220, pause_ms=450)
    tag = page.locator(".ant-modal-content").get_by_text("agent_message", exact=True).first
    tag.scroll_into_view_if_needed()
    page.wait_for_timeout(1000)
    tag.click()
    page.wait_for_timeout(1500)
    scroll_modal(page, steps=3, step_px=200, pause_ms=600)
    page.wait_for_timeout(1500)


def to_webp(video: Path, frames_dir: Path, out: Path) -> None:
    """An animated WebP from the recording: 12 frames a second, 1200 pixels wide.

    The first second is dropped: the recording starts before the page has laid out its table.
    """
    frames_dir.mkdir()
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", "1", "-i", str(video), "-vf", "fps=12,scale=1200:-1:flags=lanczos",
         str(frames_dir / "f%04d.png")],
        check=True,
    )
    frames = sorted(str(f) for f in frames_dir.glob("f*.png"))
    subprocess.run(["img2webp", "-loop", "0", "-lossy", "-q", "70", "-m", "6", "-d", "83", *frames, "-o", str(out)],
                   check=True, capture_output=True)


def main() -> None:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    shutil.copytree(BASELINE, SCRATCH / "logs" / BASELINE.name)
    IMAGES.mkdir(parents=True, exist_ok=True)
    url = serve(SCRATCH / "logs")
    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        browser = p.chromium.launch(channel="chrome")
        context = browser.new_context(viewport=VIEWPORT, record_video_dir=tmp, record_video_size=VIEWPORT)
        page = context.new_page()
        page.goto(url)
        page.wait_for_selector(f"text={BASELINE.name}")
        recording(page)
        video = Path(page.video.path())
        context.close()
        browser.close()
        to_webp(video, Path(tmp) / "frames", IMAGES / "dashboard.webp")
    shutil.rmtree(SCRATCH)
    out = IMAGES / "dashboard.webp"
    print(f"{out}: {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
