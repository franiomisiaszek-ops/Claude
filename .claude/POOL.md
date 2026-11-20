# Claude Code component pool

Helpers for *building* papertrader with Claude Code. None of this runs inside the
trading system, and none of it gets the Alpaca keys.

Source: Claude Code Templates (aitmpl.com), repository github.com/davila7/claude-code-templates,
commit `816626c` (CLI 1.33.1), copied on 2026-11-19 after reading each file. Every
Claude Code session in this repository (cloud or your PC) loads them automatically.

## Agents (`.claude/agents/`)

| Area | Agents |
|---|---|
| Security | security-auditor, api-security-audit, penetration-tester (only against our own app), llm-redteam-specialist, read-only-auditor |
| Trading review | quant-analyst, trading-risk-manager |
| Claude prompts | prompt-engineer, model-evaluator |
| iPhone web app | frontend-developer, mobile-developer, ui-ux-designer, web-vitals-optimizer |
| Engineering | python-pro, postgres-pro, test-automator |

## Skills (`.claude/skills/`)

statistical-analysis, shap, llm-evaluation, fastapi-pro, frontend-design, accessibility,
telegram-bot-builder, python-testing-patterns, docker-expert

## Protection (`.claude/settings.json`)

- **Secret scanner** (`hooks/secret-scanner.py`): blocks a `git commit` that contains an
  API key, token, password or private key.
- **Deny rules**: Claude Code may not read or edit `.env` files or a `secrets/` folder.

## Changes from the catalog versions

- quant-analyst and trading-risk-manager can only read (Read, Grep, Glob), so a reviewer
  can never change trading code by itself.
- The secret scanner resolves paths from the repository root (the original silently
  scanned nothing from a subfolder), covers `git add <folder>`, ignores UUID-shaped values
  and placeholders (`XXXX`, `****`, `${VAR}`), skips lines marked `secret-scanner: allow`,
  and prints only the first 4 characters of anything it finds.
- The catalog's `.env` write-blocking hook was replaced by permission deny rules: its
  pattern also blocked `.env.example`.

## Adding more

Send the name from aitmpl.com (e.g. `development-team/backend-architect`); it is read,
copied here at a pinned version and committed. To remove one, delete its file or folder.
