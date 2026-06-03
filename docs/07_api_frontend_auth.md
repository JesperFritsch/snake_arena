# API, Frontend, and Authentication — Design Decisions

This document captures the decisions made for the user-facing layer of the platform — the FastAPI backend, the React frontend, and the Clerk-based authentication. It complements the existing architecture docs by focusing specifically on the surface that users interact with.

## Context

By the time of writing, the platform has:

- A daemon-mode runner that executes matches in sandboxed Docker containers
- An orchestrator that polls a `match_jobs` table and dispatches work to the runner
- A `sa_common` package with shared types, DB models, and queue helpers
- A Postgres schema covering users, projects, code versions, submissions, matches, match participants, and match jobs
- A working enqueue CLI for manually queueing matches

The work captured below adds:

1. A REST API in front of the existing queue + DB
2. A React-based web frontend
3. Authentication via Clerk + GitHub OAuth
4. Abuse-prevention defenses appropriate to a single-VM, multi-user platform

---

## API

### Package layout

The API lives in a new monorepo package: `services/api/`. It's a uv-managed Python package alongside `runner`, `orchestrator`, `builder`, and `sa_common`. It imports from `sa_common` for all DB access, types, and queue helpers.

The API does **not** run business logic — it's a thin HTTP layer over the existing `sa_common` functions. Match orchestration stays in the orchestrator; agent building stays in the builder; the API only writes/reads DB rows and streams artifacts.

### Framework: FastAPI

Chosen because the rest of the codebase is Python, `sa_common` types and helpers are directly importable, and Pydantic (already in use for `SimArgs`, `MatchResult`) is FastAPI's native validation layer.

- **Pydantic** models for all request/response schemas. Existing types from `sa_common` are exposed directly where appropriate.
- **`psycopg_pool.ConnectionPool`** for connection pooling — first time this is needed in the codebase, since the runner has only ever opened one connection per match. The pool lives at module level in the API and is exposed via a FastAPI `Depends(get_db)` dependency.
- **uvicorn** as the ASGI server in dev; same in production behind Cloudflare.

### Why not other backends

- **Go / Rust**: would require duplicating `sa_common`, no performance benefit (the bottleneck is Docker container startup, not API latency).
- **Node**: shares language with the frontend, but `openapi-typescript` already solves the shared-types story language-agnostically; adopting Node would split the Python codebase for no gain.

### Endpoint surface (preliminary)

Grouped roughly by ship-ability. Specific paths and shapes get locked in per implementation chunk.

**Read-only over existing data (ship first):**

- `GET /matches/jobs` — list match jobs with status filter
- `GET /matches` — list completed matches
- `GET /matches/{id}` — match detail with participants
- `GET /matches/{id}/replay` — stream the replay file
- `GET /matches/{id}/analysis` — analysis JSON
- `GET /matches/{id}/logs/{agent_name}` — per-agent log

**Match writes:**

- `POST /matches/jobs` — `{ submission_ids, sim_args }`, returns the queued job

**Submissions and projects (depends on build_jobs design — see Open Decisions):**

- `GET /projects` — list user's projects
- `POST /projects` — create
- `GET /projects/{id}/versions` — list code versions
- `POST /projects/{id}/versions` — save new version, no build
- `POST /submissions` — `{ code_version_id }`, enqueues a build, returns submission with `status='building'`
- `GET /submissions` — list user's submissions
- `GET /submissions/{id}` — submission detail

### OpenAPI generation

FastAPI generates OpenAPI from Pydantic models and route signatures automatically — no manual schema work. The frontend consumes this schema via `openapi-typescript` to generate TypeScript types and an API client. The contract between frontend and backend stays in sync as long as the codegen is run (in CI or as a pre-commit hook).

### Replay serving

Replays currently live on local disk in the artifacts directory. The API streams them via `FileResponse` (or `StreamingResponse` for larger files). The implementation is wrapped in a `get_replay_stream(match_id) -> IO[bytes]` function so swapping disk for R2 later is a one-function change, no route impact.

---

## Frontend

### Package layout

Top-level `frontend/` directory. Independent toolchain (Node/npm), independent build.

The "frontend inside the API package" alternative was considered and rejected: the toolchains don't share anything (Python uv vs Node npm), so colocation buys nothing and costs cognitive overhead. The earlier "frontend inside API" idea only made sense for a non-Node path (Jinja + HTMX), which was rejected once "modern + mobile + LLM-built" was set as the goal.

### Stack

- **Vite** — build tool and dev server
- **React 19** + **TypeScript** in strict mode
- **Tailwind CSS** for styling
- **shadcn/ui** for components (copy-paste, not a dependency — components live in `src/components/ui/`)
- **React Router** for client-side routing
- **`openapi-typescript`** for API type generation from FastAPI's OpenAPI schema
- A thin fetch wrapper using the generated types (likely `ky` or hand-rolled around `fetch`)
- **`@clerk/clerk-react`** for auth components and session management

### Why not Next.js

Next.js conflates frontend and backend in ways that fight a separate FastAPI service. No need for SSR, RSC, or API routes when the backend already exists as a separate process. Plain Vite + React keeps the mental model clean: this is a SPA that talks to a separate API.

