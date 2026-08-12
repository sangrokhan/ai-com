<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# web

## Purpose

The console frontend: a React + three.js single-page app that renders agents as
characters seated at desks in an isometric office, polls/streams their status from the
API, and shows a login form when the session cookie is missing or invalid. Read-only —
there is no write path anywhere in this app.

## Key Files

| File | Description |
|------|--------------|
| `src/main.tsx` | Mounts `<App />` into `#root` |
| `src/App.tsx` | Top-level: login gate, WebGL-available check, header |
| `src/api/client.ts` | `login`, `fetchSnapshot`, `Snapshot`/`AgentView` types |
| `src/api/useSnapshot.ts` | `useSnapshot()` — initial fetch + SSE subscription with reconnect/backoff |
| `src/scene/Scene.ts` | `OfficeScene` — three.js scene: desks, characters, camera, anchors |
| `src/scene/layout.ts` | `gridSize`, `deskSlot`, `freeSlot`, `deskPosition` — deterministic desk placement |
| `src/scene/models.ts` | `loadModel` — GLTF loader with a placeholder-box fallback |
| `src/ui/Login.tsx` | Password form |
| `src/ui/OfficeView.tsx` | Mounts `OfficeScene` into the DOM, republishes label anchors every frame |
| `src/ui/Overlay.tsx` | `bubbleText`, name-pill/status-dot/speech-bubble DOM overlay |
| `src/ui/AgentTable.tsx` | Plain-table fallback when WebGL is unavailable |
| `vite.config.ts` | Dev-server proxy to `http://localhost:8000` for `/auth`, `/console`, `/tasks` |

## Build Commands

```bash
npm install         # once, or after package.json changes
npm run dev          # vite dev server with API proxy
npm run build        # tsc -b && vite build -> web/dist
npm run test         # vitest run
```

`web/dist` is what FastAPI serves: `aicom.inbound.app.create_app` mounts
`StaticFiles(directory=settings.console_static_dir, html=True)` at `/` when that
directory exists (`console_static_dir` defaults to `./web/dist`). There is no build step
in the Python test/lint/type-check commands — run `npm run build` yourself before
anything that needs the console to actually be served (e.g.
`tests/integration/test_console_smoke.py`).

`web/node_modules` and `web/dist` are gitignored; neither is committed.

## For AI Agents

### The camera does not move

`OfficeScene` positions the camera once in `centreCamera` (called from `setAgents` and
`resize`) and never in response to user input. There is **no** drag-to-rotate or
scroll-to-zoom, despite what an earlier draft of the design spec claimed — that claim was
corrected in `docs/superpowers/specs/2026-08-11-console-design.md` §10 (Out of scope).
Do not assume mouse/touch camera controls exist anywhere in this tree.

### Models are Kenney CC0 files committed under `web/public/models/`

`Scene.ts` loads `/models/desk.glb` and `/models/character.glb` via `loadModel`, which is
built to fail soft: a missing or unparsable `.glb` logs a warning and returns a plain
coloured box instead of throwing, so the scene never blanks out for a missing asset. As
of this task no `.glb` files are committed (see `web/public/models/README.md` for the
Kenney sources and licence — all CC0, no attribution required) — only its README is, so
the console currently renders placeholder boxes for every desk and character. Adding the
real files is a drop-in: put `desk.glb`, `character.glb`, and `floor.glb` at
`web/public/models/`.

### Desk placement is deterministic, not random

`layout.ts`'s `deskSlot` hashes the agent's UUID string (FNV-1a) to a grid cell so an
agent keeps the same desk across reloads and restarts without any server-side layout
state. `freeSlot` walks forward from that cell on a collision; see its docstring for the
invariant `Scene.ts` relies on (grid is always large enough that a walk never needs to
wrap indefinitely).

### WebGL absence falls back to a table, never a blank page

`App.tsx` checks `document.createElement("canvas").getContext("webgl2")` and renders
`AgentTable` instead of `OfficeView`/`Overlay` when it's unavailable. Both branches read
the same `Snapshot`; keep them in sync if you add a field to `AgentView`.

### Login state, not a route

There is no router. `App` renders `<Login />` whenever `useSnapshot()` reports
`needsLogin` (a 401 from `GET /console/state`), and the console proper otherwise. A
network/server error (as opposed to 401) keeps whatever is already on screen and shows
`error` in the header instead of bouncing to the login form — see `useSnapshot.ts`.

## Dependencies

### External

React 18 · three.js (`GLTFLoader` from `three/examples/jsm`) · Vite · Vitest · TypeScript.

### Internal

Talks only to the FastAPI app's `/auth/*` and `/console/*` routes (see
`src/aicom/auth/AGENTS.md` and `src/aicom/console/AGENTS.md`). Nothing in `web/` reaches
the database or any other backend module directly.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
