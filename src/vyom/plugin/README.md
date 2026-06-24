# VYOM QGIS Plugin

A thin QGIS client for the **VYOM** agentic-GIS platform. It talks to the VYOM FastAPI
server over REST, lets you ask natural-language disaster questions from inside QGIS, and
loads the agent's exported rasters straight onto the map canvas.

## What it does

| Panel control      | Endpoint              | Effect |
|--------------------|-----------------------|--------|
| **Connect**        | `GET /health`, `/events` | Verifies the API is up (DB + Gemini-key status) and loads the event list. |
| **Add AOI layer**  | (local)               | Adds the selected event's bbox as an in-memory WGS84 polygon layer and zooms to it. |
| **Ask**            | `POST /query`         | Runs the VYOM agent; shows the answer + full tool-call trace. |
| (automatic)        | `GET /exports/{file}` | Any `export_png` produced by the agent is fetched and added as a raster layer. |

All network calls run on a background thread, so QGIS never freezes while the agent works.

## Prerequisites

1. **Run the VYOM API** (from the project root, with `myenv` active):
   ```bash
   python -m vyom.api            # serves http://127.0.0.1:8000
   ```
   - `/health`, `/events`, `/scenes` work with no API key.
   - **`/query` needs a real `GEMINI_API_KEY`** in `.env` (otherwise the panel shows a
     clean *503 — key missing* message instead of an answer).
2. **QGIS 3.16+** (bundles Python 3 + PyQt). No extra Python packages are required by the
   plugin — it uses only the standard library plus QGIS's own Qt.

## Install

The plugin is the `src/vyom/plugin/` folder. Copy (or symlink) it into your QGIS plugins
directory under the name **`vyom`**:

- **Windows:** `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\vyom`
- **Linux:**   `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/vyom`
- **macOS:**   `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/vyom`

Convenience installer (copies the folder for you):

```bash
python -m vyom.plugin.install            # copy into the default QGIS profile
python -m vyom.plugin.install --symlink  # dev: symlink instead of copy
```

Then in QGIS: **Plugins → Manage and Install Plugins → Installed →** enable
*“VYOM — Agentic GIS”*. (It's marked *experimental*, so tick *Show experimental plugins*
in Settings if you don't see it.) A toolbar button and a **Plugins → VYOM** menu entry
appear; click either to toggle the dock.

## Usage

1. Click the **VYOM** toolbar button → the dock opens on the right.
2. Confirm the **URL** matches your server (default `http://127.0.0.1:8000`) → **Connect**.
3. Pick an event → **Add AOI layer** to frame the area on the map.
4. Type a question (e.g. *“How did water area change in the Kerala 2018 floods?”*) →
   **Ask**. Read the answer + tool trace; exported PNGs load automatically.

## Architecture notes

- `api_client.py` — Qt-free, stdlib-only REST client. Importable and unit-testable
  **without QGIS** (`import vyom.plugin.api_client`).
- `vyom_dockwidget.py` — the Qt panel; wraps `api_client` calls in a worker `QThread`.
- `vyom_plugin.py` — `initGui`/`unload` shell that toggles the dock.
- `__init__.py` — `classFactory(iface)` (imports QGIS lazily so the package imports
  cleanly in non-QGIS Python).

This mirrors VYOM's standing rule that tool/logic code stays importable and testable
independently of its server/runtime shell.
