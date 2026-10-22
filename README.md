# AI Emotion Chip: Hermes Emotion Chip

**Hermes Emotion Chip renders the delivery the agent chose. It measures nothing about feelings.**

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that gives an agent the full expressive range
of modern text-to-speech, so its voice can carry the stance it actually takes in a conversation. That range covers
tone, emphasis, pacing, softness, whispers, breaths, sighs and laughter, and the first two launch voices are
**ElevenLabs v4** and **Fish Audio** (s2.1-pro and Drama 3).

> Status: pre-release, under active development. Nothing here is released yet. Fish Audio and ElevenLabs backends
> are implemented. See [EVAL-PREREG.md](EVAL-PREREG.md) for how we will judge whether it
> works.

## The idea in one paragraph

The agent writes its own delivery cues inline, in a small bracket score such as `[slightly frustrated]`,
`[soft: I'm glad you told me.]` or `[pause]`. The plugin never adds an emotion the agent did not write. It gives the
agent facts it cannot see (for example "three errors in a row"), then translates the score into each vendor's dialect
at that vendor's documented ceiling. The whole range is open to the agent, crying, sobbing, screaming and
whispering included; the plugin removes nothing for taste or safety. Plain neutral speech is always a valid choice.

## Install (pre-release)

The plugin needs Hermes 0.22.2 or newer. For the default Fish backend, the
[Fish Audio plugin](https://github.com/100yenadmin/fish-audio-hermes) must be installed, enabled and signed in
(`hermes fish doctor` passes). The Emotion Chip hands audio work to Fish, so its key, voice and model settings stay
where they are. The ElevenLabs backend uses REST directly and needs no Fish plugin or extra SDK.

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
| `backend` | `fish` | Voice backend: `fish` or `eleven`. |
| `fish_model` | `s2.1-pro` | The Fish model to request when neither Fish's `tts.fish-audio.model` nor the call names one. |
| `eleven_model` | `eleven_v4` | `eleven_v4`, `eleven_v4_turbo` or `eleven_v3`; a supported `tts.elevenlabs.model_id` takes precedence. |
| `eleven_voice_id` | empty | Voice to use unless the call names one (Hermes passes the top-level `tts.voice`). It outranks `tts.elevenlabs.voice_id`, which Hermes fills with a stock voice by default. A voice is required. |
| `eleven_style` | `null` | Optional style in `voice_settings`; omitted when unset. |
| `eleven_speed` | `null` | Optional speed in `voice_settings`; omitted when unset. |
| `guide_mode` | `generate` | `generate` adds the delivery guide to the system prompt and the `emotion-chip-voice` skill. `defer` leaves guidance to you, for example an existing voice file. |
| `signals` | `off` | `facts` gives the agent dated task facts (tool errors in a row, retries, session length). `facts+affordance` adds "stopping and reporting is fine". Off until its pilot is measured. |
| `delivery_log` | `true` | A local log of cue words, dialect, parameters and drops. It never stores the spoken text. |

If you keep `tts.provider: fish-audio`, the plugin still renders a score the agent writes in a `text_to_speech` call
(with `guide_mode: generate`), but it adds the delivery guide to the prompt only when it is the speech provider.

### ElevenLabs

Set the plugin's `backend` to `eleven`, keep `tts.provider: emotion-chip`, and set `eleven_voice_id` to your voice
id (or `tts.elevenlabs.voice_id`, which applies only when `eleven_voice_id` is empty). Supply `ELEVENLABS_API_KEY` through your Hermes profile environment; the key is read
at each call. `eleven_style: 0.30` and `eleven_speed: 0.90` can match an existing voice route. Stability and similarity
come from the rendered score (neutral defaults: 0.5 and 0.75).

All three models use the ElevenLabs v4 score dialect. The rendered text limit is 10,000 characters for `eleven_v4`
and 5,000 for `eleven_v4_turbo` and `eleven_v3`; longer text raises an error. Hermes first splits speech into pieces of
at most 4,000 characters; raise that with `tts.emotion-chip.max_text_length`. File synthesis supports mp3, ogg/opus,
24 kHz PCM and WAV. Streaming produces 24 kHz PCM. Sequential v4/v4 turbo file requests reuse up to three request
ids from the same session for two hours; v3 and streams do not use request stitching.

## Use

The agent writes the score in the text of its `text_to_speech` call. For example:

```text
[[register: steady]] The deploy failed again. [frustrated] The migration times out on the same table. [pause] I'll stop retrying and look at the lock.
```

The plugin translates the syntax for the selected backend (ElevenLabs tags, Fish S2 tags, or Drama 3's paired tags).
Everything the agent writes is kept, crying, sobbing and screaming included, unless the model cannot perform it. Every change is recorded.
It never adds or upgrades an emotion word.

| Command | What it does |
|---|---|
| `/chip status` | Settings, the backend's model and dialect, the ElevenLabs voice when selected, and this session's id |
| `/chip why` | The last render in this session: the agent's own cue words, what was dropped and why, the parameters |
| `/chip quiet [on\|off]` | Drop laughs, sighs and other sounds; cues and speech stay. One switch for the whole profile, not per chat. |
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
- Synthesis calls are paid requests to ElevenLabs or Fish Audio, billed to your accounts.

## License

Code: Apache-2.0 ([LICENSE](LICENSE)). Data in `emotion_chip/data/`: CC0-1.0 ([LICENSE-DATA](LICENSE-DATA)).
