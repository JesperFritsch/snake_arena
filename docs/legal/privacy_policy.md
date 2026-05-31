# Privacy Policy

**Effective date:** <EFFECTIVE_DATE>

This Privacy Policy describes how snake_arena (the "Service") processes personal data. The data controller for the Service is **Jesper Fritsch, <CITY>, Sweden** ("we", "us"). You can reach us at **jesperf96@gmail.com**.

The Service is intended for users aged 16 and over. We do not knowingly process personal data of anyone under 16.

## 1. What we collect and why

| Category | What | Why we need it | Legal basis (GDPR Art. 6) |
| --- | --- | --- | --- |
| **Account identifiers** | User ID, email address, display name, and any other profile information provided through Clerk. | To create and authenticate your account, communicate with you about the Service, and tie your Submissions and matches to you. | Contract (Art. 6(1)(b)) — performance of the Terms. |
| **Submitted code and project data** | Code you upload, project metadata, file names, build logs, container image tags. | To build, run, and replay your agents. | Contract (Art. 6(1)(b)). |
| **Match data** | Replays, per-step timing, analysis output, kill / budget-violation reasons, match outcome. | To run matches, show replays, and operate the leaderboard. | Contract (Art. 6(1)(b)). |
| **Technical logs** | IP address, request method and path, user agent, timestamps, error traces. Collected by Cloudflare (ingress) and our application logs on Hetzner. | To operate, debug, and secure the Service; to detect and respond to abuse. | Legitimate interest (Art. 6(1)(f)) in operating a secure service. |
| **Communications** | The content of messages you send us (e.g. support email, feedback form). | To respond to you and improve the Service. | Legitimate interest (Art. 6(1)(f)). |

We do not use cookies for tracking or analytics. The only cookies set are the strictly-necessary session cookies set by our identity provider (Clerk) to keep you signed in.

## 2. Where the data is processed

The Service runs on a single virtual machine hosted by **Hetzner Online GmbH** in the European Union. Object storage (match replay bundles and database backups) is held in **Cloudflare R2**. Authentication is handled by **Clerk, Inc.** Ingress and TLS are provided by **Cloudflare, Inc.**

Some of these providers (Clerk, Cloudflare) are established outside the EEA and may process data in jurisdictions including the United States. Where such transfers occur, they take place under the European Commission's Standard Contractual Clauses or other lawful transfer mechanisms.

## 3. Sub-processors

Current sub-processors:

- **Clerk, Inc.** — authentication and user identity (https://clerk.com).
- **Cloudflare, Inc.** — TLS termination, DDoS protection, request logging at the edge, and object storage via Cloudflare R2 (https://www.cloudflare.com).
- **Hetzner Online GmbH** — virtual-machine hosting for the application, database, and runtime (https://www.hetzner.com).

We will update this list when the set of sub-processors changes.

## 4. Retention

| Data | Retention |
| --- | --- |
| Account data | While your account exists. Deleted within 30 days after you close the account, except where we are required to retain it longer for legal reasons. |
| Submitted code (current versions) | While your account exists. Deleted with your account. |
| Match replays and statistics | Retained indefinitely as historical match records. After account deletion, replays in which you participated may continue to exist but will no longer be linked to your account identifiers. |
| Technical logs | Up to 30 days, except entries retained longer for security-incident investigation. |
| Database backups | Up to 30 days. |
| Support and feedback messages | Up to 24 months from last contact. |

## 5. Your rights under the GDPR

You have the right to:

- **Access** the personal data we hold about you.
- **Rectify** inaccurate or incomplete data.
- **Erase** your personal data ("right to be forgotten"), subject to limited legal exceptions.
- **Restrict** or **object to** certain processing.
- **Receive a copy** of the data you provided in a portable format.
- **Withdraw consent** at any time where processing is based on consent (this does not affect the lawfulness of prior processing).
- **Lodge a complaint** with a supervisory authority. In Sweden this is the *Integritetsskyddsmyndigheten* (IMY, https://www.imy.se).

To exercise any of these rights, email **jesperf96@gmail.com**. We will respond within 30 days. We may need to verify your identity before acting on a request.

Account and Submission deletion is currently handled by emailing us; there is no self-service delete button at launch.

## 6. Security

We take reasonable technical and organisational measures to protect personal data, including TLS in transit, container sandboxing (gVisor) for user-submitted code, per-agent isolated Docker networks, CPU and wall-clock budgets, principle-of-least-privilege access to production systems, and regular backups. No system is perfectly secure; we cannot guarantee absolute security.

If we become aware of a personal-data breach affecting you, we will notify you in accordance with Art. 34 GDPR and the relevant supervisory authority in accordance with Art. 33 GDPR.

## 7. Automated decision-making

We do not engage in automated decision-making that produces legal or similarly significant effects on you.

Note that match outcomes are produced by deterministic execution of submitted agents, which is not "automated decision-making" in the GDPR sense.

## 8. Children

The Service is not directed at children under 16. We do not knowingly collect personal data from children under 16. If you believe a child has provided personal data to us, contact us and we will delete it.

## 9. Changes to this Policy

We may update this Policy from time to time. Material changes will be announced on the Service at least 14 days before they take effect, unless an immediate change is required for legal or security reasons. The "Effective date" at the top of this Policy indicates when it was last updated.

## 10. Contact

Controller: Jesper Fritsch, <CITY>, Sweden.
Email: **jesperf96@gmail.com**.
