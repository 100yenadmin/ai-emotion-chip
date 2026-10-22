# AGENTS.md

Guidance for AI coding agents, and for people, working in this repository.

## What this is

Hermes Emotion Chip (repo: AI Emotion Chip) is a Hermes Agent plugin that lets the agent write its own delivery cues (a small bracket score) into the text it speaks, and renders that score for Fish Audio and ElevenLabs. It renders the delivery the agent chose; it measures nothing about feelings. The emotion data in `emotion_chip/data/` is CC0 research data with attribution.

## Layout

- `plugin.yaml`, `__init__.py`: Hermes manifest and `register(ctx)`.
- `emotion_chip/`: score parser, renderers per vendor dialect, policy, provider, hooks.
- `emotion_chip/data/`: CC0 emotion concepts, rendering groups, sounds, dialect tables, model notes.
- `tests/`: unit tests and render goldens. `bench/`: paid benches (listening, ASR leak, acoustic probe).
- `EVAL-PREREG.md`: pre-registered evaluation bars; frozen by git tag.
- `evidence/`: published evaluation results.

## Setup and checks

<!-- Every command below was run in a clean checkout when this file was written. -->
```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest -q
```

## Rules

- Keep each change small and focused: one change per PR.
- Don't bump versions, create tags, or add CHANGELOG release headings. Maintainers cut releases.
- The agent is the only author of emotion cues: never add code that writes, upgrades or removes an emotion word the agent did not write. Renderers translate syntax only.
- Do not change `EVAL-PREREG.md` bars after a measurement starts; amendments go in its Amendments section with a date and reason.
- Data files are CC0; keep provenance fields filled.
- Never commit secrets, tokens, real user data, or machine-specific paths.
- Tests use a temporary `HERMES_HOME`, never a real one.

## Filing issues

- Search first: `gh issue list -R 100yenadmin/ai-emotion-chip --state all --search "<key words>"`.
- Use the bug form headings: What happened, What you expected, Steps to reproduce, Plugin version, Hermes version, Where it happened, Operating system, Diagnostics, Relevant log lines.
- Maintainers filing from the CLI always pass labels: `--label bug,triage-pending` (or `triaged` once triaged), plus `agent-drafted` when an agent wrote it.

## Security

Report vulnerabilities privately: https://github.com/100yenadmin/ai-emotion-chip/security/advisories/new
