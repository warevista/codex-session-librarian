# Codex Session Librarian

Find, reopen, clone, and share native Codex conversations, and transfer native
Claude Code conversations, from your terminal or coding agent. CSL works with
the actual JSONL history, not just a summary. See the maintained
[CLI coverage matrix](docs/PROVIDER_MATRIX.md) for command-by-command scope.

## Install

Requirements: Linux and Python 3.11+. Opening a session also requires its native
CLI (Codex or Claude Code) installed with your own authentication. Team
sharing also needs [GitHub CLI](https://cli.github.com/) (`gh`). Local history
and ZIP transfers use only the Python standard library.

```bash
git clone https://github.com/warevista/codex-session-librarian.git
cd codex-session-librarian
./install.sh
```

Make sure `~/.local/bin` is on `PATH`. No virtualenv or pip install is needed.
An optional prefix is supported: `./install.sh /usr/local`.

Update this checkout with `git pull --ff-only`. The installed launcher follows
the checkout, so no reinstall is needed.

## Share With The Team

Connect once on each computer, using that person's own GitHub account:

```bash
gh auth login
csl team connect warevista/codex-session-exchange
```

The exchange is **private** and separate from this public code repository.
Accept the collaborator invitation to gain access. Packages are GitHub Release
assets; nobody needs to clone the exchange, commit ZIPs, manage branches, or
make a PR to share a conversation.

Send:

```bash
csl share SESSION_UUID --title "Workspace publishing discussion"
```

CSL exports a portable snapshot and prints its share ID and private GitHub link.
You can share several session UUIDs in one command. Without a selector, a
numbered local picker is available in an interactive terminal.

Receive:

```bash
csl inbox
csl receive csl-SHARE_ID --cwd ~/Documents/projects --open
```

Use the actual `csl-xxxxxxxxxxxx` ID printed by `share` or `inbox`.
`receive` downloads, validates, imports with new UUIDs and opens the native CLI.
The directory must exist locally. Omit `--open` to get the resume command
without launching Codex. Omit the share ID for an interactive inbox picker.

Put CSL options before `--open`. Everything after it is passed literally to
the session's CLI (also supported by `csl import`):

```bash
csl receive csl-SHARE_ID --cwd ~/Documents/projects --new --open --model MODEL
```

For an explicitly unsandboxed launch, append
`--dangerously-bypass-approvals-and-sandbox` after `--open`. This disables Codex
approvals and sandboxing; CSL never adds it automatically. For Claude Code,
the corresponding explicit bypass flag is `--dangerously-skip-permissions`;
CSL does not translate flags between CLIs.

### Claude Code

```bash
csl sessions --provider claude --query "backend"
csl share CLAUDE_UUID --title "Claude handoff"
csl export CLAUDE_UUID --to ~/Downloads
```

`share` and `export` also work without `--provider`: CSL searches both local
histories and detects the CLI from the unique match. If both CLIs match, it
stops and asks for `--provider codex` or `--provider claude`; within one CLI,
use a more specific UUID or file path. Explicit `--provider` remains an optional
lookup filter. File paths are detected from transcript contents, not filenames.

Receive/import auto-detects the CLI from the package. All CSL options go before
`--open`, then native CLI options follow:

```bash
csl receive csl-SHARE_ID --cwd ~/work/project --open --model haiku
csl receive csl-SHARE_ID --cwd ~/work/project --provider claude --open --dangerously-skip-permissions
```

Use your real share ID. `--provider` on receive/import asserts the source CLI,
not conversion; `--target` explicitly converts it (experimental multimodal profile). Claude
exports/shares require updated CSL on both sides. Search/recall/cut commands
remain Codex-only; basic Claude discovery uses `sessions`. Inspect the full
matrix with `csl capabilities` or `csl capabilities --json`.

### Conversion And Continuity

```bash
csl convert SESSION_UUID --to claude --cwd ~/work/project
csl receive csl-SHARE_ID --target codex --cwd ~/work/project --open
csl convert SESSION_UUID --to canonical --out ~/Downloads/conversation.json
csl doctor SESSION_UUID --native --json
```

Conversion preserves supported text/tools, references, embedded images and PDFs
through a versioned canonical format;
it rejects unsupported assets and opaque context instead of silently dropping
them. Native imports print structural/context preflight evidence. `doctor --native`
checks request reconstruction in isolated homes against localhost, without model
generation or real credentials. It is not a claim of successful live recall or
universal context fit. See [conversion and continuity](docs/CONVERSION.md).

Image/PDF conversion requires the optional isolated decoder:

```bash
./install-media.sh
```

PNG, JPEG, static GIF and WebP are supported. PDFs retain their original bytes;
Codex receives extracted text and rendered pages, including scanned pages.
External paths/URLs/file IDs are not collected. For untrusted media, require
Linux bubblewrap with `CSL_MEDIA_SANDBOX=required`; see the decoder limits in
[conversion](docs/CONVERSION.md).

`csl verify-live --allow-paid` separately tests synthetic continuations with
explicit budgets and isolated histories. It is not run by ordinary transfers or
`doctor`. See [setup, results and limits](docs/LIVE_VERIFICATION.md).

Receiving the same snapshot again reuses your copy. Add `--new` to import
another independent copy. Each continuation stays independent: this is a
snapshot handoff, not live synchronization or automatic merging. Sharing a
received session again records which snapshot it continues.

Imports adapt modern visible-message events for Codex's standalone history UI,
without changing the model's message records. If an older import shows only
timings or completion events, update CSL and receive it again with `--new`.
The sender does not need to export the same package again.
This applies to the display-only issue. Packages with inconsistent compaction
history/metadata are rejected: the sender must re-export the original session
with updated CSL, rather than re-share a damaged imported copy.

`csl inbox --json` provides structured metadata for agents without downloading
the conversations. `csl team status` checks your connection.

See [team sharing](docs/SHARING.md) and [team installation](docs/TEAM_INSTALL.md).

## Share A ZIP Without GitHub

```bash
csl export SESSION_UUID --to ~/Downloads
csl export SESSION_UUID ANOTHER_UUID --to ~/Downloads/team-handoff.csl.zip
csl import ~/Downloads/team-handoff.csl.zip --list
csl import ~/Downloads/team-handoff.csl.zip --cwd ~/Documents/projects --open
```

Export accepts UUIDs, unambiguous prefixes, indexed names, or JSONL paths.
Archived and newly created sessions can be found by UUID without reindexing.
Metadata-only histories are rejected because they do not contain messages.
`--list` validates without importing.

Default export/share removes encrypted reasoning and opaque encrypted state
for portability. `--raw` retains it, but may fail under a different account.
Messages and tool history are preserved; originals are never changed.
See [portable transfers](docs/TRANSFERS.md).

## Find And Reopen A Conversation

```bash
csl index --prune
csl recent --limit 20
csl recall "LinkedIn API token developer app" --anchor linkedin
csl search vitral gtm
csl show SESSION_UUID --messages 20 --include-assistant
codex resume --all SESSION_UUID
```

Search uses a local SQLite index and lexical ranking, not embeddings.
`recall` helps with fuzzy remembered discussions; `find-work`, `patches`,
and `pinpoint` help locate actual implementation work. Match snippets must
be checked before claiming that a session is the right one.

For an interactive local browser, use `csl cockpit`.
For durable annotations:

```bash
csl annotate SESSION_UUID --summary "Publishing workspaces and visitor conversations." --tags "publishing, slides"
```

The default index covers active local sessions. Archived files can be indexed
separately with `index --sessions-dir`; export/share also locate them directly.

## Clone Or Trim

```bash
csl native-clone SESSION_UUID --name "Independent continuation"
csl native-sanitize SESSION_UUID --name "Portable local copy"
csl timeline SESSION_UUID --width 180
csl native-cut SESSION_UUID --before-text "Later topic starts here" --name "Earlier discussion"
```

These commands create new native sessions. `native-sanitize` removes encrypted
reasoning. For a Markdown context handoff instead:

```bash
csl cut SESSION_UUID --to 12 --name earlier-discussion
```

## What Moves And What Does Not

- Packages contain conversation messages, tool calls/results and provenance.
- A fork may reference parent history absent from its package. CSL warns about
  this and imports only the included records; it cannot reconstruct missing ancestors.
- Project repositories, installed tools, auth files and external attachments are
  not collected. Historical paths in message text still refer to the sender.
- Each recipient uses their own Codex and GitHub authentication.
- Conversation/tool text may contain secrets; portable cleanup is **not** secret
  redaction. Review before sharing. No real session packages belong in this public repo.
- Checksums detect corruption, not sender authenticity.
- Imported history is context, not authorization to execute unfinished tasks.

Native replay depends on Codex's current JSONL format and the recipient's
available model/provider. Metadata without messages cannot restore history.
A successful import alone does not prove a successful model turn.

## Storage And Development

CSL reads `$CODEX_HOME` (default `~/.codex`):

| Location | Purpose |
|---|---|
| `sessions/`, `archived_sessions/` | Native local histories |
| `session_index.jsonl` | Codex session names |
| `session_librarian.sqlite` | Search index and annotations |
| `session_cuts/` | Markdown handoffs |
| `csl-sharing/` | Team repository setting and local receive receipts |

Claude transcripts are read/written under `$CLAUDE_CONFIG_DIR/projects`
(default `~/.claude/projects`). Shared inbox settings/receipts remain under
`$CODEX_HOME/csl-sharing` for both CLIs; neither CLI's credential files are copied.

No extra Python dependencies:

```bash
python3 -m unittest discover -s tests -v
```

With Codex installed, include the offline native history-visibility regression:

```bash
CSL_RUN_CODEX_NATIVE_TESTS=1 python3 -m unittest discover -s tests -v
```

This uses an isolated temporary Codex home and makes no model requests.
Add `CSL_RUN_CLAUDE_NATIVE_TESTS=1` to include the isolated Claude loader check
against localhost, which stops before generation and uses no real API key.

## Documentation

- [CLI/provider command and content coverage](docs/PROVIDER_MATRIX.md)
- [Canonical conversion and continuity checks](docs/CONVERSION.md)
- [Team installation and contributions](docs/TEAM_INSTALL.md)
- [Private GitHub sharing](docs/SHARING.md)
- [Portable ZIP transfers](docs/TRANSFERS.md)
- [Command reference](docs/COMMANDS.md)
- [Session workflows](docs/SESSION_WORKFLOWS.md)
- [Evolution](docs/EVOLUTION.md)