### Why this stack specifically

- React + TS + Tailwind is the most-represented stack in current LLM training data — produces the highest-quality AI-generated output by a clear margin.
- shadcn/ui works particularly well for LLM-assisted development because its components are source files copied into the repo, not opaque library imports — they can be read and modified directly.
- Tailwind's responsive prefixes (`sm:`, `md:`, `lg:`) make mobile-friendly design declarative.

### Dev workflow

- `uvicorn` runs the API on `:8000`
- `vite dev` runs the frontend on `:5173`
- Vite's dev server proxies `/api/*` to `:8000` — frontend code calls `/api/matches` directly, no CORS needed if the proxy is set up.
- FastAPI also has `CORSMiddleware` configured to allow `http://localhost:5173` as a fallback.

### Production hosting

Two viable paths; decision deferred:

1. **FastAPI serves the built bundle.** `pnpm build` produces `dist/`, deploy script copies it to a directory FastAPI mounts via `StaticFiles(html=True)`. One process, one port, simplest deploy.
2. **Cloudflare Pages.** Frontend deploys from git on push, Cloudflare routes `/api/*` to the VM. Better performance for global users, zero egress on static assets, free tier covers any realistic load.

Start with option 1 for v0; migrate to option 2 if/when deploy friction or load justifies it.

---

## Authentication

### Overall approach

- **Buy, don't build.** Clerk is the provider.
- **OAuth-only.** No email/password flows — eliminates password storage, reset flows, leak handling, and most bot-account creation incentives.
- **GitHub as the only provider at v0.** Audience match: every user writing a snake AI agent has a GitHub account. Adding Google later is an afternoon's work. Apple is skipped indefinitely (web-only, no App Store requirement). Meta is permanently skipped.

### Why Clerk

- 50K MAU free tier (as of Feb 2026) — $0/month at any realistic scale for this platform.
- Prebuilt React components (`<SignIn />`, `<UserButton />`, `<SignedIn />`, `<SignedOut />`) save days of UI implementation and produce a polished auth experience by default.
- GitHub OAuth integration is on the order of 30 lines.
- OAuth-only storage = low switching cost if we ever want to migrate to self-hosted (Authentik) or build-it-yourself (Authlib).

### Why not build it ourselves

The build path (FastAPI + Authlib + GitHub OAuth) is roughly 2–3 days of careful work. Clerk replaces that with a few hours of configuration and produces a better-looking auth screen. The tradeoff is one more vendor in the dependency graph, but Clerk's free-tier ceiling is well above what this platform will ever realistically hit.

### Token model

- **Bearer tokens** carried in `Authorization: Bearer ...` headers on API requests.
- Tokens are short-lived JWTs minted by Clerk, signed with Clerk's keys.
- FastAPI verifies tokens against Clerk's JWKS endpoint (cached in memory, rotated periodically per JWKS standard).
- No session cookies on the API itself. CSRF is therefore not a concern for any API endpoint.

### Token storage in the browser

Handled by the Clerk SDK:

- Session info lives in an `httpOnly` + `Secure` + `SameSite` cookie scoped to Clerk's domain.
- Short-lived access tokens are minted via `getToken()` in the React SDK and held in memory.
- Page refreshes don't lose the session because Clerk's cookie persists; access tokens are re-minted on demand.
- We never directly handle the refresh token — it's Clerk's responsibility.

This is the "hybrid" token-storage pattern we'd have built ourselves, but Clerk implements it for us.

### Backend JWT verification

FastAPI middleware verifies every incoming `Authorization: Bearer` token against Clerk's JWKS. On success, the request gets a `current_user` injected via a `Depends(get_current_user)` dependency. Unauthenticated requests are rejected for protected routes.

Library choice (TBD): likely `clerk-backend-api` (official) or hand-rolled with `python-jose` + JWKS caching.

### User model

Clerk owns identity (email, OAuth IDs, name, avatar). The platform's own `users` table exists for relational integrity — every project, submission, and match job has a `user_id` FK.

- `users.id` is an internal integer (existing).
- A new column, `users.clerk_user_id` (text, unique), links to the Clerk user.
- On first API call from a new Clerk user, the API checks for an existing row by `clerk_user_id` and inserts one if missing (just-in-time provisioning).

The webhook alternative (Clerk fires `user.created`, we insert) is more robust but adds an endpoint to maintain. Just-in-time is sufficient for v0 and can be replaced with webhooks if event-driven sync becomes necessary (e.g., for handling user deletion / GDPR).

---

## Abuse Prevention

The threat model is **"an authenticated user spams the match queue and burns the VM,"** not "anonymous DDoS." Defenses are layered, with the most important being queue-level quotas.

### Layer 1: Cloudflare at the edge

Already in place. Configure a coarse rate-limiting rule like 200 req/min per IP, blocking for 10 minutes on breach. Stops dumb bots before they reach the VM. Free, set once.

### Layer 2: Auth on every write endpoint

Anonymous reads are fine for public leaderboards and match views. Every write endpoint requires a valid Clerk JWT. Clerk's signup flow includes its own bot mitigation, making mass account creation impractical.

