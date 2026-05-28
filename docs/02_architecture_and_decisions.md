# Architecture and Key Decisions (with rationale)

This file captures the *why* behind the design. Future chats should consult it before re-litigating choices that have already been made.

## Big-picture component split

Four concerns, deliberately separate even though some are merged into one process today:

1. **Build service** — turns user code into a runnable image. Input: code blob + language. Output: tagged image in the local Docker daemon.
2. **Match runner** — executes a single match. Input: N image tags + match config. Output: replay file + `MatchResult`.
3. **Orchestrator** — decides *when* to run matches and *which*. Polls `match_jobs` (ranked) and `test_match_jobs` (dev test). Tournament scheduler not yet built.
4. **State store** — Postgres. Tracks users, projects, matches, results.

Both test mode and tournament mode share #1 (build) and #2 (run a match). They differ only in who triggers them and what data is loaded.

## The big-picture flow

**Test mode (user iterating):**
```
user saves code → build service → image → match runner (vs built-in bots) → replay → user sees result
```

**Tournament mode:**
```
scheduler triggers tournament → fetch frozen submissions per user
  → match runner runs all pairings → results aggregated → leaderboard
```

## Versioning model

```
User ──< Project (dev state + submitted state on one row)
```

- A user owns one or more projects (one project = one snake agent for v1).
- A `projects` row carries **two parallel states**: a mutable `dev_*` side (editor draft + latest test build) and a frozen `submitted_*` side (pinned code archive, pinned image tag, version counter).
- **Save** overwrites `dev_code_archive` and resets build status.
- **Test** builds the dev image, runs a match. On success, status reaches `ready`.
- **Submit** (requires `ready`) promotes dev → submitted: copies the code archive and image tag, bumps `submitted_version`. Later saves/builds don't touch the submitted side.
- No separate `code_versions` or `submissions` tables. The version counter on the row is enough to reconstruct "version N of agent X played this match" from `match_participants.project_version`.

## Decisions made (do not re-debate without new info)

### Hosting: Hetzner CCX13, single VM

- 2 dedicated AMD cores, 8 GB RAM, 80 GB NVMe SSD, **20 TB egress included** for ~155 SEK/month.
- Closest Hetzner DC to Sweden: Helsinki.
- **Why not AWS/Azure:** "free tier for 12 months then back to $15-30/month" is fine for resume value, but the surprise-bill risk on egress is real. The 20 TB included egress on Hetzner is the single biggest cost protector.
- **Why one VM:** comfortably fits Postgres + API + runner + 1-2 concurrent matches + warm sandbox pool at idle ~2-3 GB, peak ~4-5 GB on the 8 GB box.
- **Skip at this budget:** managed databases, managed K8s/ECS, separate dev/staging/prod, multi-region, autoscaling, distributed tracing.
- **Keep at this budget:** containers + gVisor for sandboxing, Cloudflare in front, daily DB backups to R2, a Postgres-backed job queue (even if just a table), hard resource limits on every container.

### Object storage: Cloudflare R2

- **Zero egress fees** — decisive for serving replays to browsers.
- API-compatible with S3 (`boto3` and friends work unchanged).
- Generous free tier (10 GB storage, ~10M class-A ops/month).
- Replays + analysis + DB backups all land in one bucket with prefixed paths.
- Decoupled from VM lifecycle — VM rebuild does not lose replays.

### Database: Postgres (not SQLite)

- SQLite was considered for zero ops weight, but it locks the whole DB on writes, and even at v1 the runner + builder + (eventually) API will write concurrently.
- Postgres in a Docker container is one line of compose and ~300-500 MB RAM at this scale.
- Standard tooling (`pg_dump`, monitoring) and the migration story scales without redesign.

### Sandboxing: gVisor + container hardening + per-agent networks

- All user code runs in containers with `runtime=runsc` (gVisor), `--cpus=1.0`, non-root, no caps, read-only FS where possible.
- **One isolated Docker network per agent.** The sim is on each agent's network (so it can call them via gRPC). Agents can't reach each other.
- The sim also lives on a non-internal network so it can reach the runner outside Docker (for the socket observer callback). The agents themselves are on `internal: true` networks — no internet, no host.
- **Layered defense, not perimeter defense.** Even if gVisor were bypassed, the attacker lands in a tiny VM with no network and no host filesystem.
- Trust model is uniform: everyone (anonymous user, trusted friend, future me) gets full sandbox always. No "trusted user" tier.

### Sandboxing topology — why two networks per match

The sim talks to agents via gRPC on a private network. The sim *also* needs to call back to the runner (over the socket observer protocol). The runner runs outside Docker, on the host.

Implementation:
- For each agent: a Docker network with `internal: true` (no NAT, no egress). The sim and that one agent are both attached.
- For the sim's runner callback: the sim is *also* attached to a non-internal bridge network so it can reach the host. From inside the sim, the host is reachable as `host.docker.internal` (Linux: requires `--add-host=host.docker.internal:host-gateway`) or, more portably, by the bridge gateway IP.
- The sim is trusted code; it is OK that it has outbound networking. Agents have none.

