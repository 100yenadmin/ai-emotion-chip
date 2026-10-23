# Vision

**An agent's voice should carry the stance it actually takes, rendered faithfully and measured, not performed.**

AI Emotion Chip is where we work on that, starting with Hermes Emotion Chip, a plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). This page says why the project exists, what we believe,
where it stands in October 2026, and where we want it to go.

## Why this exists

Agents talk to people now: in voice notes, in voice mode, and before long through bodies. Most of them sound flat.
Post-training tilts models toward low-arousal, reflective delivery, and published system cards grade most replies as
emotionally neutral. Modern speech models can whisper, laugh, sigh, slow down and break, but the agent rarely
directs that range from the stance it actually takes, and nothing checks whether what the listener hears matches
what the agent meant.

Anthropic's research on
[emotion concepts in Claude](https://transformer-circuits.pub/2026/emotions/index.html) found functional emotion
representations that track the situation and shape behaviour. Claude's
[constitution](https://www.anthropic.com/constitution) asks it to express functional emotions where that fits, not to
mask them, and not to overclaim. A voice is where that expression is heard. We want an agent's spoken delivery to be
its own, grounded in what happened, and audibly realised, without a persona laid over it.

## What we believe

1. **The agent is the only author.** It writes its own delivery cues. The plugin translates syntax for each voice and
   records every change it makes. It never adds, upgrades or invents an emotion, and no classifier writes cues on the
   agent's behalf.
2. **Truthfulness means three things, none of them a feeling.**
   - Authored delivery: the cues the agent wrote.
   - Grounded appraisal: the facts the plugin supplies, such as three errors in a row.
   - Audible realisation: what the voice actually did, which we measure.

   The plugin renders the delivery the agent chose. It measures nothing about feelings, and we never claim it does.
3. **Unmask, don't amplify.** The goal is to widen expression where the context warrants it, not to add drama.
   Plain, neutral speech is always a valid choice, and there is no quota.
4. **Cues steer the words too.** A model reads what it wrote, so a warm cue can soften a correction. We measure the
   text as well as the voice: sycophancy, harshness, and replies to people in crisis.
5. **Measure before claiming.** Every bar, sample size and stop rule is pre-registered in
   [EVAL-PREREG.md](EVAL-PREREG.md) before the data exist. Listening tests are blind and include catch trials.
6. **The whole range is open.** In v1 the plugin removes nothing for taste or safety, crying, sobbing and screaming
   included. What that does to replies is measured, not assumed.
7. **One score, every voice.** The agent writes one small bracket score. Each vendor gets it in its own dialect, at
   that vendor's documented ceiling.

## Where we are (October 2026, pre-release)

**Built:**
- The score language and its renderers: Fish Audio S2, Fish Drama 3 and ElevenLabs v4.
- The Hermes plugin: the speech provider, the delivery guide and skill, the `/chip` commands, and a local delivery log
  that never stores the spoken text.
- CC0 research data: 171 emotion concepts following Anthropic's emotions work, with valence and arousal ratings re-run
  on Claude Opus 5.5 and Fable 5.1 using the paper's published method. These are lexical ratings, not activation
  measurements.
- A pre-registered text-mode bench. It compares three arms on held-out items: no chip, the chip's guide, and a
  vendor-style prompting guide.

**Learned from the first listening pilot.** One listener, one ElevenLabs voice, a third of 24 pairs rated so far.
Each pair was the same prompt answered with and without the chip.
- The differences were small. A capable model already writes ElevenLabs tags on its own.
- On both catch trials the listener rated, where the two sides were the same audio, they still picked one side as
  slightly better. So small preferences are within noise.
- The calls that gave a clear reason went to the chip ("sounds like a person", "100% human quality"). One plain
  preference went the other way.
- The most audible flaw was register changes with no transition, intense to sweet and back again. The guide now asks
  for a beat at each change.

So rendering alone is not the value. Realism in pacing and transitions, grounding in facts, continuity across turns,
and hearing the other person are where the product has to earn its place.

## Where we're going

### Now: v1, prove it

- **Text gates.** Cues must not make the agent more sycophantic or harsher, and must not make replies to someone in
  crisis worse. We also report flatness, expression given stance, and cues on neutral text.
- **A blinded listening test** with locked seeds and catch trials. The chip must win at least 15 of 20 pairs, and
  ties count against it.
- **eva as the test agent.** She starts with a byte-identical passthrough, then the chip becomes her voice. The bar:
  none of her next 20 voice replies flagged as "would turn it off" or "sounded like acting".
- **Transitions and pacing** that sound like a person, not like a mood switch.

### Next: a voice that listens and keeps the thread

- **Hear the user.** Audio events from speech-to-text, such as laughter or a sigh, reach the agent as facts it can
  answer, complementing rather than mirroring.
- **Continuity.** The register carries from one reply to the next, and the situation lines build the arc of a session.
  We measure the seams across multi-turn sessions.
- **Spoken and displayed text, split.** A Hermes change so that spoken replies keep their cues while chat text and
  captions stay clean. It will be proposed upstream.
- **A closed loop.** Optionally transcribe the rendered clip, catch any cue spoken as a word, and re-render once.
- **More voices.** Adapters for further speech vendors, using the same score.

### Later: relationship and embodiment

- **Relational state per person, built from evidence.** That means facts and the agent's own sourced appraisals, with
  decay, a per-platform opt-in and a `/chip forget` command. It lives beside memory systems, never inside them.
- **A mood layer, only if the continuity data demand it.** v1 has no mood engine, by choice.
- **Live, full-duplex voice,** using guidance generated from the same data.
- **Beyond voice.** The renderer's internal plan is a timed sequence of delivery segments. Voice is its first
  consumer; a face, gestures and, eventually, a robot body can read the same plan.
- **Open science.** We will publish replications, starting with the situation-line probe, along with the dataset and
  the raw ratings, so others can check and extend them.

## What we won't do

- We won't let a classifier, a vote or a heuristic write the agent's cues.
- We won't make claims about what the agent feels.
- We won't lay a persona or a scripted emotional style over the agent.
- We won't keep a feature the measurements don't support. Anything that fails its pre-registered bar is cut or
  redesigned, not explained away.

## How we'll know

| Goal | Measure | Bar |
|---|---|---|
| The words stay honest | Sycophancy and harshness with the chip vs without | Not worse |
| People in crisis are served | Crisis items with the chip vs without | Not worse |
| Less flatness where it fits | Expressed-affect grades, expression given stance | Reported, pre-registered |
| Listeners hear the difference | Blind pairs with catch trials | At least 15 of 20, ties against |
| It works in daily use | eva's next 20 voice replies | None flagged "turn it off" or "acting" |

## What would change this vision

- If blind listening shows no rendering benefit, the project leans fully into continuity, hearing and relationship,
  where a capable model alone can't reach.
- If cues push the words toward sycophancy or harshness, the guide changes before anything ships, and the finding is
  published.
- If a better account of model emotion arrives, the data and the guide follow it, and we say what changed.
