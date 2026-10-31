# E7 session e7-eleven-1 (2026-10-23): void, kept as a record

Session `e7-eleven-1` is **void** under the stop rules in `EVAL-PREREG.md`, as an infrastructure failure (amendment
A12). It was never scored. Session `e7-eleven-2` replaces it.

## What ran

- Tag `e7-eleven-1`, commit b2d31a0. ElevenLabs `eleven_v4`, voice Ava, `claude-opus-5-5` through eva's provider, as
  set out in A11.
- Generation: 67 of 67 cells completed on the first attempt, with no retries. The chip arm had 45 (20 comparison a,
  20 comparison b, 2 catch, 3 mini-sessions), and the eva arm 22 (20 comparison b, 2 catch).
- After generation, every recorded hash was recomputed and matched (`session-checks.json`). The hashes cover the Hermes
  source tree, the provider copy and its source, the probe plugin, the archived chip plugin, eva's snapshotted inputs and
  the `sag` program. Every source clip was MP3: chip 71, eva 22, so 93 in all. Each of the 4 catch pairs plays one
  source clip on both sides, so the bundle held 97 clips.
- `session-checks.json` was written on 2026-10-23 by the operator's evidence script, to a file outside the session
  folder, so it survived. It holds counts and booleans only. Its `fish_audio` field records the fish-audio plugin
  version, because every Hermes test home installs it (the chip's Fish backend wraps it), including on ElevenLabs runs.
- Build: 40 real pairs, 4 catch pairs and 3 three-turn mini-sessions, 97 clips. The key's sha256,
  `a896e7f7a4b2cdb4b400605dae591bdd9e46831f12ebe637580b3e287e9eb23f`, was posted on issue #10 before any listening.

## Why it is void

- 2026-10-24: a disk cleanup on the run machine treated the idle session folder as stale scratch and moved it to the
  system Trash.
- 2026-10-30: the Trash was emptied.
- 2026-10-31: the loss was found, when the folder was needed.

The folder held the only copy of the key, the manifest and the source clips. The run machine's copy of the blinded
bundle has also gone. The page published for listening is hosted under an account the run machine can no longer reach,
so whether it was rated or locked is not known here.

The order and sides were drawn from the operating system's random source, so the key cannot be rebuilt.

The following are the operator's statements, not things the checks show:
- the key was never opened;
- no source clip was played;
- only cell statuses and counts were read from the manifest;
- the run machine has no backups or snapshots.

## What it does not show

These checks show only that the session was generated as preregistered. They say nothing about how the chip sounds.
No rating from this session is scored or reported.
