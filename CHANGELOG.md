# Changelog

## 0.3.2 — 2026-11-10

Docs only. The plugin's code is unchanged from 0.3.1.

- **Docs: the demo film plays on the front page.** The README shows the demo video under the demo link.

## 0.3.1 — 2026-11-10

Bug fixes. No new features.

- **Fix: `chip_preview` shows what the voice is actually sent** (#48). For the profile's own dialect the preview now
  runs the speech provider's own preparation for a call from Hermes. On the Fish backend that includes Fish's tag
  adapter, which turns Fish-native cues in parentheses such as `(laughs)` into bracket cues; the preview used to skip
  it. It also shows the final speed (Hermes's `tts.speed`, or Fish's speed, times the cue's), the provider's notes
  (the S1 tagless route, a pinned Fish model) and ElevenLabs style and speed settings. It still writes nothing. Any
  other dialect, and the offline `hermes chip render`, render the score as written.
- **Docs: a front page you can act on.** The README opens with what the plugin does, true badges, the install line
  that works on every Hermes (the catalog name fails on Hermes 0.23.0), three steps to a first result, voice setup for
  Fish Audio and ElevenLabs, where the agent's delivery is and is not heard, a privacy and cost summary, troubleshooting
  and how to report a bug. It links the demo, and plays one line both ways: plain, and with the agent's score.

## 0.3.0 — 2026-11-09

The agent can see its own voice. Until now it wrote a score, called `text_to_speech` and learned nothing back.

- **Agent tool `chip_status`** (read-only): whether the voice can speak with this profile's settings, what to fix if
  not, the engine, model, dialect and voice in use, whether voice replies are covered, and the register the agent last
  wrote. The same facts as `/chip status`, from one shared source.
- **Agent tool `chip_preview`** (offline, no cost): what the voice engine would receive for a score, and which cues it
  would drop and why, without synthesizing. The same result as `hermes chip render --json`, with the profile's quiet
  switch applied.
- **A render report on each `text_to_speech` result**: with the chip as the speech provider, a successful result gains
  an `emotion_chip` entry (dialect, the agent's cue words, drops and notes, at most 600 characters), so the agent learns
  what the engine did with its score. It stays in the conversation and is sent nowhere; any `media:` in it is escaped so
  Hermes never reads it as an attachment.
- Both tools and the report appear only with `guide_mode: generate`; `defer` stays a byte-identical passthrough.

## 0.2.1 — 2026-11-09

Fixes from an independent review of 0.2.0. No new features.

- **Fix: the delivery log never stops speech** (#43). The log is written before synthesis, and a failed write (a full
  disk, an unwritable `plugin-data/` folder) used to fail every render. It now logs one warning and speech goes on;
  later rows are written once the folder is writable again.
- **Fix: a failed voice reply is recovered** (#44). Hermes skips its own voice note after any `text_to_speech` call in
  the turn, a failed one too. So when the agent's call failed (text over the model's limit, a bad argument), a chat with
  voice replies on got no voice note, or the reply read without cues. On those turns the line now asks the agent to fix
  the cause and try once more. The Hermes side is filed upstream (NousResearch/hermes-agent#151862).
- **Fix: `/chip status` is honest about what it checked** (#45). When it cannot read the profile's key, or the
  fish-audio plugin's own check fails, it says `ready: probably (not checked from here: …)` instead of `ready: yes`. A
  new `voice_replies:` value says whether voice replies are covered and, if not, why: no message seen yet, no
  voice-reply check in this Hermes, the wrong settings, or a restart needed after switching to the chip.
- **Docs: install from the repository first.** On a fresh Hermes 0.23.0, installing by catalog name can fail: Hermes
  falls back to the catalog copy it shipped with, which predates this plugin (NousResearch/hermes-agent#151856).

## 0.2.0 — 2026-11-09

Voice replies on messaging platforms, a `hermes chip` command and a readiness check.

- **Voice replies carry the agent's delivery** (#40). With voice replies on in a chat (`/voice on`, `/voice tts` or
  `voice.auto_tts`), Hermes read the reply aloud itself, without cues. When a message comes in, the chip now asks Hermes
  whether it will voice the reply, and on that message's turn adds one line asking the agent to say its answer with
  `text_to_speech` and its score; Hermes then sends no plain voice note of its own. Everything else is unchanged,
  including the system prompt. Not on Discord, where Hermes plays its own reply in voice channels. Only with the chip
  as the speech provider and `guide_mode: generate`.
- **`hermes chip render` and `hermes chip guide`** (#39). See what the chip sends ElevenLabs v4, Fish S2 or Drama 3 for
  any score, or the guide the agent gets, offline: no keys, no network.
- **`/chip status` says whether the chip can speak** (#41): `ready: yes`, or `ready: no` with each fix (the speech
  provider, the ElevenLabs key or voice, the Fish Audio plugin).

## 0.1.1 — 2026-11-08

Fixes from the first release's review backlog (#3, #4, #34). No new features; the Fish model setting gains a
real `fish-default` choice.

- Fix: code inside a span stays span text in every dialect, by the same rule as outside spans:
  `[whisper: call items[index] now]` is one whispered segment, no longer split around `[index]` (#34).
- Fix: `validate-data` rejects a sound event or alias id the parser cannot read as a sound (not lowercase and
  single-spaced, no letter, a bracket, or span-shaped; such a sound was read as a cue, past the sound cap and
  `/chip quiet`), and a dialect `notes` value that is not a string (the guide would fail to build) (#4).
- Fix: after `/new`, `/chip status`, `/chip why` and `/chip register` show the new conversation (Hermes 0.22.2); the
  plugin now registers `on_session_reset`. When several chats share the old session (after `/resume` from a second
  chat), each one follows from its next message (#3).
- Fix: with `signals` on, the situation line goes on a failing tool's own result, counts that failure (it used to lag
  one call on Hermes's serial tool path), keeps a JSON result's formatting and its own `situation` key, never raises on
  deeply nested JSON, and reads no plugin state while `signals` is off (#3).
- Fix: the `emotion-chip-voice` skill follows a live Fish model or backend change without a restart (#3).
- Change: `fish_model` has a real `fish-default` choice, now the default. Fish still picks the model by your account
  when nothing names one, and `/chip`, the delivery log and Desktop now say so instead of claiming `s2.1-pro` (#3).
- Docs: `/chip quiet` is one switch for the whole profile and says so; the plugin README states that Hermes keeps only
  the first plugin's tool-result rewrite and what happens when Hermes compresses mid-reply (#3).
- Docs: the plugin README states exactly what the compatibility path does when another speech provider is selected
  (with `guide_mode: generate`). Fish S2 and Drama 3 and ElevenLabs v3/v4 get their dialects. Other providers and
  models, Fish S1 included, get plain text: standalone bracketed words are removed as cues, recognised or not, while
  spans keep their words and numeric references and code stay. Command providers are left alone.

## 0.1.0 — 2026-11-08

First release.

- **The agent writes its own delivery.** A small bracket score in the text of a `text_to_speech` call: sentence cues
  (`[slightly frustrated]`, `[warm, amused]`), spans (`[soft: …]`, `[whisper: …]`, `[slow: …]`, `[stress: word]`,
  `[smiling: …]`), sounds (`[sigh]`, `[laugh]`, `[chuckle]`, `[gasp]`, `[breath]`) and pauses. The plugin never adds,
  upgrades or removes an emotion word the agent did not write.
- **Two voice backends.** ElevenLabs v4, v4 turbo and v3 over REST, with request stitching on v4; Fish Audio S2
  (s2.1-pro) and Drama 3 through the Fish Audio plugin in the same process. Each score is translated into that
  vendor's dialect at its documented ceiling, and anything a model cannot perform is dropped and recorded.
- **Delivery guide.** A system-prompt section and the `emotion-chip-voice` skill teach the score. Plain speech is
  always a valid choice.
- **`/chip` command.** `status`, `why` (the last render: the agent's cue words, drops and parameters), `quiet` and
  `register`.
- **Optional situation facts** (`signals`): dated task facts such as errors in a row and retries. Off by default.
- **Delivery log.** Local metadata without the spoken text; it can be turned off.
- **CC0 emotion data.** 171 concepts with attribution, rendering groups, sounds and dialect tables.
- **Evaluation.** Pre-registered bars in `EVAL-PREREG.md`; scope A3 run 3 passed its mechanical gate (#26). This
  release has no guardrails, so the guarded design's gates G1, G2 and G3 were not validly measured (amendment A3);
  run 1's observed numbers, including one G3 failure, are in `evidence/run-1/`.
- **Hermes compatibility.** Requires Hermes 0.22.2 or newer; tested on 0.23.0 (#29). The ElevenLabs key is read
  through the profile's secret scope (#30). The installable plugin lives in `plugin/` (#31).
