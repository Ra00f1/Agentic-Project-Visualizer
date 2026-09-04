# Agentic Project Visualizer

Windows-first desktop app that reads an agentic project's database and codebase and renders the whole system as a single interactive graph. A Tauri 2 + React 19 shell talks to a Python FastAPI sidecar that queries MongoDB and statically analyses the target project's source with `ast`.

The repo includes a synthetic `mock-agent-project` fixture — MongoDB seed + minimal codebase — so you can run everything end-to-end without pointing at a real project.

## Prerequisites

Installed once per machine:

- **Python 3.11** — the backend interpreter. Check with `py --list` (Windows) or `python3.11 --version`.
- **Node.js 20+** — for the desktop app. Check with `node -v`.
- **Rust toolchain** — Tauri compiles a Rust shell. Install from [rustup.rs](https://rustup.rs). Check with `rustc --version`.
- **uv** — Python package manager. Install: `winget install astral-sh.uv` (or [see docs](https://docs.astral.sh/uv/) on other platforms). Check with `uv --version`.
- **MongoDB Community** — the target database. Any local `mongod` on port 27017 works. Docker one-liner if you'd rather not install: `docker run -d -p 27017:27017 --name mock-mongo mongo:7`.
- **Windows only** — MSVC build tools for Rust: `winget install Microsoft.VisualStudio.2022.BuildTools --override "--wait --add Microsoft.VisualStudioComponent.VC.Tools.x86.x64"`.

## Getting the code

```powershell
git clone https://github.com/<your-user>/agentic-project-visualizer.git
cd agentic-project-visualizer
```

All commands below assume you're at the repo root.

## First-time setup

Run these once, top to bottom. Commands are shown for PowerShell; substitute forward slashes on macOS/Linux.

```powershell
# 1. Fix PowerShell so npm scripts can run (Windows, once per machine).
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 2. Seed the mock MongoDB fixture.
cd mock-agent-project
pip install motor pymongo
python mongo\seed.py --drop
cd ..

# 3. Install backend dependencies.
cd apps\backend
uv sync
cd ..\..

# 4. Install desktop dependencies.
cd apps\desktop
npm install
cd ..\..
```

## Daily startup

Two terminals. Keep both open while you work.

**Terminal 1 — backend (Python sidecar).**

```powershell
cd apps\backend
uv run uvicorn agentic_visualizer.main:app --port 8765
```

Wait for `Uvicorn running on http://127.0.0.1:8765`. Sanity check: `http://127.0.0.1:8765/health` should return `{"status":"ok"}`.

**Terminal 2 — desktop app.**

```powershell
cd apps\desktop
npm run tauri dev
```

First run compiles Rust — 5–10 minutes. Every run after is a few seconds. When the window opens, you land on the **Setup** screen: pick your MongoDB URI (`mongodb://localhost:27017`) and DB name (`mock_agent`), check which collections to include, optionally set a **Codebase root** to point at a project's source folder (leave blank to skip L2/L3), hit **Continue →**, and the graph renders.

## Using it against your own project

1. Point the Setup screen's MongoDB URI + DB at your real Mongo, and pick the collections that carry your workflows/agents/tools.
2. Set **Codebase root** to the folder containing your project's Python source. That enables L2 (tool → code function) and L3 (function → function call graph).
3. Hit **Continue →**. Tool resolution warnings — a small yellow ⚠ on a tool node — mean the DB names a symbol the scanner couldn't find in the source; right-click the tool and choose **Open in IDE** to inspect.

## Interacting with the graph

- **Click** a node to focus it — everything else dims.
- **Double-click** an expanded node to collapse its subtree.
- **Esc** clears focus and the search filter.
- **Search** (top-left) highlights nodes by name substring.
- **Tiered / Compact** toggle: Tiered uses shortest-path columns (stable when you open a subtree); Compact uses dagre's optimising layout (denser).
- **Reset layout** re-runs auto-layout, throwing away any manual drags.
- **Right-click a tool** → **Open in IDE** launches `vscode://` / `cursor://` on the resolved source file.

## Running tests

```powershell
cd apps\backend
uv run pytest -v
```

Unit tests always pass; integration tests need a seeded MongoDB running (step 2 above).

## Re-seeding the fixture after schema changes

Whenever `mock-agent-project/mongo/dump.json` changes:

```powershell
cd mock-agent-project
python mongo\seed.py --drop
cd ..
```

Then click **Refresh** in the Tauri window (or restart uvicorn if the backend also changed).

## Repo layout

```
agentic-project-visualizer/
├── apps/
│   ├── backend/          Python 3.11 · FastAPI · uv
│   └── desktop/          Tauri 2 · React 19 · Vite · Tailwind v4
├── packages/
│   └── shared-types/     TypeScript types mirroring Pydantic
├── mock-agent-project/   Synthetic fixture used for testing / demos
├── CLAUDE.md             Design contract
└── HANDOVER.md           Handover notes for the next contributor
```

## Troubleshooting

**Setup screen shows "MongoDB not reachable" / red error banner.**
`mongod` is not running. Start the service (`Get-Service MongoDB`, then `Start-Service MongoDB`) or the Docker container (`docker start mock-mongo`).

**Tauri window opens but graph fails to load.**
The backend isn't running or is on the wrong port. Confirm `http://127.0.0.1:8765/health` responds in a browser. The desktop app expects the backend on port 8765.

**`npm : File ... cannot be loaded because running scripts is disabled`.**
You skipped step 1 of first-time setup. Run `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` and answer `Y`.

**Rust compile fails on first `npm run tauri dev`.**
Usually missing MSVC build tools. Install: `winget install Microsoft.VisualStudio.2022.BuildTools --override "--wait --add Microsoft.VisualStudioComponent.VC.Tools.x86.x64"`.

**Backend won't start: `ModuleNotFoundError: motor`.**
Dependencies weren't synced. `cd apps\backend && uv sync`.

**Vite starts but the browser shows a blank page after edits.**
Sometimes Vite loses hot-reload after big config changes. Ctrl-C both terminals and start again from Daily startup.

**"Open in IDE" launches nothing / Windows asks about running other applications.**
Windows shows a one-time security prompt the first time a `vscode://` link is opened. Tick "Always allow" and it stops asking. VS Code / Cursor may also show their own "Do you trust the sender?" dialog — accept once and set `security.promptForLocalFileProtocolHandling` to `false` in your editor if you don't want to see it again.
