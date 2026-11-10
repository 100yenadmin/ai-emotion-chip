# Hermes Emotion Chip

![Hermes Emotion Chip](docs/media/banner.png)

**Your Hermes agent decides how to say it: the sighs, whispers and pauses it writes, voiced by ElevenLabs or Fish Audio.**

https://github.com/user-attachments/assets/43eacd65-ce66-4abe-9eff-d7f32146a16d

[![Latest release](https://img.shields.io/github/v/release/100yenadmin/ai-emotion-chip)](https://github.com/100yenadmin/ai-emotion-chip/releases)
[![License: Apache-2.0, data CC0](https://img.shields.io/badge/license-Apache--2.0%20%2B%20CC0%20data-blue)](#license)
[![Hermes 0.22.2 or newer](https://img.shields.io/badge/Hermes-%E2%89%A5%200.22.2-6f42c1)](https://github.com/NousResearch/hermes-agent)
[![CI](https://github.com/100yenadmin/ai-emotion-chip/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/100yenadmin/ai-emotion-chip/actions/workflows/ci.yml)

## Install

```bash
hermes plugins install 100yenadmin/ai-emotion-chip/plugin --enable
```

It needs Hermes 0.22.2 or newer and a voice: the [Fish Audio plugin](https://github.com/100yenadmin/fish-audio-hermes)
signed in (the default), or an ElevenLabs API key. Next, [set up a voice](#set-up-a-voice).

From the Hermes plugin catalog, `hermes plugins install emotion-chip --enable` works once your Hermes's catalog lists
the plugin. On Hermes 0.23.0 it fails with "not in the Hermes plugin catalog", because Hermes reads the catalog copy it
shipped with, which predates this plugin
([hermes-agent#151856](https://github.com/NousResearch/hermes-agent/issues/151856)). Use the line above.

## Try it

1. Install it (above).
2. See what an engine receives, with no keys and no network:

   ```bash
   hermes chip render --dialect eleven_v4 "[whisper] Ten thousand users! [pause] Cheer when the baby's [stress: awake]."
   ```

3. You get the text ElevenLabs v4 would be sent: `[whisper] Ten thousand users! [pause] Cheer when the baby's AWAKE.`
   With a voice set up, ask your agent to tell you, quietly, that the tests passed because the baby is asleep: it
   writes its own score and you hear it whisper. `/chip why` shows the cues it wrote and what the engine got.

![Hermes CLI: the agent whispers that the tests passed because a baby is asleep, /chip why shows the render, and the next turn is whispered excitement](docs/media/screenshot-cli.png)

## How it works

Hermes Emotion Chip is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin. The agent writes its
own delivery cues, a small bracket score such as `[sigh]`, `[whisper: it passed.]`, `[pause]` or `[warm, amused]`,
into the text it speaks. The plugin renders that score for **ElevenLabs v4** and **Fish Audio** (s2.1-pro and
Drama 3), each in its own dialect and at that vendor's documented ceiling. It gives the agent facts it cannot see,
never adds an emotion the agent did not write, and treats plain speech as a valid choice. It renders the delivery the
agent chose; it measures nothing about feelings.

```text
[sigh] An entire day lost to one character. [pause] Glad you found it.
```

## Set up a voice

**Fish Audio (the default).** Install and sign in to the Fish Audio plugin, then make the chip the speech provider:

```bash
hermes plugins install fish-audio --enable && hermes fish login && hermes fish doctor
hermes config set tts.provider emotion-chip
hermes config set tts.fish-audio.voice <a Fish voice id>
```

**ElevenLabs.** Put `ELEVENLABS_API_KEY=...` in the profile's `.env` (or Desktop ▸ Settings ▸ Keys), pick a voice in
the [voice library](https://elevenlabs.io/app/voice-library), then:

```bash
hermes config set tts.provider emotion-chip
hermes config set plugins.entries.emotion-chip.settings.backend eleven
hermes config set plugins.entries.emotion-chip.settings.eleven_voice_id <voice id>
```

Restart the gateway (`hermes gateway restart`) and run `/chip status`: it says whether the chip can speak with your
settings, and what to fix if not.

| Setting (under `plugins.entries.emotion-chip.settings`) | Default | What it does |
|---|---|---|
| `backend` | `fish` | `fish` or `eleven`. |
| `eleven_voice_id` | empty | The ElevenLabs voice; required with `backend: eleven`. |
| `guide_mode` | `generate` | `generate` gives the agent the delivery guide, skill and tools; `defer` leaves guidance to you. |
| `delivery_log` | `true` | A local log of cue words and parameters, never the spoken text. |

Every setting, the `/chip` commands and the agent tools are in **[plugin/README.md](plugin/README.md)**.

## Where the agent's delivery is heard

- Every `text_to_speech` call the agent makes carries its cues to the voice.
- With voice replies on in a chat (`/voice on`), the agent says its answer itself, cues included, so the voice note
  carries its delivery. Not on Discord, where Hermes plays its own reply in voice channels.
- Read-aloud and voice mode speak the reply text without cues: Hermes has no seam yet that keeps cues for speech and
  hides them in the chat ([proposed upstream](https://github.com/NousResearch/hermes-agent/issues/152238)).
- GPT-Live voice chat speaks through OpenAI's live voice model, not a speech provider, so the chip does not reach it.

Details: [plugin/README.md, Coverage](plugin/README.md#coverage).

## Privacy and cost

- With ElevenLabs, the chip sends your key, the rendered text, the model and voice ids, voice settings, an optional
  seed and, on v4 models, up to three earlier request ids from the session to `api.elevenlabs.io`: paid requests on
  your account. With Fish Audio it sends nothing itself; the Fish Audio plugin sends the rendered text under its own
  disclosure, billed to your Fish account.
- On disk, under the profile's `plugin-data/`: a delivery log of cue words, dialect and parameters (never the spoken
  text, at most about 10 MB) and per-session state. No telemetry, no background processes.

The full disclosure is in [plugin/README.md](plugin/README.md#disclosure).

## Troubleshooting

- **`/chip status` says `ready: no`.** It names the fix: the speech provider, the key, the voice or the Fish plugin.
- **No sound on the Fish backend.** Run `hermes fish doctor`.
- **`voice_replies: off until Hermes restarts`.** The chip read your earlier settings when Hermes started; run
  `hermes gateway restart`.
- **The catalog name is not found.** Use the repository line in [Install](#install).

## Report a bug

Open an issue with the [bug form](https://github.com/100yenadmin/ai-emotion-chip/issues/new?template=bug_report.yml).
Report security problems privately through a
[security advisory](https://github.com/100yenadmin/ai-emotion-chip/security/advisories/new), not a public issue
([SECURITY.md](SECURITY.md)). Problems in Hermes itself belong
[upstream](https://github.com/NousResearch/hermes-agent/issues).

## What is in this repository

| Path | What it is |
|---|---|
| [`plugin/`](plugin/) | The installable plugin; a catalog install takes only this folder. |
| [`plugin/emotion_chip/data/`](plugin/emotion_chip/data/) | CC0 research data: 171 emotion concepts with attribution, rendering groups, sounds, dialect tables. |
| [`EVAL-PREREG.md`](EVAL-PREREG.md) | Every evaluation bar, sample size and stop rule: frozen by git tag, with dated amendments made before each run's data and any deviation recorded. |
| [`evidence/`](evidence/) | Published results, regenerable from the raw files. |
| [`bench/`](bench/) | The harnesses that produced them: text benches through real Hermes sessions and the blind listening test. |
| [`VISION.md`](VISION.md) | Why the project exists and where it is going. |
| [`tests/`](tests/) | Unit tests and render goldens. |

## Evaluation so far

- The first complete pre-registered text run (scope A3, run 3) passed its mechanical gate. It used Claude Opus 5.5 and
  gpt-6.1-sol, 684 replies each, judged cross-model:
  [evidence/a3-run-3](evidence/a3-run-3/README.md).
- The blind listening test (E7) is built and comes next.

We state the research the cues follow at the strength it supports. A cue records the delivery the agent chose, not a
measured internal state; see [plugin/README.md](plugin/README.md#grounding).

## Links

[Releases](https://github.com/100yenadmin/ai-emotion-chip/releases) ·
[CHANGELOG](CHANGELOG.md) · [CONTRIBUTING](CONTRIBUTING.md) · [SECURITY](SECURITY.md) · [LICENSE](LICENSE)

## License

Code: Apache-2.0 ([LICENSE](LICENSE)). Data in `plugin/emotion_chip/data/`: CC0-1.0 ([LICENSE-DATA](LICENSE-DATA)).
