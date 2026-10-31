# E7 session e7-eleven-1 (2026-10-23): void, kept as a record

Session `e7-eleven-1` is **void** under the stop rules in `EVAL-PREREG.md`, as an infrastructure failure (amendment
A12). It was never scored. It is replaced by session `e7-eleven-2`.

## What ran

- Tag `e7-eleven-1`, commit b2d31a0. ElevenLabs `eleven_v4`, voice Ava, `claude-opus-5-5` through eva's provider, as
  set out in A11.
- Generation: 67 of 67 cells completed on the first attempt, with no retries (chip arm 45, eva arm 22).
- After generation, every recorded hash was recomputed and matched: the Hermes source tree, the provider copy and its
  source, the probe plugin, the archived chip plugin, eva's snapshotted inputs and the `sag` program
  (`session-checks.json`). Every clip was MP3 (chip 71, eva 22).
- Build: 40 real pairs, 4 catch pairs and 3 three-turn mini-sessions, 97 clips. The key's sha256,
  `a896e7f7a4b2cdb4b400605dae591bdd9e46831f12ebe637580b3e287e9eb23f`, was posted on issue #10 before any listening.

## Why it is void

On 2026-10-24 a disk cleanup on the run machine treated the idle session folder as stale scratch and moved it to the
system Trash. The Trash was emptied on 2026-10-30. The folder held the only copy of the key, the manifest and the
source clips, and the machine has no backups or snapshots to restore them from. The key was never opened. The order and
sides were drawn from the operating system's random source, so the key cannot be rebuilt, and nothing else records which
clip came from which arm.

## What it does not show

These checks show only that the session was generated as preregistered. They say nothing about how the chip sounds.
No rating from this session is scored or reported.
