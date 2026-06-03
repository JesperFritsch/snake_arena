# Pre-Launch Checklist

A practical checklist of what needs to happen before snake_arena goes public. Grouped by category, ordered roughly by what blocks launch vs. what can ship after.

The known-unbuilt items from `01_README_state.md` are reclassified here as either launch-blocker or post-launch.

## 1. Legal / GDPR

Solo dev in Sweden hosting user accounts + executing user-submitted code → GDPR applies, and the executable-code aspect makes ToS/AUP load-bearing for abuse response.

Minimum set before launch:

- **Privacy Policy** — what is collected (Clerk identifiers, email, submitted code, IP via Cloudflare logs), purpose, retention, list of sub-processors (Clerk, Cloudflare, Hetzner, R2). Required by GDPR Art. 13.
- **Terms of Service** — covers: no malware / illegal content in submissions, operator may inspect and remove submissions, no warranty, liability cap, governing law (Swedish), account termination conditions.
- **Acceptable Use Policy** — explicit prohibitions: no attempts to escape the sandbox, no use of the platform as C2 / scraping infra, no copyrighted code without rights. Gives clear grounds for banning.
- **Sub-processor list** — kept current inside the Privacy Policy. Update when a new third-party service is added.
- **Right to erasure** — must be *possible* to fully delete a user (account + projects + bundles). Self-service is not required at launch; a documented manual procedure is enough.
- **Contact address** — a reachable email for GDPR requests. Alias is fine.
- **Age minimum** — Clerk does not gate by age. State a minimum in ToS (13+ at the very least; 16+ is the safer EU interpretation).

Not needed at this size: DPO, cookie consent banner (only Clerk auth cookies = strictly necessary). Add a banner if analytics is introduced later.

## 2. Follow-through commitments the legal docs create

Publishing the documents in `docs/legal/` commits us to actually doing the things they describe. Each item below is a concrete operational task that exists *because* the docs say so. Most are launch-blockers — if any of them is false on launch day, the corresponding policy is misleading and should be edited instead of left in place.

**Account lifecycle**

- **Manual account-deletion procedure** — documented internal runbook: delete the `users` row, owned `projects`, submitted code archives, dev/submitted Docker images, and any in-flight `*_jobs`. Promised 30-day SLA.
- **Match-participant anonymisation** — schema/code change so deleting a user replaces their `user_id` in `match_participants` with NULL or a sentinel (and clears any display name embedded in match metadata) without dropping the participant row. Required by the privacy policy's "replays continue to exist but will no longer be linked to your account identifiers" clause.
- **Project / submission deletion procedure** — same idea, scoped to a single project rather than the whole account. Useful for AUP enforcement.
- **GDPR-request inbox SLA** — process for responding to access / rectification / erasure / portability requests within 30 days. Include a light identity-verification step (e.g. respond from the registered account email) before acting.

**Retention enforcement**

- **Application + Cloudflare log retention ≤ 30 days** — `logrotate` config on the VM; Cloudflare log retention setting confirmed.
- **Database backup retention ≤ 30 days** — R2 lifecycle rule on the backup prefix.
- **Submitted-code / bundle retention tied to account lifecycle** — covered by the account-deletion runbook above; verify there is no orphan path that keeps bundles after deletion.
- **Support / feedback message retention ≤ 24 months** — at minimum a periodic manual cleanup reminder.

**Sub-processor governance**

- **Accept / sign the DPAs** — Clerk DPA, Cloudflare DPA (accept in dashboard), Hetzner AVV. The privacy policy claims transfers happen under Standard Contractual Clauses; this is only true if these are actually in place.
- **Sub-processor change procedure** — when adding a new third-party service, update the privacy policy first and give the 14-day notice. Refusing to add a sub-processor silently is the easy default; this just makes the process explicit.

**Policy-change announcement mechanism**

- **14-day change notice surface** — needs *some* way to actually notify users of material ToS / Privacy / AUP changes. Cheapest workable version: a dismissable banner component driven by a `policy_version` value, plus an email to all accounts on material changes. Without this the "we will announce changes 14 days in advance" promise is empty.
- **`Effective date` discipline** — bump the date in each document whenever it materially changes. Keep an internal changelog (e.g. `docs/legal/CHANGELOG.md`) of what changed and when.

**Security claims that must be true on launch day**

