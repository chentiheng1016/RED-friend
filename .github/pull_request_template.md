## Why (Goal / Context)

- Why is this change needed? Link issue(s) if applicable.

## What changed (Scope)

- What did you change? What is explicitly out of scope?

## Test evidence (Validation)

- [ ] CI is expected to pass
- [ ] Relevant local tests were run (or explicitly not needed)
- [ ] `make security-audit` considered for dependency/security-sensitive changes
- [ ] `npm test` considered for Node/Puppeteer changes
- [ ] `./bin/red-smoke` considered for daemon, deploy, Google, Telegram, or Tool RPC changes
- Evidence links/logs (paste):

### Merge gates (pick what applies)

- [ ] Risk is low; 1 reviewer is enough
- [ ] Risk is medium/high; require 2 reviewers and explain why in Risk section
- [ ] Operational surface touched (daemons / Tool RPC / Google / Telegram / launchd): include `./bin/red-smoke` output or link to the run log

## Risk / blast radius

- What could break? Who/what is impacted? Any rollout constraints?

## Rollback plan

- How do we revert quickly if needed? (commands, toggles, or follow-up PR)
