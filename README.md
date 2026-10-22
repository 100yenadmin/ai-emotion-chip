# AI Emotion Chip: Hermes Emotion Chip

**Hermes Emotion Chip renders the delivery the agent chose. It measures nothing about feelings.**

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that gives an agent the full expressive range
of modern text-to-speech, so its voice can carry the stance it actually takes in a conversation. That range covers
tone, emphasis, pacing, softness, whispers, breaths, sighs and laughter, and the first two launch voices are
**ElevenLabs v4** and **Fish Audio** (s2.1-pro and Drama 3).

> Status: pre-release, under active development. Nothing here is released yet. See [EVAL-PREREG.md](EVAL-PREREG.md)
> for how we will judge whether it works.

## The idea in one paragraph

The agent writes its own delivery cues inline, in a small bracket score such as `[slightly frustrated]`,
`[soft: I'm glad you told me.]` or `[pause]`. The plugin never adds an emotion the agent did not write. It gives the
agent facts it cannot see (for example "three errors in a row"), then translates the score into each vendor's dialect
at that vendor's documented ceiling. Plain neutral speech is always a valid choice.

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
