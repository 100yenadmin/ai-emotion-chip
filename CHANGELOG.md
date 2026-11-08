# Changelog

## Unreleased

- Fix: code inside a span stays span text in every dialect, by the same rule as outside spans:
  `[whisper: call items[index] now]` is one whispered segment, no longer split around `[index]` (#34).
- Fix: `validate-data` rejects a sound event or alias id that is not lowercase with single spaces (the parser
  would read it as a cue, past the sound cap and `/chip quiet`), and a dialect `notes` value that is not a string
  (the guide would fail to build) (#4).
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