- **gVisor enabled on prod** — the privacy policy names it explicitly. Validated in the Ubuntu test VM today; must be the actual runtime on Hetzner.
- **TLS enforced end-to-end** — Cloudflare "Always Use HTTPS" + HSTS header from the application. The "TLS in transit" line stops being true if any path serves HTTP.
- **No tracking / analytics cookies actually load** — audit the built frontend bundle and live page at launch for third-party scripts; confirm no Cloudflare features that set cookies (Rocket Loader, Bot Fight challenges that drop `__cf_bm`, etc.) are enabled in a tracking sense. If any non-strictly-necessary cookie ends up set, either disable it or add a cookie banner and revise the privacy policy.
- **Incident-response one-pager** — short written procedure for personal-data breaches: detect → contain → assess scope → notify users (Art. 34) and IMY (Art. 33) within 72 hours. Required to honour the breach-notification commitment in the privacy policy.

**Abuse-handling mechanisms (from the AUP)**

- **`suspended` flag (or equivalent) on `users` and `projects`** — so submissions can be refused-to-build / refused-to-run without deleting data. Without this, "we may refuse to build or run any Submission" is only achievable by destructive SQL.
- **Cloudflare IP-block runbook** — short doc on how to add a WAF rule, since the AUP reserves the right to block IP addresses.
- **Law-enforcement / preservation request procedure** — even a one-paragraph "what I do if I get a Swedish police request" note. Unlikely at this scale, but the AUP promises we can do it.
- **`/.well-known/security.txt`** — supports the good-faith vulnerability reporting clause in the AUP. Contains the contact email and a link to the AUP's safe-harbor language.
- **Operator inbox monitoring discipline** — `jesperf96@gmail.com` is now load-bearing for GDPR requests, abuse reports, security disclosures, and account compromise reports. Set up Gmail filters / labels so these don't get lost; commit to checking at least daily.

## 3. Operational hardening (launch-blockers from `01_README_state.md`)

These were already listed as unbuilt and are the ones that will hurt in production:

- **R2 wiring** — `LocalBundler` only survives until disk pressure. If R2 is delayed, at minimum: disk-usage monitoring + a manual prune procedure.
- ~~**Rate limiting** (`slowapi`) — Cloudflare alone will not stop a logged-in user from queuing thousands of test matches.~~ Implemented: per-user queue quotas (test matches 120/h, submissions 5/h + 20/d, image uploads 10/d) plus a general Redis-backed per-user/per-IP request limiter. See `07_api_frontend_auth.md` §Abuse Prevention.
- **Stale `running` job reaper** for the ranked match daemon — the test-match daemon already does this at startup; ranked must too, or one orchestrator crash leaves jobs wedged indefinitely.
- **Image GC cron** — without pruning, Docker storage fills within weeks.

## 4. Operational gaps not yet captured elsewhere

- **Backup restore test** — a `pg_dump` that has never been restored is not a backup. Restore once to a scratch DB and confirm.
- **Error monitoring** — Sentry free tier on API + frontend. Without it, breakage in production is invisible.
- **Uptime / health checks** — UptimeRobot free tier is enough for a single VM.
- **Sandbox-escape alarm** — if the hostile suite ever stops being contained on prod, the operator must find out fast. Log + alert on unexpected egress and on non-zero gVisor exits.
- **Abuse response plan** — written procedure for suspending a user when a submission is reported (even a single manual SQL statement is fine, but document it).
- **Cost ceiling alarms** — Hetzner billing alert + R2 spend alarm so the 200 SEK/month cap cannot be silently exceeded.

## 5. Feedback channel

Have one from day one — without it there is no signal on what to build next.

Recommended combo:
- **In-app feedback form** — link in the nav, posts to a `feedback` table (or a mailto for the simplest version). Captures users who will not leave the app.
- **GitHub Discussions** — free, doubles as a public roadmap / changelog.

Skip Discord unless a community actually forms.

## 6. Post-launch (not blocking)

Items from `01_README_state.md` that can ship after, with the leaderboard marked "coming soon" in the UI:

- Tournament scheduler + ELO / ranking computation.
- Leaderboard / rankings page in the frontend.
- Per-agent timing summary in `match_participants.metrics`.
- Second language base image (Rust or Go).
- Submission history view.
- `final_length` backfill in `match_participants`.

## Suggested launch order

1. Legal documents (ToS + Privacy + AUP) — drafts exist in `docs/legal/`.
2. Follow-through commitments from §2 that are launch-blockers: account-deletion runbook, match-participant anonymisation, log + backup retention, sub-processor DPAs, `suspended` flags, gVisor on prod, TLS enforcement, third-party-cookie audit, incident-response one-pager, `/.well-known/security.txt`.
3. Operational hardening from §3: rate limiting + stale-job reaper + R2 + backup restore test.
4. Operational gaps from §4: Sentry + UptimeRobot + cost alarms.
5. Feedback form + GitHub Discussions repo.
6. Policy-change announcement mechanism (banner + email path) — needed before the first material policy update, not necessarily day-one.
