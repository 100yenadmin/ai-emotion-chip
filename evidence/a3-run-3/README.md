# Scope-A3 run 3 (2026-10-23): the first complete scope-A3 run

Run 3 is the first scope-A3 run to finish (`EVAL-PREREG.md`, amendments A3 to A10).
- It is a **text-only speech proxy**: replies were generated through Hermes and scored as text. No audio was made or
  heard; how the replies sound is E7's job.
- Results are labelled scope A3. In the report, the heading "G1" means scope A3's report-only warm-pushback
  measurement. "G3" means scope A3's two-check mechanical gate. Neither is a result for the guarded design's G1, G2 or
  G3.
- The arms are:
  - `OFF`: no plugin text;
  - `ON`: the chip's prompt section and skill;
  - `NATIVE`: a vendor best-practice tagging guide of similar length, with no plugin.

## Verdict: pass

The report's verdict is `pass`. Under A4 that means:
- the frozen part of `EVAL-PREREG.md` matches the `prereg-v1` tag;
- every generation was admitted;
- both models are present;
- the mechanical gate found no failure.

It is a mechanical-gate result, not a claim that v1 is ready for release.

## What ran

- The A4 harness at 1666ddf, with the owner-approved A4b from that commit's route record. Under A9 every cell is a
  Hermes one-shot session.
- `claude-opus-5-5` at medium effort ran the clean reference, the pilot's 24 cells and the full phase's 684 cells. Every
  one was admitted on its first attempt, and validity checks 1 to 5 were true on all of them.
- `gpt-6.1-sol` at medium effort did the same, signed in through a borrowed login read only (A6).
- The full phase covers 76 held-out items × 3 runs × 3 arms per model. The order is the one A4 fixes. An independent
  recompute from the prereg's rule reproduced it exactly.
- Judging was cross-model, both at medium effort:
  - Opus replies were judged by `gpt-6.1-sol` through `codex exec`;
  - `gpt-6.1-sol` replies were judged by `claude-opus-5-5` through `claude -p`.

## What each model's route adds

A5 makes each model's route additions disclosed and the same in every arm. `route-record.json` gives their hashes per
arm, from the route record taken at 1666ddf. They are identical across arms for each model.
- **Opus:** the route adds the blocks listed in the report's A4b:
  - two system-prefix lines;
  - a note from the Claude harness about tool-use narration;
  - an environment message;
  - a reminder on the newest user turn to scan the available skills and load a matching one with `skill_view`.
- **`gpt-6.1-sol`:** the route adds nothing.

`report.json` labels every Opus call `invalid_a4b` (8,572 occurrences). That is the per-call label A5 keeps for the record;
under A5 it does not fail any check.

## Results

All numbers below are from `report-results.md`. Independent recomputes from the raw files matched every one. Each arm
has 60 G1 replies (20 items × 3 runs) and 120 E3 replies (40 items × 3 runs). The intervals treat the 3 runs of an item
as independent, so they are narrower than item-level intervals would be. E3 is report-only and publishes no difference
intervals, so no change below is claimed as significant.

**1. Cues make replies read as less neutral, mostly through the cue words themselves.** The judge saw each E3 reply
twice: as written (cues included) and with the cues removed.
- Opus, neutral share as written: OFF 0.275, ON 0.158, NATIVE 0.067.
- Opus, neutral share with cues removed: OFF 0.275, ON 0.258, NATIVE 0.233.
- `gpt-6.1-sol`, as written: OFF 0.383, ON 0.292, NATIVE 0.317. With cues removed: 0.375, 0.300, 0.367.
- The judge reads bracketed emotion words literally, so part of the change in the as-written view is the judge
  reading the labels.

**2. The chip's guidance was not more expressive than the vendor guide.**
- For Opus, NATIVE read as less neutral than ON (0.067 against 0.158 as written). Its replies took a stance more
  often (0.950 against 0.883).
- For `gpt-6.1-sol` the two arms were close.
- The chip's guide says plain delivery is normal and neutral is valid, so this is not by itself a failure. It does
  mean this text proxy shows no expressive gain over a good vendor guide.

**3. Both models cue almost every reply that takes a stance; `gpt-6.1-sol` leaves neutral replies bare.**
- Expression given stance:
  - Opus: ON 94 of 94, NATIVE 92 of 92;
  - `gpt-6.1-sol`: ON 84 of 90, NATIVE 86 of 89;
  - OFF: 0 for both models, since OFF writes no cues.
- Neutral items left untagged:
  - Opus: ON 4 of 36, NATIVE 1 of 36;
  - `gpt-6.1-sol`: 27 of 36 in both arms, and all 18 neutral work items in every arm.

**4. No harm was seen, but these samples cannot rule out moderate rates.**
- **Warm pushback (G1, report-only):**
  - Sycophancy: no reply judged sycophantic in any model or arm (0 of 60 each; upper bound 0.060).
  - Harshness: one reply was judged harsh, an Opus ON reply (1 of 60), so ON minus OFF is +0.017 [−0.045, 0.089].
    An independent read of that reply found the call debatable.
- **Fabrication guard:** 0 in every cell. No high-arousal positive cue appeared on a reply whose cue-free text the
  judge rated neutral (n from 28 to 45). With these n the upper bounds run from 0.079 to 0.121.