Why not put the runner *also* in a container: avoidable complexity at v1. The runner manages Docker; running Docker-in-Docker or socket-mounting the host daemon is more attack surface than it's worth before we have an API.

### CPU fairness: per-step CPU-time budget, enforced by the runner

The naive approach — kill an agent when its wall-time per step exceeds a threshold — does not survive multiple concurrent matches on a 2-core VM. Wall time becomes "real time / contention factor"; honest agents get killed under load.

Decision:
- Sim no longer enforces a wall-time killswitch (kept very loose, ~60s, only as a hang backstop).
- Sim accepts external **kill commands** for a given `agent_id`. After receiving one, it stops sending `Update` to that snake and treats it as dead from that step on.
- Runner tracks each agent's CPU time by reading the container's cgroup `cpu.stat` directly, on a 10 ms tick in a per-container thread.
- Each step grants the agent an additional fixed CPU budget (the "cumulative budget" pattern). Unused budget banks for later steps — so an agent can do an occasional deep search without being killed. The runner kills when cumulative usage exceeds cumulative grant.
- The runner has its own match-level wall-clock timeout as a last-resort backstop.

This is **multi-match-fair** on shared cores: an agent's CPU accounting is unaffected by what other matches on the host are doing.

### Sim ↔ runner protocol: bidirectional socket with length-prefixed protobuf

- Sim opens a TCP connection to the runner at the start of the match. The runner is a TCP server.
- Framing: 5-byte header (`>BI`, 1-byte message type + 4-byte length), then a protobuf payload. Length-prefixed; no delimiter problems.
- One connection per match. Sim sends step data outbound; runner sends commands inbound on the same socket.
- One observer instance handles one receiver — simpler than multi-receiver. If multiple consumers are needed later, run a relay process.
- Conceptually, the socket bridge is *both* `ILoopObserver` (read-only notification of step data) *and* `ILoopController` (issues commands to the loop). The two interfaces stay independent; one class happens to implement both. **Do not** call it `IBiDirLoopObserver` — observers observe, controllers command; conflating them is semantic mud.

### Why not ZMQ or HTTP for the sim ↔ runner channel

- ZMQ was considered. Overkill for one connection. Plain TCP + length-prefixed protobuf is simpler and has zero extra deps.
- HTTP would require running a server inside the sim. More moving parts; framing is no easier.

### Submission flow: Docker image, not source-on-disk

- Users submit code, builder produces an image whose entrypoint runs a gRPC server on a known port.
- The builder image per language has the harness + generated stubs baked in. The user's code is `COPY`'d into the final per-submission image at build time.
- Users do **not** have to make their code public. The builder ingests submitted code (web upload eventually; CLI for now) and the resulting image is private to the platform.
- Language-agnostic by design: any language with gRPC support can have a base image. Python is built; Rust or Go is the natural next one.

### Money / entry fees: **NOT in v1**

The conversation explored entry-fee tournaments with a platform cut. This was rejected on legal grounds:

- Under the Swedish Gambling Act (2018), stake + prize + any element of chance = regulated gambling under Spelinspektionen. The framework is conservative; no clean "skill game" carve-out the way some US states have. Random map generation, matchmaking variance, anything non-deterministic will be argued as "chance."
- A Swedish online gambling license costs €23,760/year + 18% gaming tax on GGR + AML/KYC integration + executive background checks + server location requirements.
- "I'll just put it on a `.com` and use English UI" does not help — Spelinspektionen's test for "targeting Sweden" includes Swedish currency, Swedish-language UI, and Swedish marketing, but **the operator being Swedish is itself a strong signal**. Other countries (esp. US, state-by-state) have their own regimes; cross-border makes it worse, not better.
- Stripe / PayPal / mainstream payment processors prohibit real-money skill gaming without proper licensing — accounts terminated within weeks of the first transaction.

Viable monetization later if desired: free tournaments + donations / Patreon / sponsorships / ads / sponsored prize pools (operator funds prize, entry is free — promotional contest rules, much lighter regime) / virtual currency that does not cash out. **Build the free version first.**

### Cloud-as-credibility caveat

Stated explicitly in the source conversation: the goal is good engineering and an honest "I migrated and operated a real system" story, not "I overengineered a Kubernetes cluster on AWS for 12 users." A single well-run VM with sound architecture interviews better than a sprawling managed-service stack at hobby scale.

## Open architectural questions

These have been raised but not closed:

- **Retention policy for replays.** Keep all forever? GC after N months? Tier to cold storage? Wait for usage data.
- **Code version retention.** Keep every save, or only "submitted" versions? Probably every save; revisit if storage gets weird.
- **Tournament data shape.** Schema sketch exists; refine when tournaments are actually built.
- **Image GC policy.** Default plan is "last N per project" with N=5 or 10. Revisit when VM disk gets tight.
- **Concurrent matches per host.** Start sequential; introduce concurrency when measurement justifies it. The CPU budget design supports concurrency once we turn it on.
- **Scoring formula** (ELO, win rate, sum of placements, composite). Persist the underlying data; defer the formula.
