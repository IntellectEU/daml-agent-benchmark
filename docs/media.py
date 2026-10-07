"""Record the README's dashboard recordings from the shipped baselines.

Serves `baselines/` from a scratch logs folder, opens the dashboard in Chrome, and writes two
recordings:

- `docs/images/dashboard.webp`, from the implementation baseline: the run table, fitted to the
  width and back, one task opened, its pop-up scrolled through, and the event timeline filtered
  to the agent's messages.
- `docs/images/dashboard-test-generation.webp`, from the test-generation baseline: the
  test-generation tab with its catch counts, fitted to the width and back, the same task opened
  at its mutant-by-script matrix and its event timeline filtered to the agent's messages, and
  the mutation catalogue, scrolled through two of its tasks.

A caption in a strip below the page says what each stage shows, and a bar along the strip's
bottom edge shows how far the recording has played.

Run it again after a change to the dashboard, so the recordings keep matching the code:

    uv run --with playwright python docs/media.py [--only implementation|test-generation]

`--only` redoes one recording and leaves the other file as it is. The recordings play in real
time one after the other; turning them into WebP takes longer, so they are encoded in parallel.

It needs Google Chrome, `ffmpeg`, and `img2webp` from libwebp (`brew install ffmpeg webp`).
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn
from playwright.sync_api import Page, sync_playwright

from daml_agent_benchmark.locations import configure
from daml_agent_benchmark.server.app import create_app, ensure_frontend_built

ROOT = Path(__file__).resolve().parents[1]
BASELINES = ROOT / "baselines"
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


# The caption strip's height. The recording's window is this much taller than VIEWPORT, and
# the strip fills the extra space below the page, so a caption never covers the dashboard.
CAPTION_STRIP = 64
# Installed in every page before the dashboard loads: the strip, and a style that ends the
# page and its pop-ups above it.
CAPTION_SETUP_JS = f"""
document.addEventListener('DOMContentLoaded', () => {{
  const style = document.createElement('style')
  style.textContent = `
    body {{ padding-bottom: {CAPTION_STRIP}px; box-sizing: border-box; }}
    .ant-modal-wrap, .ant-modal-mask {{ bottom: {CAPTION_STRIP}px !important; }}
    #media-caption {{
      position: fixed; left: 0; right: 0; bottom: 0; height: {CAPTION_STRIP}px; z-index: 100000;
      display: flex; align-items: center; justify-content: center; padding: 0 40px;
      background: #0f172a; color: #fff; font: 500 21px/1.3 system-ui, -apple-system, sans-serif;
      text-align: center; pointer-events: none;
    }}
    #media-caption span {{ transition: opacity 250ms; }}`
  document.head.appendChild(style)
  const strip = document.createElement('div')
  strip.id = 'media-caption'
  strip.appendChild(document.createElement('span'))
  document.body.appendChild(strip)
}})
"""
CAPTION_JS = """text => {
  const span = document.querySelector('#media-caption span')
  span.style.opacity = '0'
  setTimeout(() => { span.textContent = text; span.style.opacity = '1' }, 250)
}"""


def caption(page: Page, text: str) -> None:
    """Show `text` in the caption strip below the page; an empty text leaves the strip blank."""
    page.evaluate(CAPTION_JS, text)


def fit_switch(page: Page):
    return page.locator(".label-wrap", has_text="Fit tasks to width").get_by_role("switch")


def scroll_modal(page: Page, steps: int, step_px: int, pause_ms: int) -> None:
    for _ in range(steps):
        page.locator(".ant-modal-wrap").evaluate(f"el => el.scrollBy({{top: {step_px}, behavior: 'smooth'}})")
        page.wait_for_timeout(pause_ms)


def implementation_recording(page: Page, clock: float) -> list[tuple[float, float]]:
    caption(page, "Each row is a run and each column a task, coloured by how the task ended")
    page.wait_for_timeout(3500)
    fit_switch(page).click()
    caption(page, "Fit tasks to width: every task in view, the cells reduced to their colours")
    page.wait_for_timeout(3500)
    fit_switch(page).click()
    caption(page, "Click a cell to open the task")
    page.wait_for_timeout(1500)
    task_cell(page).hover()
    page.wait_for_timeout(1000)
    task_cell(page).click()
    page.wait_for_selector(".ant-modal-content")
    caption(page, "The task's grade, the files the agent wrote, and what the agent did")
    page.wait_for_timeout(2000)
    scroll_modal(page, steps=5, step_px=220, pause_ms=450)
    tag = page.locator(".ant-modal-content").get_by_text("agent_message", exact=True).first
    tag.scroll_into_view_if_needed()
    page.wait_for_timeout(1000)
    tag.click()
    caption(page, "The agent's event timeline, filtered to its messages")
    page.wait_for_timeout(1500)
    scroll_modal(page, steps=3, step_px=200, pause_ms=600)
    page.wait_for_timeout(2000)
    return []


def test_generation_recording(page: Page, clock: float) -> list[tuple[float, float]]:
    """Returns the stretch to cut: the catalogue loading its syntax highlighter, which the
    browser does on first use and the recording would otherwise show as a blank page.

    The captions say "planted bugs" for the mutants: the recording is the first thing a
    newcomer sees, before any of the benchmark's own terms are explained."""
    page.wait_for_timeout(1000)
    page.get_by_text("Test generation", exact=True).click()
    page.wait_for_selector(".agent-matrix-catch")
    caption(page, "Test generation: the agent writes the tests, and each cell counts how many planted bugs they caught")
    page.wait_for_timeout(4000)
    fit_switch(page).click()
    caption(page, "Fit tasks to width: every task in view, the cells reduced to their colours")
    page.wait_for_timeout(3500)
    fit_switch(page).click()
    caption(page, "Click a cell to open the task")
    page.wait_for_timeout(1500)
    task_cell(page).hover()
    page.wait_for_timeout(1000)
    task_cell(page).click()
    page.wait_for_selector(".ant-modal-content")
    caption(page, "Every planted bug against every test the agent wrote: a test that fails on a bug has caught it")
    page.wait_for_timeout(1500)
    matrix = page.locator(".agent-mutant-matrix").first
    matrix.wait_for()
    matrix.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'start'})")
    page.wait_for_timeout(4500)
    tag = page.locator(".ant-modal-content").get_by_text("agent_message", exact=True).first
    tag.scroll_into_view_if_needed()
    page.wait_for_timeout(1000)
    tag.click()
    caption(page, "The agent's event timeline, filtered to its messages")
    page.wait_for_timeout(1500)
    scroll_modal(page, steps=3, step_px=200, pause_ms=600)
    page.wait_for_timeout(2000)
    # Off the pop-up first, or a tooltip under the pointer stays on screen after it closes.
    page.mouse.move(VIEWPORT["width"] - 10, 10)
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)
    page.get_by_text("Mutation catalogue →").click()
    caption(page, "Every bug the benchmark plants in the code: what it changes, and which of the original tests it breaks")
    loading = time.monotonic() - clock
    page.wait_for_selector('.mc-sbs span[style*="color"]')
    page.wait_for_timeout(300)
    loaded = time.monotonic() - clock
    page.wait_for_timeout(3500)
    scroll_to_mutation(page, 1)
    page.wait_for_timeout(3000)
    page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
    page.wait_for_timeout(1000)
    other = page.locator(".mc-index li button", has_text=TASK.rsplit("/", 1)[1].removesuffix(".daml")).first
    other.scroll_into_view_if_needed()
    page.wait_for_timeout(600)
    other.click()
    # Off the task list, or the button's tooltip covers the list while the task loads.
    page.mouse.move(VIEWPORT["width"] - 10, VIEWPORT["height"] // 2)
    caption(page, "Each task has its own planted bugs, here those of the task opened above")
    page.wait_for_selector('.mc-task-path:has-text("TestAmuletTokenDvP")')
    page.wait_for_selector('.mc-sbs span[style*="color"]')
    page.wait_for_timeout(2500)
    scroll_to_mutation(page, 1)
    page.wait_for_timeout(2500)
    scroll_to_mutation(page, 2)
    page.wait_for_timeout(2500)
    return [(loading + 0.2, loaded)]


def scroll_to_mutation(page: Page, index: int) -> None:
    """Scroll the catalogue smoothly to the task's mutation at `index`, counting from 0."""
    page.locator(".mc-mut").nth(index).evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'start'})")