### Layer 3: Per-event quotas (most important)

The expensive operation is a match running in Docker, not an API call. Per-event quotas are per-user and split across two implementations depending on whether the underlying table records every event:

**Test matches — 120/hour** (sliding window, DB-counted).

`test_match_jobs` records `requested_by` and `requested_at` per row, so the count comes straight from SQL:

```sql
SELECT count(*),
       min(requested_at) + interval '1 hour' AS next_slot_at
FROM test_match_jobs
WHERE requested_by = $1
  AND requested_at > now() - interval '1 hour'
```

The `next_slot_at` returned to the client is when the **oldest in-window job** ages out — the user gets exactly one more slot back, not the full limit. With a sliding window there is no single "reset" boundary. UI: shows `used / limit`, turns yellow under 10 remaining, disables the submit button at 0 with a countdown to `next_slot_at`. Helper lives in `sa_common/db/quotas.py`.

**Ranked submissions — 5/hour + 20/day** and **image uploads — 10/day** (fixed window, Redis-counted).

`projects.submitted_at` only carries the most recent submission, and image uploads aren't recorded at all, so a DB-counted sliding window would need a new event table. We use Redis fixed-window counters instead (bucket key includes the window-start epoch, `INCR` + `EXPIRE` on each consume). The window boundary is exposed to the UI as a clean reset time.

The Redis quotas use a **peek + consume** pattern: the route checks `remaining > 0` before doing the operation and only INCRs after success, so a 409 ("test your project first") doesn't burn a slot.

Peek endpoints: `GET /test-matches/quota`, `GET /projects/submit-quota`, `GET /projects/upload-image-quota`. Each returns `{ limit, used, remaining, next_slot_at, window_seconds }`. The submit endpoint returns both hourly and daily windows wrapped in `{ hourly, daily }`.

### Layer 4: General per-user / per-IP API rate limit

Belt-and-suspenders behind Cloudflare, applied as ASGI middleware on every request. Auth'd requests are keyed on the Clerk `sub` claim (decoded from the bearer token locally, no DB hit); anonymous requests fall back to the client IP (`CF-Connecting-IP` → `X-Forwarded-For` → socket).

| Principal             | Limit                          |
| --------------------- | ------------------------------ |
| Authenticated, reads  | 60 req/min                     |
| Authenticated, writes | 20 req/min (POST/PUT/PATCH/DELETE) |
| Anonymous             | 30 req/min per IP              |

Counter storage: Redis fixed-window (`INCR` + `EXPIRE`, single round trip per request). `/health` is exempt so uptime monitors don't compete for slots. 429 responses include `Retry-After`.

The general limiter does **not** decorate every route — the middleware handles all paths uniformly, and per-event quotas (Layer 3) sit on top for the small set of expensive endpoints.

### Layer 5: Container resource limits

Already in place. CPU budget per agent (enforced by `CpuBudgetObserver` reading cgroups), memory caps, PID caps, network isolation. The bottom layer: even if abuse leaks through everything else, one bad actor can't take down the VM.

### Captcha policy

Don't put captchas on API endpoints. Clerk handles bot mitigation on its signup screen automatically.

---

## Resolved Decisions (from the original open list)

1. **Build flow** — No separate `build_jobs` table. The test-match daemon builds inline: it claims a `test_match_jobs` row, builds the dev image if needed, and runs the match in one flow. Build status is tracked on `projects.dev_build_status`.

2. **Clerk JWT verification** — Hand-rolled: `PyJWT` + JWKS caching via `httpx`. No `clerk-backend-api` dependency.

3. **Code editor** — Monaco (via `@monaco-editor/react`).

4. **User sync** — Just-in-time provisioning: first API call from a new Clerk user inserts a `users` row. No webhooks.

5. **Frontend production hosting** — TBD; start with FastAPI mounting the built `dist/` bundle.

## Still open

- **Tournament scheduler** — not built.
- **R2 wiring** — `IBundler` interface exists; R2 implementation and env wiring not done yet.
- **Replay host / bundle URL** — `REPLAY_HOST` env var wired in settings; production CDN path not finalised.

---

## Implementation Order

Suggested chunks to ship the platform's user-facing surface, smallest to largest:

1. **API scaffolding.** `services/api/` package, FastAPI app, `psycopg_pool`, Clerk JWT verification middleware, one health endpoint. Confirms the wiring works.
2. **Read-only endpoints.** List jobs, list matches, get match detail, stream replay. No new writes, no new schema.
3. **Match writes.** `POST /matches/jobs`. Replaces the enqueue CLI.
4. **Frontend scaffolding.** `services/frontend/` package, Vite + React + TS + Tailwind + shadcn/ui, Clerk SDK integration, `openapi-typescript` codegen, one page that lists matches.
5. **Match UI.** Match detail, replay player, agent logs.
6. **Build queue design + implementation.** Its own session, then `POST /submissions` + builder daemon.
7. **Project / version UIs.** Code editor, version history.
8. **Polish.** Leaderboard, tournament views, profile page.

Each chunk is roughly a session's worth of work and ships independently behind the existing infrastructure.
