# Legal documents

Drafts of the user-facing legal documents for gridsnake.

**These are developer-authored drafts, not lawyer-reviewed.** They are designed to be a reasonable starting point for a Swedish-operated solo hobby project and to satisfy the structural requirements of GDPR Art. 13 and standard ToS practice. Have a lawyer review before public launch if budget allows; at minimum, read them through end-to-end yourself and confirm every factual claim matches what the platform actually does on launch day.

## Placeholders to fill in before publishing

Search-and-replace across the three documents:

- `<CITY>` — your city of residence, used in the operator/controller identity block.
- `<DOMAIN>` — the public domain the service will run on (e.g. `gridsnake.example`). If still undecided, leave `<DOMAIN>` in until the domain is registered.
- `<EFFECTIVE_DATE>` — the date the documents go live (ISO 8601, e.g. `2026-06-15`).

## Files

- `terms_of_service.md` — the contract between gridsnake and its users.
- `privacy_policy.md` — what personal data is processed and why (GDPR Art. 13 notice).
- `acceptable_use_policy.md` — what users may not submit or do with the platform.

## Publishing

For launch, these should be reachable at stable URLs (e.g. `<DOMAIN>/terms`, `<DOMAIN>/privacy`, `<DOMAIN>/acceptable-use`) and linked from the site footer and the sign-up flow. The sign-up flow should require an explicit "I agree to the Terms and Privacy Policy" checkbox before account creation.