# Each recording: its name for `--only`, the baseline run it shows, how to drive the page, and
# the file it becomes. The first one's baseline is the run every recording waits for on load.
RECORDINGS = (
    ("implementation", "gpt-6-sol", implementation_recording, "dashboard.webp"),
    ("test-generation", "gpt-6-sol-test-generation", test_generation_recording, "dashboard-test-generation.webp"),
)


# The progress bar along the bottom edge of the caption strip, which shows how far the
# recording has played and where it loops.
PROGRESS_BAR_HEIGHT = 5
PROGRESS_BAR_COLOUR = "0x3b82f6"


def to_webp(video: Path, frames_dir: Path, out: Path, cuts: list[tuple[float, float]]) -> None:
    """An animated WebP from the recording: 12 frames a second, 1200 pixels wide, with the
    `cuts` (seconds from the start of the recording) left out and a progress bar drawn in.

    The first second is always cut: the recording starts before the page has laid out its table.
    """
    frames_dir.mkdir()
    keep = "".join(f"*not(between(t,{start:.2f},{end:.2f}))" for start, end in [(0.0, 1.0), *cuts])
    select = f"select='1{keep}',setpts=N/FRAME_RATE/TB,"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf", f"{select}fps=12,scale=1200:-1:flags=lanczos",
         str(frames_dir / "raw%04d.png")],
        check=True,
    )
    # A second pass, once the number of frames is known: frame n of the total gets a bar
    # (n + 1) / total of the width, so the last frame shows it full.
    total = len(list(frames_dir.glob("raw*.png")))
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-framerate", "12", "-i", str(frames_dir / "raw%04d.png"),
         "-f", "lavfi", "-i", f"color=c={PROGRESS_BAR_COLOUR}:s=1200x{PROGRESS_BAR_HEIGHT}",
         "-filter_complex", f"[0][1]overlay=x='-w+w*(n+1)/{total}':y=H-h:eval=frame:shortest=1",
         str(frames_dir / "f%04d.png")],
        check=True,
    )
    frames = sorted(str(f) for f in frames_dir.glob("f*.png"))
    subprocess.run(["img2webp", "-loop", "0", "-lossy", "-q", "70", "-m", "6", "-d", "83", *frames, "-o", str(out)],
                   check=True, capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=[name for name, *_ in RECORDINGS], help="make this recording only")
    args = parser.parse_args()
    chosen = [r for r in RECORDINGS if args.only in (None, r[0])]

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    # Every baseline is served, whichever recording is made: each opens on the implementation tab.
    for _, run, _, _ in RECORDINGS:
        shutil.copytree(BASELINES / run, SCRATCH / "logs" / run)
    IMAGES.mkdir(parents=True, exist_ok=True)
    url = serve(SCRATCH / "logs")
    with tempfile.TemporaryDirectory() as tmp:
        videos = []  # (video, cuts, output file) per recording, encoded once all are recorded
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            for name, _, drive, file_name in chosen:
                # A fresh context per recording, so each starts on the dashboard's default tab.
                window = {"width": VIEWPORT["width"], "height": VIEWPORT["height"] + CAPTION_STRIP}
                context = browser.new_context(
                    viewport=window, record_video_dir=Path(tmp) / name, record_video_size=window
                )
                context.add_init_script(CAPTION_SETUP_JS)
                page = context.new_page()
                # The video starts with the page; times measured from here locate the cuts in it.
                clock = time.monotonic()
                page.goto(url)
                page.wait_for_selector(f"text={RECORDINGS[0][1]}")
                cuts = drive(page, clock)
                video = Path(page.video.path())
                context.close()
                videos.append((video, cuts, IMAGES / file_name, Path(tmp) / name / "frames"))
            browser.close()

        def encode(video: Path, cuts: list[tuple[float, float]], out: Path, frames_dir: Path) -> Path:
            to_webp(video, frames_dir, out, cuts)
            return out

        # Encoding is the slow part and each `img2webp` runs on one core, so the files are made side by side.
        with ThreadPoolExecutor() as pool:
            for out in pool.map(lambda v: encode(*v), videos):
                print(f"{out}: {out.stat().st_size / 1e6:.1f} MB")
    shutil.rmtree(SCRATCH)


if __name__ == "__main__":
    main()
