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

### Slow suites and release skips

The paid suites in `bench/` never run in CI. A release runs every suite whose inputs it changes, and its CHANGELOG
entry names each suite it skipped, with the reason. Loading the plugin through Hermes imports every module under
`plugin/emotion_chip/hermes/`, so those suites can skip by path only for the files in the last column. A version bump in
`plugin/plugin.yaml` and `plugin/emotion_chip/__init__.py` never requires a suite: nothing reads the version. Any
other skip names the code that changed and why the suite cannot run it: for example, the gateway-ingress part of
`hermes/voice.py`, because no suite builds a gateway. Paths are under `plugin/emotion_chip/` unless they start with
`plugin/`.

| Suite | Measures ([EVAL-PREREG.md](EVAL-PREREG.md)) | How it uses the plugin | Skip by path only when the release changes nothing but |
|---|---|---|---|
| `bench/gen.py`, `judge.py`, `score.py` | E2 and E3 text arms, generated with `claude -p` | imports `guide`, `data`, `render`, `score`, `model`, `policy`, `consistency` | `hermes/`, `cli.py`, `__main__.py`, `plugin/skills/`, `plugin/plugin.yaml`, `plugin/__init__.py`, `plugin/README.md`, `plugin/LICENSE`, `plugin/LICENSE-DATA` |
| `bench/hermes/text.py` | the scope A3 runs (mechanical gate, G1 report-only, E3) through Hermes; harness per A4 | the whole plugin, loaded by Hermes | `__main__.py`, `plugin/README.md`, `plugin/LICENSE`, `plugin/LICENSE-DATA` |
| `bench/e4/` | E4 situation-line pilot (parked), `claude -p` with tool hooks | imports `hermes/hooks.py` and what it imports, `hermes/situation.py`, and `bench/score.py` | `hermes/registration.py`, `hermes/commands.py`, `hermes/tools.py`, `hermes/middleware.py`, `cli.py`, `guide.py`, `__main__.py`, `data/`, `plugin/skills/`, `plugin/plugin.yaml`, `plugin/__init__.py`, `plugin/README.md`, `plugin/LICENSE`, `plugin/LICENSE-DATA` |
| `bench/e7/` | E7 blind listening | the whole plugin, loaded by Hermes, plus `hermes/eleven.py` and `render` directly | `__main__.py`, `plugin/README.md`, `plugin/LICENSE`, `plugin/LICENSE-DATA` |
| `bench/hermes/run.py` | spoken replies through Hermes (no EVAL-PREREG test): chip on and off on Fish, guidance on and off on ElevenLabs | the whole plugin, loaded by Hermes | `__main__.py`, `plugin/README.md`, `plugin/LICENSE`, `plugin/LICENSE-DATA` |

## Rules

- Keep each change small and focused: one change per PR.
- Contributor PRs don't bump versions, create tags, or add CHANGELOG release headings. Maintainers cut releases in
  their own release PRs, which do all three.
- The agent is the only author of emotion cues: never add code that writes, upgrades or removes an emotion word the agent did not write. Renderers translate syntax only.
- Do not change `EVAL-PREREG.md` bars after a measurement starts; amendments go in its Amendments section with a date and reason.
- Data files are CC0; keep provenance fields filled.
- Never commit secrets, tokens, real user data, or machine-specific paths.
- Tests use a temporary `HERMES_HOME`, never a real one.

## Release and escalation
- Lane by changed path, not by plugin (below, "the orchestrating owner" and "the human owner" are this project's owners). The careful lane covers changes to storage or migrations, deletion or compaction, locking, provider routing, streaming or retries, permissions, shared hooks, dependencies or update machinery, a host-wide install (a shared plugin root or processes, including one that can't be isolated), and anything with uncertain recovery. On top of the fast-lane proof floor below, it adds mechanism tests (old-state upgrade, restart or interruption, concurrency, recovery after new writes), a consistent backup, a recovery rehearsed on a copy that keeps post-upgrade data, and a canary on one already-authorized live install, running real operations with stated pass/fail thresholds. For an install that can't be isolated, the orchestrating owner decides the blast radius. Everything else takes the fast lane.
- Fast-lane proof floor for the smallest proven fix: an independent non-author review and green CI at the exact final SHA, targeted regression tests, and a smoke test of install, startup and the affected workflow against the current release. Reaching that proof should take hours, not days. Don't wait for GA, a release date, unrelated work or a benchmark campaign. Once it passes, release on the cadence below. A remaining P0 doesn't block the other fixes if it is contained (verified workaround, no new exposure, known-issue note, named fix owner, next-update date); otherwise the orchestrating owner decides the trade-off. In either lane, before shipping, name a watcher whose wake route has been seen to fire and a ready rollback or disable command, then watch for 72 h or one update cycle. On corruption, disclosure, an unauthorized action, a critical-path failure or a breached threshold: stop, contain, roll back or disable, tell the orchestrating owner and keep the evidence. A published release can't be recalled from auto-updating users; when that matters, ship a fast corrective release with clear user instructions.
- Tests follow the diff: if the plugin has slow or benchmark suites, keep a short map from each one to the code and behavior it measures. A release runs only the suites whose inputs changed and records each skip in the release note ("suite X skipped: nothing it measures changed"). The full suite runs on its own nightly or weekly schedule, off the release path. A regression it finds gets its own fix and never gates an unrelated hotfix.
- Severity and cadence (binding release rules for maintainers; not a support promise to users). P0 is data loss, disclosure or an unauthorized action (even for one user), or the default critical workflow broken with no safe workaround: contain a suspected P0 first and tell the orchestrating owner at once. P1 is a supported workflow blocked or materially degraded for some users, without P0 harm. An owner's priority call goes into your plan and changes the order of work, never the severity. P0 fixes ship immediately; ready P1 fixes go out in at most one bundled release a day; features ride a regular train (weekly by default, set per plugin by the orchestrating owner). Never hold a ready fix past the next daily bundle, and never cut a release that carries nothing a user needs.
- Anti-stall clocks, which never reset. A P0 with no safe path within 2 h of triage goes to the orchestrating owner. For a fix candidate with no decisive test, review or authorization result within 4 h, send the orchestrating owner the blocker, the remaining evidence, the fix owner and the next result time; a longer test needs the orchestrating owner's explicit OK, once, and a missed checkpoint re-escalates. A ready fix held 4 h needs a named blocker; send it to the orchestrating owner with a deadline for their decision. Running out of time never waives proof; it forces a decision.
- Who decides: maintainers make reversible technical choices within the permissions they already have, then report. The orchestrating owner decides cross-plugin priorities, user-impact trade-offs, unresolved high risks, blast radius and longer tests; an escalation carries the evidence, the options, a recommendation, a deadline and a safe fallback. The human owner decides new access or credentials, money, anything public in their name and destructive actions. This standard grants no new authority to publish, install or spend: live installs happen only in the guarded install window (backup first, then one restart), run by whoever the orchestrating owner has assigned to it, never on your own. Silence allows only a default that is already delegated and passes the gates above; it never grants new authority or accepts new risk: on silence, hold or contain, and escalate again.

Maintainer agents: the full internal standard is in eng-standards policies/release-and-escalation.md.

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
