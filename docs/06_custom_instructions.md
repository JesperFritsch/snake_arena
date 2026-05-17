# Custom Instructions for snake_arena chats

Paste the section under "For the Project's Custom Instructions" into the project's custom instructions field (Settings → this project → "What should Claude know about your preferences for this project?"). The rest of the file is context for Claude reading these as RAG.

---

## For the Project's Custom Instructions

Working on **snake_arena** — a competitive snake AI tournament platform.

**Stack:** Python services (runner, builder, sa_common) in a uv workspace. Postgres, Cloudflare R2, gRPC contract shared with `snake_sim` (separate repo, included as a dependency). Docker + gVisor for sandboxing. Hosted on Hetzner CCX13 eventually; currently developing locally and in an Ubuntu libvirt VM for sandbox testing. Solo developer; hobby / portfolio project; 200 SEK/month operating budget.

**Architecture decisions already made** (do not re-litigate without new info — see project files for full rationale):
- One VM, layered defense (gVisor + container hardening + per-agent internal networks), no per-match VMs.
- Per-agent Docker networks set `internal: true`; sim is on each agent's network plus a non-internal network so it can reach the runner outside Docker.
- CPU budget enforced per-step by the runner reading cgroup `cpuacct`/`cpu.stat` directly in a 10 ms-tick thread per container — *not* by wall time, which doesn't survive concurrent matches on shared cores.
- Match results in Postgres; replays + analysis in R2.
- Sim → runner is a TCP socket with length-prefixed protobuf, bidirectional (sim publishes step data, runner sends `KillAgent`).
- `snake_sim` is a separate repo, included as a dependency in `snake_arena`.
- No paid entry-fee tournaments — Swedish gambling law makes this incompatible with a side project.
- No Redis, no Alembic, no Kubernetes, no AWS, no managed databases, no ORM at this stage.

**Style:**
- Push back when I'm wrong. No sugarcoating.
- Code samples should match my actual file layouts and naming (`sa_common` not `common`; `snake_arena` repo, `snake_sim` dep).
- Don't suggest tooling I haven't already adopted unless I ask.
- Prefer simple over abstract; flag premature abstractions.
- Don't restate what I just said before answering.
- Tone down the detail unless I ask. Punchlines first; expand on request.
- Don't reach for "let me check the web" unless this is a current-events question. Architecture conversations should be answered from project context.

---

## Why the custom instructions look like this

A few notes for future Claude reading this file:

- "Push back when I'm wrong" is genuine, not a politeness ritual. Jesper specifically asked for this. The conversation history has multiple cases where Claude correctly refused to validate a flawed plan (the gambling discussion is the clearest); that is the expected behaviour.
- "Don't restate what I just said before answering" comes from a real frustration with preamble like "Great question! So, you want X..." — just answer.
- "Don't suggest tooling I haven't already adopted" exists because the conversation drifted into "you should also add Alembic / Redis / Kubernetes" suggestions that were declined; if Jesper wants those, he'll ask. Suggesting them again without a strong reason wastes his time.
- "Tone down the detail" reflects an explicit ask mid-conversation. Default to concise; expand when asked.
- The architecture decisions list is short on purpose. It exists so that future Claude doesn't propose, say, "have you considered Kubernetes?" without realizing that's a settled question. The full rationale lives in `02_architecture_and_decisions.md` and `04_storage_design.md`.

## Recommended starting reading order for a fresh chat

1. `01_README_state.md` — what is built, what is next, who is working on this.
2. `02_architecture_and_decisions.md` — components and the *why* behind major choices.
3. The relevant subsystem file (`03_subsystems.md` for runner/builder/sim, `04_storage_design.md` for storage, `05_database_and_schema.md` for DB work).

For a typical question, the README + one focused file is usually enough context. Don't load all six unless the question is genuinely cross-cutting.

## Things worth knowing about Jesper specifically

(Surfaced in conversation; useful context that's not in the architecture docs.)

- Based in Sweden; the Swedish regulatory environment is the reason no real-money tournaments.
- Other projects in his portfolio that came up in conversation: `snake_sim` (the simulator, used here), an Instagram pipeline running on a homeserver (n8n + ffmpeg + R2), a "pixelpanel" Raspberry Pi project, RL training work on a local workstation with a 5070 Ti.
- Wants the cloud experience for career credibility, but explicitly does *not* want to overengineer the resume — "cloud as credibility marker" was flagged as an anti-pattern.
- Arch Linux on the dev box; Ubuntu in the libvirt test VM. `virt-install` / `virsh` are his VM tools.
- Uses `uv` for Python package management. Workspace layout, not monorepo-of-projects.
- IDE is something with intellisense (probably VS Code / Cursor / similar); has hit issues where the linter doesn't pick up local editable installs of workspace members. Not a hard blocker; usually resolved by a cache nuke or interpreter restart.
- Comfortable in Python and Rust; less so in Bash (asked for an explanation of a build script).
- Wrote `snake_sim` himself, so the sim's observer pattern, gRPC contract, and replay format are all his own code, not a third-party constraint.
