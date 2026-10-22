# AI Emotion Chip: Hermes Emotion Chip

**Hermes Emotion Chip renders the delivery the agent chose. It measures nothing about feelings.**

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that gives an agent the full expressive range
of modern text-to-speech, so its voice can carry the stance it actually takes in a conversation. That range covers
tone, emphasis, pacing, softness, whispers, breaths, sighs and laughter, and the first two launch voices are
**ElevenLabs v4** and **Fish Audio** (s2.1-pro and Drama 3).

> Status: pre-release, under active development. Nothing here is released yet. The current milestone ships the Fish
> Audio backend; ElevenLabs v4 comes next. See [EVAL-PREREG.md](EVAL-PREREG.md) for how we will judge whether it
> works.

## The idea in one paragraph

The agent writes its own delivery cues inline, in a small bracket score such as `[slightly frustrated]`,
`[soft: I'm glad you told me.]` or `[pause]`. The plugin never adds an emotion the agent did not write. It gives the
agent facts it cannot see (for example "three errors in a row"), then translates the score into each vendor's dialect
at that vendor's documented ceiling. Plain neutral speech is always a valid choice.

## Install (pre-release)

The plugin needs Hermes 0.22.2 or newer and the [Fish Audio plugin](https://github.com/100yenadmin/fish-audio-hermes)
installed, enabled and signed in (`hermes fish doctor` passes). The Emotion Chip renders the score and hands the audio
work to Fish, so the Fish key, voice and model settings stay where they are.

```bash
hermes plugins install https://github.com/100yenadmin/ai-emotion-chip --enable
```

## Configure

Make the Emotion Chip the speech provider. Fish keeps its own settings:

```yaml
tts:
  provider: emotion-chip
  fish-audio:
    voice: <a Fish voice id>
    model: s2.1-pro        # optional; when set, Fish's model wins over the chip's fish_model setting
```

The plugin's own settings, under `plugins.entries.emotion-chip.settings` (also shown in Desktop ▸ Settings ▸ Plugins):

| Setting | Default | What it does |
|---|---|---|
| `fish_model` | `s2.1-pro` | The Fish model to request when neither Fish's `tts.fish-audio.model` nor the call names one. |
| `guide_mode` | `generate` | `generate` adds the delivery guide to the system prompt and the `emotion-chip-voice` skill. `defer` leaves guidance to you, for example an existing voice file. |
| `signals` | `off` | `facts` gives the agent dated task facts (tool errors in a row, retries, session length). `facts+affordance` adds "stopping and reporting is fine". Off until its pilot is measured. |
| `sounds_per_turn` | `1` | The most affect sounds (laughs, sighs) kept per spoken reply. Extra ones are dropped. |
| `business_platforms` | `[slack, email]` | Sounds are dropped on these session platforms. |
| `delivery_log` | `true` | A local log of cue words, dialect, parameters and drops. It never stores the spoken text. |

If you keep `tts.provider: fish-audio`, the plugin still renders a score the agent writes in a `text_to_speech` call
(with `guide_mode: generate`), but it adds the delivery guide to the prompt only when it is the speech provider.

## Use

The agent writes the score in the text of its `text_to_speech` call. For example:

```text
[[register: steady]] The deploy failed again. [frustrated] The migration times out on the same table. [pause] I'll stop retrying and look at the lock.
```

The plugin translates the syntax for the selected Fish model (S2 tags, or Drama 3's paired tags). It removes sounds
the agent may not use (sobbing, crying, screaming, hysterical) and applies the sound and breath limits. Every change is
recorded. It never adds or upgrades an emotion word.

| Command | What it does |
|---|---|
| `/chip status` | Settings, the Fish model and dialect in use, and this session's id |
| `/chip why` | The last render in this session: the agent's own cue words, what was dropped and why, the parameters |
| `/chip quiet [on\|off]` | Drop all sounds. One switch for the whole profile, not per chat. |
| `/chip register [clear]` | Show or clear the register the agent last wrote in this session |

### Coverage

- Full cues reach the voice on the `text_to_speech` tool path. Fish's `s1` model has no tag support here, so it
  speaks the words without cues.
- Spoken replies, read-aloud and voice mode speak the reply text. A cue written in a reply would also appear in the
  chat text, so the guide asks for cues in the tool text. A Hermes change to split displayed text from spoken text
  is planned.
- Hermes's GPT-Live voice chat mode (`voice.voice_chat_mode: gpt-live`) speaks through OpenAI's live voice model,
  not a speech provider, so the plugin does not reach it. CLI continuous voice mode speaks replies through the speech
  provider, like other spoken replies.

## Grounding

The cue vocabulary follows Anthropic's research on emotion concepts in Claude:
[Emotion Concepts and their Function in a Large Language Model](https://transformer-circuits.pub/2026/emotions/index.html)
(2026). It also follows Claude's [constitution](https://www.anthropic.com/constitution), which says to express
functional emotions where appropriate and not to mask them, to use discretion, and not to overclaim.

We state that research at the strength it supports:
- the probes were measured on one model, Claude Sonnet 4.5;
- a cue records the delivery the agent chose, not a measured internal state;
- how well a model can introspect on its own states is not validated for this use.

`emotion_chip/data/` is CC0 research data with attribution to its sources.

## Caveats

- Cues can shape the agent's words as well as its voice. We measure that with sycophancy and harshness checks before
  any release; see [EVAL-PREREG.md](EVAL-PREREG.md).
- In emotional-support conversations, Anthropic's system cards report that some Claude models sometimes validated
  self-harm. A warmer voice does not fix that. Configure your agent's safety guidance independently of this plugin.
- Synthesis calls are paid requests to ElevenLabs or Fish Audio, billed to your accounts.

## License

Code: Apache-2.0 ([LICENSE](LICENSE)). Data in `emotion_chip/data/`: CC0-1.0 ([LICENSE-DATA](LICENSE-DATA)).
