# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately: [open a security advisory](https://github.com/100yenadmin/ai-emotion-chip/security/advisories/new).
Don't open a public issue for a security problem.

We aim to acknowledge reports within 3 business days and to share a decision or fix plan within 14 days.

## What counts as a vulnerability here

- Exposure of an ElevenLabs or Fish Audio API key (logs, rendered text, delivery log, errors).
- Text or audio sent to a provider or endpoint the user did not configure.
- The delivery log or plugin state readable outside the profile's `HERMES_HOME`.

## Out of scope

- Problems in Hermes Agent core: report those to [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/security).
- Fish Audio's own plugin (`fish-audio`): report to https://github.com/100yenadmin/fish-audio-hermes.
- Vendor-side behaviour of Fish Audio or ElevenLabs models.

## Supported versions

Fixes go into the latest release.
