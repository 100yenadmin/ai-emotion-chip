# Scope-A3 run 1 (2026-10-23): stopped at the pilot, kept as a record

Run 1 of scope A3 was stopped as an infrastructure failure (`EVAL-PREREG.md`, amendment A7). It scored no reply and is
not a result. It is kept so that the failure, and what the run saw before it, can be checked.

## What ran

- The A4 harness at the merge of #20, with A5 and A6, the owner-approved A4b from that commit's route record, and the
  `gpt-6.1-sol` login in `borrowed-read-only` mode.
- `claude-opus-5-5` only:
  - the clean reference (one OFF cell on the first G1 held-out item), admitted;
  - the pilot's 24 cells, all admitted, one of them on its rerun.
- The Opus full phase had started 7 cells when the run was stopped. A phase records its attempts only when it ends,
  so those cells were never recorded or published. `gpt-6.1-sol` did not start.

## Why it stopped

Pilot cell E3 `e3-h01`, NATIVE, run 1, failed check 2 on its first attempt. Its Hermes-side system text `S` was not
the arm's expected `S`. A local diff of that cell's stored prompt against its valid rerun found one extra line:
Hermes' Python toolchain line (`tools/env_probe.py`).

Hermes adds that line only when a background probe finishes within 10 seconds of the prompt build. Inside the bench
sandbox it usually does not, so the golden homes and most cells have no line, but now and then a cell does. That does
not depend on the arm. Under A4, a second check 1–3 failure stops a model. Opus already had one, so at the pilot's rate
(1 in 25 cells) the full phase would very likely have stopped both models. A7 turns the probe off in every home.

## Files

- `attempts.json`: every recorded attempt (reference and pilot), with its checks, first-call prompt tokens, phase
  median, captures (hashes, lengths, tool-schema hashes and marker labels only) and admission. Reply text is replaced
  by its sha256 and length, because these replies are not scored.
- `failures.jsonl`: the bench's failure log for the one invalid attempt.

Not proven: anything about the plugin's effect. The pilot checks captures, the output channel and validity only.
