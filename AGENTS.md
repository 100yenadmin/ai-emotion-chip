# AGENTS.md

Guidance for AI coding agents, and for people, working in this repository.

## What this is

Hermes Emotion Chip (repo: AI Emotion Chip) is a Hermes Agent plugin that lets the agent write its own delivery cues (a small bracket score) into the text it speaks, and renders that score for Fish Audio and ElevenLabs. It renders the delivery the agent chose; it measures nothing about feelings. The emotion data in `plugin/emotion_chip/data/` is CC0 research data with attribution.

## Layout

- `plugin/`: the installable Hermes plugin (a catalog install takes only this directory).
- `plugin/plugin.yaml`, `plugin/__init__.py`: Hermes manifest and `register(ctx)`.
- `plugin/emotion_chip/`: score parser, renderers per vendor dialect, policy, provider, hooks.
- `plugin/emotion_chip/data/`: CC0 emotion concepts, rendering groups, sounds, dialect tables, model notes.
- `tests/`: unit tests and render goldens. `bench/`: paid benches (listening, ASR leak, acoustic probe). Importing
  `bench` puts `plugin/` on `sys.path`, so bench entry points import `emotion_chip` without `PYTHONPATH`.
- `EVAL-PREREG.md`: pre-registered evaluation bars; frozen by git tag.
- `evidence/`: published evaluation results.

## Setup and checks

<!-- Every command below was run in a clean checkout when this file was written. -->
```bash
uv venv && uv pip install -r requirements-dev.txt
uv run pytest -q
PYTHONPATH=plugin uv run python -m emotion_chip check-bundle   # after editing anything in plugin/emotion_chip/data/: build-bundle
```

## Rules

- Keep each change small and focused: one change per PR.
- Contributor PRs don't bump versions, create tags, or add CHANGELOG release headings. Maintainers cut releases in
  their own release PRs, which do all three.
- The agent is the only author of emotion cues: never add code that writes, upgrades or removes an emotion word the agent did not write. Renderers translate syntax only.
- Do not change `EVAL-PREREG.md` bars after a measurement starts; amendments go in its Amendments section with a date and reason.
- Data files are CC0; keep provenance fields filled.
- Never commit secrets, tokens, real user data, or machine-specific paths.
- Tests use a temporary `HERMES_HOME`, never a real one.

## Release closeout (required after every release, rc promoted to stable, or GA tag)

After the release is published and its receipt sent:
1. Close every issue the release fully served, with a comment naming the version (never an umbrella/parent issue whose own acceptance is not met).
2. Tick the boxes it completed on ROADMAP.md / VISION.md, roadmap or tracking issues, and closed issues whose checklists shipped; close a milestone once all its issues are closed.
3. Update every README / ROADMAP line that names the latest release or what ships next.
4. Post one closeout line as a comment on the release pull request: issues closed, boxes ticked, docs updated (or "none").

In this repository the roadmap is VISION.md ("Where we are" and "Where we're going"); there are no milestones.

## Filing issues

- Search first: `gh issue list -R 100yenadmin/ai-emotion-chip --state all --search "<key words>"`.
- Use the bug form headings: What happened, What you expected, Steps to reproduce, Plugin version, Hermes version, Where it happened, Operating system, Diagnostics, Relevant log lines.
- Maintainers filing from the CLI always pass labels: `--label bug,triage-pending` (or `triaged` once triaged), plus `agent-drafted` when an agent wrote it.

## Security

Report vulnerabilities privately: https://github.com/100yenadmin/ai-emotion-chip/security/advisories/new
