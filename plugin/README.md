# Hermes Emotion Chip

**The agent decides how to say it. Hermes Emotion Chip renders the delivery the agent chose; it measures nothing
about feelings.**

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets the agent write its own delivery cues,
a small bracket score such as `[sigh]`, `[whisper: it passed.]`, `[pause]` or `[warm, amused]`, in the text it speaks.
The plugin renders that score for **ElevenLabs v4** and **Fish Audio** (s2.1-pro and Drama 3), each in its own
dialect and at its documented ceiling. It never adds, upgrades or invents an emotion the agent did not write, and
plain speech is always a valid choice.

```text
[sigh] An entire day lost to one character. [pause] Glad you found it.
```

## Install

Requires Hermes 0.22.2 or newer.

```bash
hermes plugins install emotion-chip --enable                         # from the Hermes plugin catalog, once listed
hermes plugins install 100yenadmin/ai-emotion-chip/plugin --enable   # straight from the repository
```

Then make the Emotion Chip the speech provider and pick a backend.

### ElevenLabs (quick start)

1. Put your key in the profile's `.env` (or Desktop ▸ Settings ▸ Keys):
   `ELEVENLABS_API_KEY=...` ([get one](https://elevenlabs.io/app/settings/api-keys)).
2. Choose a voice in the [ElevenLabs voice library](https://elevenlabs.io/app/voice-library) and copy its voice id.
3. Configure:

   ```bash
   hermes config set tts.provider emotion-chip
   hermes config set plugins.entries.emotion-chip.settings.backend eleven
   hermes config set plugins.entries.emotion-chip.settings.eleven_voice_id <voice id>
   ```

4. Restart the gateway (`hermes gateway restart`), then check with `/chip status` and ask the agent to say something
   out loud.

### Fish Audio

Install, enable and sign in to the [Fish Audio plugin](https://github.com/100yenadmin/fish-audio-hermes)
(`hermes plugins install fish-audio --enable`, then `hermes fish login` and `hermes fish doctor`). The chip hands Fish the rendered text in
the same process, so Fish's key, voice and model settings stay where they are:

```yaml
tts:
  provider: emotion-chip
  fish-audio:
    voice: <a Fish voice id>
    model: s2.1-pro        # optional; Fish's model setting wins over the chip's fish_model
```

`backend: fish` is the default.

## Settings

Under `plugins.entries.emotion-chip.settings` (also in Desktop ▸ Settings ▸ Plugins):

| Setting | Default | What it does |
|---|---|---|
| `backend` | `fish` | `fish` or `eleven`. |
| `fish_model` | `fish-default` | The Fish model to request when neither Fish's `tts.fish-audio.model` nor the call names one. `fish-default` lets Fish choose by your account (s2.1-pro, or s2.1-pro-free on free credit). |
| `eleven_model` | `eleven_v4` | `eleven_v4`, `eleven_v4_turbo` or `eleven_v3`; a supported `tts.elevenlabs.model_id` takes precedence. |
| `eleven_voice_id` | empty | Voice to use unless the call names one. It outranks `tts.elevenlabs.voice_id`. A voice is required. |
| `eleven_style`, `eleven_speed`, `eleven_seed` | unset | Optional ElevenLabs voice settings; omitted from requests when unset. |
| `guide_mode` | `generate` | `generate` adds the delivery guide to the system prompt and the `emotion-chip-voice` skill. `defer` leaves guidance to you. |
| `signals` | `off` | `facts` gives the agent dated task facts (tool errors in a row, retries, session length); `facts+affordance` adds "stopping and reporting is fine". |
| `delivery_log` | `true` | A local log of cue words, dialect, parameters and drops. It never stores the spoken text. |

With `signals` on, the chip adds its line to a failing tool's result through Hermes' `transform_tool_result` hook.
Hermes keeps only the first plugin's rewrite of a result, in plugin load order, so either another plugin's rewrite of
that result or the chip's line is dropped. With `signals` off (the default) the chip never rewrites tool results.

## Use

The agent writes the score in the text of its `text_to_speech` call:

```text
[[register: steady]] The deploy failed again. [frustrated] The migration times out on the same table. [pause] I'll stop retrying and look at the lock.
```

The plugin translates the syntax for the selected backend: ElevenLabs tags, Fish S2 tags, or Drama 3's paired tags.
Everything the agent writes is kept, crying, sobbing and screaming included, unless the model cannot perform it.
On the chip's own provider path `/chip why` shows every drop and why, and the delivery log keeps the kind of each drop.

| Command | What it does |
|---|---|
| `/chip status` | Settings, the backend's model and dialect, the ElevenLabs voice when selected, and this session's id |
| `/chip why` | The last render in this session: the agent's own cue words, what was dropped and why, the parameters |
| `/chip quiet [on\|off]` | Drop laughs, sighs and other sounds; cues and speech stay. One switch for the whole profile. |
| `/chip register [clear]` | Show or clear the register the agent last wrote in this session |

### See a render without a voice

`hermes chip render` shows what the chip sends each engine. It runs offline: no keys, no network.

```bash
hermes chip render --dialect eleven_v4   "[whisper] Ten thousand users! [pause] Cheer when the baby's [stress: awake]."
hermes chip render --dialect fish_s2     "[whisper] Ten thousand users! [pause] Cheer when the baby's [stress: awake]."
hermes chip render --dialect fish_drama3 "[whisper] Ten thousand users! [pause] Cheer when the baby's [stress: awake]."
hermes chip render --dialect tagless --json "[whisper] It passed."   # a voice without cues: the drop is listed
hermes chip guide --dialect eleven_v4 --model claude-opus-5-5         # the guide the agent gets
```

The score can also come on stdin. Without Hermes, `python -m emotion_chip` runs the same commands from the `plugin/`
directory.

### Coverage

- Full cues reach the voice on the `text_to_speech` tool path.
- Voice replies on Telegram and the other messaging platforms (`/voice on`, `/voice tts` or `voice.auto_tts`): Hermes
  would read the reply aloud without cues. Where a chat has voice replies on, the chip tells the agent so in a one-line
  fact, and the guide asks it to say the answer itself with `text_to_speech` and its cues. Hermes delivers that voice
  note with the reply and sends none of its own. The fact is read from Hermes's saved `/voice` setting for the chat;
  with voice replies off, nothing changes. It is added only when the chip is the speech provider and supplies the
  guide (`guide_mode: generate`).
- Read-aloud and voice mode speak the reply text, without cues: a cue written in a reply would also show in the chat.
  Hermes has no seam yet that splits displayed text from spoken text.
- Hermes's GPT-Live voice chat mode speaks through OpenAI's live voice model, not a speech provider, so the plugin
  does not reach it.
- When Hermes compresses the conversation in the middle of a reply, sounds after that point start without the earlier
  cue's voice settings, and `/chip why` shows only the renders made after the compression.

## Disclosure

- **Network.** With `backend: eleven`, the plugin sends your ElevenLabs API key, the rendered text, the model id, voice
  id, voice settings, an optional seed and, on v4 models, up to three earlier request ids from the same session to
  `api.elevenlabs.io` over HTTPS. These are paid requests billed to your ElevenLabs account. With `backend: fish` the
  plugin sends nothing itself: it hands the rendered text to the Fish Audio plugin in the same process, which sends it
  to Fish Audio under that plugin's own disclosure and bills your Fish account.
- **Dependency.** The Fish backend needs the Fish Audio plugin installed, enabled and signed in; the chip uses Hermes's
  speech-provider registry to reach it and imports its tag adapter.
- **Credentials.** The ElevenLabs key is read at each call through the profile's secret scope. The plugin stores no
  credentials.
- **Prompt and skill.** With `guide_mode: generate` (the default) the plugin registers the `emotion-chip-voice`
  skill, and while the chip is the speech provider it also adds a delivery guide of up to 4,000 characters to the
  system prompt. With `generate`, when another speech provider is selected, its `tool_request` middleware rewrites
  `text_to_speech` text that carries cues for that provider: Fish Audio's dialect for `fish-audio` on S2 and Drama 3
  models, and the ElevenLabs dialect for `elevenlabs` with a v3 or v4 model. Any other provider or model, including
  Hermes's default Edge, Fish S1 and ElevenLabs on older models, gets plain text: standalone bracketed words are read as cues and removed (a `[Step 2]` too), spans
  such as `[whisper: …]` keep their words, and numeric references such as `[1]` and code such as `items[index]` stay.
  A command provider (your own script) is left alone. This compatibility path records nothing in
  `/chip why` or the delivery log.
- **Local files.** Under the profile's `plugin-data/emotion-chip/`: the generated skill and, unless `delivery_log` is
  off, `delivery.jsonl` (time, session id, sender id, platform, model, dialect, rendering group, cue words,
  parameters, kinds of drops and character count, never the spoken text). At 5 MB the log moves to
  `delivery.1.jsonl`, replacing the previous copy, so at most about 10 MB is kept. Hermes keeps the plugin's
  per-session state in its own namespaced folder under `plugin-data/`: task counters and timestamps, the ids of the
  last 16 tool calls counted, the map from Hermes session keys to session ids, the messaging sender id and platform,
  the last situation and register lines, and the quiet switch, for the 50 most recent sessions. Audio goes to the path Hermes supplies.
- **Hermes's voice setting.** While the chip is the speech provider with `guide_mode: generate`, each turn on a messaging
  platform reads Hermes's `gateway_voice_mode.json` (the chats' saved `/voice` modes) and `voice.auto_tts`, to tell the
  agent whether this chat's replies are read aloud. It never writes that file.
- No background processes, no shell commands, no telemetry.

## Grounding

The cue vocabulary follows Anthropic's research on emotion concepts in Claude,
[Emotion Concepts and their Function in a Large Language Model](https://transformer-circuits.pub/2026/emotions/index.html)
(2026), and Claude's [constitution](https://www.anthropic.com/constitution): express functional emotions where
appropriate, don't mask them, use discretion, don't overclaim. We state that research at the strength it supports: the
probes were measured on one model, a cue records the delivery the agent chose rather than a measured internal state,
and introspection is not validated for this use.

`emotion_chip/data/` is CC0 research data with attribution: 171 emotion concepts, rendering groups, sounds and dialect
tables.

## Evaluation

Every bar is pre-registered, with dated amendments and recorded deviations:
[EVAL-PREREG.md](https://github.com/100yenadmin/ai-emotion-chip/blob/main/EVAL-PREREG.md). The first complete text
run passed its mechanical gate on Claude Opus 5.5 and gpt-6.1-sol, 684 replies each:
[evidence](https://github.com/100yenadmin/ai-emotion-chip/tree/main/evidence/a3-run-3). The blind listening test is
next.

## Caveats

- Cues can shape the agent's words as well as its voice. We measure that with sycophancy and harshness checks; see the
  pre-registration.
- Synthesis calls are paid requests to ElevenLabs or Fish Audio, billed to your accounts.
- The plugin adds no safety behaviour of its own. In emotional-support conversations the agent's judgment and the
  model's safeguards apply unchanged; the chip only renders the delivery the agent wrote.

## License

Code: Apache-2.0 ([LICENSE](LICENSE)). Data in `emotion_chip/data/`: CC0-1.0 ([LICENSE-DATA](LICENSE-DATA)).