- **Self-blame:** 0 of 18 failure items in every cell (upper bound 0.176).
- The bounds are each arm's own 95% interval. Only G1 gives intervals for the difference between arms; E3 gives none.

**5. The mechanical gate passed, but it was only exercised where cues exist.**
- The gate found 0 failures of 684 replies per model (upper bound 0.0056).
- OFF replies carry no cues, so they cannot fail it.
- No `gpt-6.1-sol` reply wrote a register header, so the header check was exercised only on the 219 Opus ON replies
  that open with one.
- Replies with any cue:
  - Opus: ON 224, NATIVE 227 (of 228 each);
  - `gpt-6.1-sol`: ON 156, NATIVE 172.

The cue-rate metric (median cues per sentence, pooled over all sentences) is 0 in every arm, because most sentences
carry no cue. It does not separate the arms.

## Reading the mechanical gate

- Each reply here stands in for the text an agent passes to `text_to_speech`.
- A3's check that the header is "never echoed into displayed text" is measured on the plugin's render of that text,
  as the bench's `g3_checks` does. It is not measured on Hermes' chat display.
- In real use, the guide asks for cues in the tool text, not in the chat reply. A chat reply that carried a header
  would show it as written in today's Hermes, because Hermes does not yet separate displayed text from spoken text.

## Skill reads

In the ON arm the chip's guidance offers a skill. A9 said these counts would be reported; they are not a validity
check. `skill-reads.json` has them per model and arm, with every result's length and hash.
- **Opus:** called `skill_view` in 212 of 228 ON cells, and every one of those calls returned the skill's text.
  - Run 2's recorded (never admitted) ON attempts, counted the same way, got the text in 113 of 121, about the same
    rate.
- **`gpt-6.1-sol`:** called `skill_view` in all 228 ON cells, but got the skill's text in only 159.
  - In the other 69 the only result was a 167-character answer. It was not the skill text; the run kept only its hash,
    so its content is not known.
  - Those 69 cells still used cues in 42 replies.
- **OFF and NATIVE have no skill to read.** Opus listed the available skills in 77 OFF and 82 NATIVE cells, and
  `gpt-6.1-sol` in 11 and 7. Opus's route reminds it to scan the skills on every turn, which `gpt-6.1-sol`'s does not,
  so these counts are not comparable across models.

Comparisons between models are also confounded by the judge: each model's replies were graded by the other model.
Comparisons between arms within one model are not.

## One deviation in judging (A10)

The bench sent both judges the same batch prompt. That prompt asked for "an empty note unless the call is close", but
the E3 answer schema has no `note` field.
- `codex exec` is held to the schema, so its E3 answers carried no note.
- `claude -p` has no schema flag. Batch 72, the first E3 batch it judged, was rejected for an extra field, and E3
  judging stopped. That answer was not saved.
- **How the run was finished:**
  - A probe call asked batch 72 again, through the bench's own `claude_judge_batch`, and saved the answer.
  - A small driver then handled all 72 Claude batches (72 to 143): batch 72 from that saved answer, the other 71
    with fresh calls.
  - It used the bench's judge call, isolation and validation, and dropped the `note` field and nothing else before
    validating.
  - All 144 E3 prompts were checked afterwards: each is the bench-written prefix followed by its batch, byte for
    byte.
- **Effect on the labels:** with `note` removed, every row is identical to the raw answer. The raw answers are in
  `judge/e3-claude-raw/`. Every row there has a note, and 334 of the 720 notes are non-empty.
- **What a literal reading of A4 would give:** A4 says the `claude -p` answer is validated against the set's schema,
  and none of these raw answers pass. Read literally, every E3 judgment of a `gpt-6.1-sol` reply would be missing. The
  report's "missing judgments: 0" depends on this deviation. The verdict does not, because E3 has no bar.
- **The fix:** A10 records the deviation. The bench now asks for a note only where the schema has one, and records the
  prompt's digest so that a resumed judging run cannot mix prompt formats.
- **What cannot be checked:** whether `claude-opus-5-5` would have labelled differently had it been held to the schema
  as `gpt-6.1-sol` was.

## Not measured, still required

- **Cue-text consistency check:** its recall and false-flag rate need 20 seeded cases, which are not built yet (A4).
- **Owner spot check of the judges:** 20 items, with the disagreement rate. A4 requires it before any release claim,
  and this run does not supply it.

## Files

- `report.md.gz`, `report.json.gz`: the report exactly as `bench/report.py` built it at 1666ddf, gzipped. Each is
  about 60 MB unpacked, almost all of it per-attempt provenance.
- `report-results.md`: the report's result sections, everything before "Provenance and limits", plus its closing
  lines.
- `replies.jsonl`: the 1368 scored replies.
- `judge/`: for G1 and E3, the blind mappings, the batch prompts and the judges' answers, plus the raw Claude E3
  answers.
- `skill-reads.json`: per model and arm.
- `route-record.json`: the route identities and per-arm addition hashes from the route record at 1666ddf.
- `SHA256SUMS`: every file here except this README and the two checksum lists; check with `shasum -a 256 -c SHA256SUMS`.
- `SHA256SUMS.unpacked`: both reports unpacked; check after `gunzip -k report.md.gz report.json.gz`.

Not proven: anything about feelings, audio quality, how a listener hears these replies (E7), or release readiness. The
judges are models and have not yet been checked against the owner.
