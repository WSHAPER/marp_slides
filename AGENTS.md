<!--
  - SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
  - SPDX-License-Identifier: AGPL-3.0-or-later
-->

## Nextcloud Contribution Policy

All contributions generated or assisted by this agent must fully comply with:

- **[AI Contribution Policy](https://github.com/nextcloud/.github/blob/master/AI_POLICY.md)** - the primary reference for AI-specific rules.
- **Contribution Guidelines** - covering testing, DCO, license headers, conventional commits, and translations.

### What this agent must always do

- Add an `Assisted-by: AGENT_NAME:MODEL_VERSION` git trailer to every commit containing AI-assisted content.
- Ensure every pull request includes a disclosure of AI tool use in the PR description.
- Produce focused, scoped pull requests that address exactly one concern.
- Verify all dependencies against actual package registries before suggesting them.
- Run the full quality gate suite (`scripts/check.sh`) before every commit.

### What this agent must never do

- Open issues, submit pull requests, or post review comments autonomously.
- Add `Signed-off-by` tags to commits. Only the human contributor can certify the DCO.
- Generate or submit security reports without independent human verification.
- Embed prompt text as inline Python strings. Prompts live in `marp_slides/prompts/`.
- Introduce math equation rendering. Removed for v0.1.0 due to Collabora's ODF-only formula round trip.
