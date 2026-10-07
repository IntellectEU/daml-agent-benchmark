# Run dashboard

This is the web UI over the benchmark's runs. It shows every run as a row of a matrix, with
one cell per task. It opens a task in full: its grade, the agent's events, the token usage,
and the implementation files before and after the agent wrote them. You can archive and
delete runs from it.

The page is a Vite + React + TypeScript project. The server that it talks to is
`daml_agent_benchmark.server`, which is part of the package.

## Build and serve

```bash
uv sync
uv run python -m daml_agent_benchmark.server            # http://127.0.0.1:8010
```

On its first start the server builds the page with npm, so Node.js has to be installed.
The built page lives in `frontend/dist/`, which is not committed. After pulling changes
to `frontend/`, start the server once with `--rebuild`.

## Develop

Start the server as above. Then start the Vite dev server, which reloads on edits and
proxies API calls to the server:

```bash
cd frontend
npm run dev
```

The proxy targets port 8010. Point it elsewhere with `VITE_API_TARGET=http://127.0.0.1:8020 npm run dev`.

## API

- `GET /health`
- `GET /api/agent/matrix`: every run as a row of task cells, over the union of the runs' tasks. `include_archived=true` adds archived runs.
- `GET /api/agent/runs/{run_id}/tasks/detail?task_id=...`: one task in full.
- `POST /api/agent/runs/archive`: move runs to `z_archive/` under the logs directory.
- `POST /api/agent/runs/delete`: delete runs, active or archived.
- `GET /api/agent/mutations`: every task that has mutations, with each mutation's id, kind and origin.
- `GET /api/agent/mutations/{task file name}`: one task's mutations in full, with their validation where a report has one and the original text of the files they patch.

Runs are read from `locations.logs_dir`, which defaults to `logs/` in the repository root.
