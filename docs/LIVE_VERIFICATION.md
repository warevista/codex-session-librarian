# Synthetic Live Verification

`doctor --native` measures request reconstruction against localhost without
inference. `verify-live` separately measures actual model recall after native
continuation. Neither certifies every private session, backend or context size.
The live runner accepts only generated synthetic cases, not session selectors.

## Setup

Install both native CLIs and choose models available to your accounts. For
Codex, authenticate a dedicated test home yourself; CSL never copies your login:

```bash
CODEX_HOME="$HOME/.local/share/csl/live-codex" codex login --device-auth
```

Normal `~/.codex` is rejected. Tests create sessions in the dedicated home, so
do not point it at any other personal/custom production home. Codex requires
ChatGPT login here; no API-key fallback is used. Claude currently requires an
`ANTHROPIC_API_KEY` supplied securely in the environment. Its disposable home
uses `--bare`; subscription, Bedrock, Vertex and Foundry are not tested routes.
Each subprocess receives only its own credential family. Do not put keys in
command arguments, reports, transcripts or Git.

For media cases, install the optional decoder and synthetic fixture extras:

```bash
./install-media.sh
~/.local/share/csl/media-venv/bin/python -m pip install -r media-test-requirements.txt
csl verify-live --allow-paid --targets both --cases all \
  --codex-home "$HOME/.local/share/csl/live-codex" \
  --claude-budget-usd 0.05 --total-budget-usd 0.60 --max-calls 24 \
  --out "$HOME/.local/share/csl/live-results/run-001"
```

Choose `--codex-model` / `--claude-model` if the defaults are unavailable.
`--targets codex|claude|both` selects destinations; `--cases text|media|all`
defaults to text. Media also needs `CSL_MEDIA_PYTHON` if installed elsewhere.
For untrusted media outside this synthetic suite, require bubblewrap as described
in [conversion](CONVERSION.md).

## Guards And Output

- Explicit `--allow-paid`; no paid checks in normal transfers, doctor or CI.
- Maximum 24 CLI attempts; no harness retries; stop after two consecutive
  failures for a destination. Each subprocess has a 90-second timeout.
- Claude reservation is debited before launch. Per-call budget is at most
  USD 0.10 and total at most USD 2.00. CLI-reported spend is also checked before
  later launches. These are not a billing guarantee: a first request can exceed
  a CLI threshold. Underlying provider/SDK retry behavior is not controlled fully.
- Codex consumes the ChatGPT plan, not the Claude dollar reservation; the report
  records returned token usage, not an invented dollar price.
- Tools are disabled, tool attempts fail the check, historical tools never run.
- Output directory must be new; it is private `0700`, report `0600`. Reports
  contain synthetic answers, field-level failures, versions, usage, reservations,
  source immutability and original asset-retention checks. New runs also record
  configuration and adapter SHA-256 fingerprints.
- Raw provider streams are not saved by the runner. Reports redact known
  credentials. Native CLIs still write synthetic histories in isolated homes;
  keep all run artifacts out of public Git.

## Measured Results

2026-10-31, Linux, Codex CLI 0.166.0 with ChatGPT login (`gpt-6.1-sol`), Claude
Code 2.1.308 with Anthropic API (`claude-haiku-5-5`):

| Check group | Passed |
| --- | --- |
| Original synthetic native histories | 2/2 |
| Text: Codex/Codex, Codex/Claude, Claude/Codex, Claude/Claude | 4/4 |
| Media: the same four routes | 4/4 |
| Checkpoint, rollback, branch and explicit UNKNOWN tool repair, both destinations | 8/8 |
| Second-generation media roundtrips, including both cross-CLI return paths | 4/4 |
| Total actual model continuations | 22/22 |

Tests require corrected decisions rather than obsolete values, tool-only facts,
parallel results, failed-read outcomes and historical references. Media questions
require image-only markers/colors, a two-page invoice and an image-only scanned
PDF; expected answers are not embedded in the question. Eight post-live media
histories retained the original embedded hashes after reconversion. Zero tools
executed. The same histories were revalidated with required bubblewrap afterward.

Claude CLI-reported estimate: USD 0.003920475, not an invoice; USD 0.55 reserved
against a USD 0.60 cap. Codex returned 105,099 input and 505 output tokens, zero
cached input reported. Two earlier configuration attempts failed before provider
requests; one fixture preflight also failed before inference. No paid retry loop
was used. A credential-value audit of 29 private artifact files found no matches.
Private histories/reports and credentials are not published with these results.

After final safety/reporting changes, 172 offline tests passed with optional
media tests, both installed native CLIs and required bubblewrap. Paid calls were
not repeated for reporting-only changes; these results are evidence for the
tested behaviors, not a claim that every final byte ran against a paid provider.

## Remaining Limits

No opaque compaction decryption, missing-parent reconstruction, external asset
fetching, audio/video/SVG, animated image conversion or universal model/context
compatibility. Uncertain checkpoints fail closed; visible recovery and unfinished
tool repair require explicit choices. OS namespace isolation is Linux-specific;
process/resource limits alone are not an equivalent security boundary. Model
availability, account routing and CLI formats can change; rerun the relevant
bounded checks when upgrading.
