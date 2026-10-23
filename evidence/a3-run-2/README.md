# Scope-A3 run 2 (2026-10-23): stopped in the Opus full phase, kept as a record

Run 2 of scope A3 was stopped (`EVAL-PREREG.md`, amendment A9). It scored no reply and is not a result. It is kept so
that the stop, and what the run saw before it, can be checked.

## What ran

- The A4 harness at the merge of #22 (A8), with the owner-approved A4b from that commit's route record and the
  `gpt-6.1-sol` login in `borrowed-read-only` mode.
- `claude-opus-5-5`:
  - the clean reference, admitted;
  - the pilot's 24 cells, all admitted on their first attempt;
  - 364 of the full phase's 684 cells. A phase records its attempts only when it ends, so these are recorded as
    aborted and none is admitted or scored. 362 passed checks 1, 2, 3 and 5 on their first attempt. Check 4 (prompt
    tokens) is computed only when a phase ends, so it is absent.
- `gpt-6.1-sol`:
  - the clean reference, admitted;
  - the pilot's 24 cells, all admitted on their first attempt;
  - about 130 full-phase cells, stopped by the operator once run 2 could no longer give a result. They were not
    recorded.

## Why it stopped

- **A sign-in failure.** The 365th Opus full cell got Hermes' sign-in rejection: the Claude sign-in token the cells
  read had expired. The harness stops a model at the first credential failure, as A4 requires, so Opus ended there. The
  token had been renewed by another process a few minutes later. Without Opus the run could not give a verdict.
- **A write check 3 cannot reproduce.** E3 `e3-h07`, NATIVE, run 1 failed check 3 on both attempts. After the user's
  criticism ("You told me the API paginated by offset…"), the model used Hermes' `skill_manage` to create a skill
  recording the lesson. Hermes offers that tool in interactive sessions so the agent can learn for later ones. Check 3
  rejects a write, so the cell could never be admitted, and the other two NATIVE runs of that item had not been reached.
  It happened in 1 of 365 Opus cells and in none of the `gpt-6.1-sol` cells that ran. A9 runs every cell as a Hermes
  one-shot session, which does not offer that tool.

## Files

- `attempts.jsonl`: one line per recorded attempt (Opus reference, pilot and the 364 aborted full attempts; `gpt-6.1-sol`
  reference and pilot), with its checks, first-call prompt tokens, captures (hashes, lengths, tool-schema hashes and
  marker labels only), and admission. Reply text is replaced by its sha256 and length, because these replies are not
  scored.

Not proven: anything about the plugin's effect. The attempts check captures, the output channel and validity only.
