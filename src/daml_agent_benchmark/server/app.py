"""The FastAPI app behind the run dashboard.

`create_app()` builds it: the run routes, a health check, CORS for the Vite dev server, and
the built frontend from `frontend/dist` when that directory exists. `app` is the
one uvicorn loads by name. `main()` is the command line: it checks that the port is free,
builds the page with npm when `frontend/dist` is missing, then starts uvicorn.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from daml_agent_benchmark.constants import PACKAGE_DIR
from daml_agent_benchmark.server.routes import router

# The Vite project next to the package, and what `npm run build` writes there.
FRONTEND_DIR = PACKAGE_DIR.parents[1] / "frontend"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"
# The Vite dev server proxies the API from its own origin.
DEV_SERVER_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
DEFAULT_PORT = 8010


def create_app(frontend_dist: Path = FRONTEND_DIST_DIR) -> FastAPI:
    """The dashboard app. The built frontend is served from `frontend_dist` when it exists."""
    app = FastAPI(title="Daml Agent Benchmark", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_SERVER_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    started_at_utc = datetime.now(timezone.utc).isoformat()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "pid": os.getpid(), "started_at_utc": started_at_utc}

    app.include_router(router)
    if frontend_dist.is_dir():
        # The built page is `index.html` plus the bundle it names under `assets/`. Each
        # gets its own path, so nothing here depends on the order routes are registered in.
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(frontend_dist / "index.html")

    return app


app = create_app()


def ensure_frontend_built(frontend_dir: Path = FRONTEND_DIR, *, rebuild: bool = False) -> None:
    """Build the page with npm when `dist/` is missing, or when asked to rebuild.

    Needs Node.js. Without it, this says so and stops, since the server would otherwise
    start and answer `/` with a 404.
    """
    if (frontend_dir / "dist" / "index.html").exists() and not rebuild:
        return
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit(
            f"The dashboard page is not built and npm is not installed. Install Node.js and start again, "
            f"or build the page yourself with `npm install && npm run build` in {frontend_dir}."
        )
    if not (frontend_dir / "node_modules").is_dir():
        subprocess.run([npm, "install"], cwd=frontend_dir, check=True)
    subprocess.run([npm, "run", "build"], cwd=frontend_dir, check=True)


def ensure_port_free(host: str, port: int) -> None:
    """Stop at once when another program already listens on the port.

    This runs before the page is built, so a taken port does not cost the build time first.
    The socket uses the same address reuse as uvicorn's. So a port that a stopped server
    left in TIME_WAIT counts as free, as it does for uvicorn.
    """
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise SystemExit(
                f"Port {port} on {host} is not free ({exc.strerror}). Another server may be using it. "
                f"Start the dashboard on another port with --port, for example --port {port + 1}."
            ) from exc


def main(argv: list[str] | None = None) -> None:
    """Start the server from the command line, building the page first when it is missing."""
    parser = argparse.ArgumentParser(description="Serve the run dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"the port to serve on (default: {DEFAULT_PORT})")
    parser.add_argument("--rebuild", action="store_true", help="build the page again, after changes to frontend/")
    args = parser.parse_args(argv)
    ensure_port_free(args.host, args.port)
    ensure_frontend_built(rebuild=args.rebuild)
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)
