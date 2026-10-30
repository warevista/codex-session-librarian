# CLI Coverage And Limits

CSL supports **native Codex -> Codex and Claude Code -> Claude Code handoffs**.
"Provider" in CSL means the transcript/CLI adapter (`codex` or `claude`), not
the model vendor, API endpoint, subscription or billing account.

This matrix describes shipped CSL commands, not capabilities that exist only
in a separate portability experiment. `supported` is bounded by the limits below;
it does not mean every historical transcript or future CLI version is compatible.
`experimental` depends on evolving native formats. `not-integrated` is unavailable
in this public tool, even if a related laboratory experiment has passed.

## Command Matrix

The table is mirrored by `csl capabilities` / `csl capabilities --json`.
Tests require every registered command to appear and this table to match the
registry. Update implementation, tests and this document in the same change.

| Command | Codex | Claude Code |
| --- | --- | --- |
| `annotate` | supported | not-integrated |
| `capabilities` | supported | supported |
| `cockpit` | supported | not-integrated |
| `cut` | supported | not-integrated |
| `export` | supported | supported |
| `find-work` | supported | not-integrated |
| `import` | supported | supported |
| `inbox` | supported | supported |
| `index` | supported | not-integrated |
| `native-clone` | experimental | not-integrated |
| `native-cut` | experimental | not-integrated |
| `native-sanitize` | experimental | not-integrated |
| `patches` | supported | not-integrated |
| `pinpoint` | supported | not-integrated |
| `queue` | supported | not-integrated |
| `recall` | supported | not-integrated |
| `receive` | supported | supported |
| `recent` | supported | not-integrated |
| `related` | supported | not-integrated |
| `search` | supported | not-integrated |
| `sessions` | supported | supported |
| `share` | supported | supported |
| `show` | supported | not-integrated |
| `stats` | supported | not-integrated |
| `summarize` | supported | not-integrated |
| `team` | supported | supported |
| `timeline` | supported | not-integrated |
| `why` | supported | not-integrated |

`sessions --provider claude` scans main project transcripts and filters UUID/title
with `--query`; it is not full-history semantic search. Codex listing uses its
existing index, refreshed with `csl index`. Inbox and team commands manage a
shared exchange, not the CLI's own built-in picker. `summarize` prepares a local
context package; CSL does not buy model inference or generate a cloud summary.

## Commands For Each CLI

Source commands default to Codex. Select Claude explicitly for discovery/export/share:

```bash
csl sessions --provider codex
csl export CODEX_UUID --to ~/Downloads
csl share CODEX_UUID --title "Codex handoff"

csl sessions --provider claude --query "backend" --json
csl export CLAUDE_UUID --provider claude --to ~/Downloads
csl share CLAUDE_UUID --provider claude --title "Claude handoff"
```

Both source adapters accept a UUID, unambiguous title or JSONL path (Codex title
lookup requires its index). Claude UUID lookup excludes nested subagent files.
Claude export without a selector requires one explicitly; Codex retains its picker.

The receiver auto-detects the native CLI from the validated package. An optional
`--provider` asserts the expected CLI; it does **not** request conversion:

```bash
csl import ~/Downloads/handoff.csl.zip --cwd ~/work/project --open
csl receive csl-xxxxxxxxxxxx --cwd ~/work/project --open
csl receive csl-xxxxxxxxxxxx --provider claude --cwd ~/work/project --new --open --model haiku
```

Everything after `--open` is a literal CLI argument. CSL options must come first.
No shell evaluation or automatic translation of provider flags occurs:

| Purpose | Codex argument after `--open` | Claude Code argument after `--open` |
| --- | --- | --- |
| Model | `--model MODEL` | `--model MODEL` |
| Additional working directory | `--add-dir PATH` | `--add-dir PATH` |
| Approval/permission policy | `--ask-for-approval POLICY` | `--permission-mode MODE` |
| Explicit bypass | `--dangerously-bypass-approvals-and-sandbox` | `--dangerously-skip-permissions` |

These are different controls, not equivalent security guarantees. Neither bypass
is enabled by CSL. Flag validity and model availability belong to the installed
CLI. Forwarding a prompt or `--print` can explicitly start inference; plain
`--open` does not submit a prompt. Claude launches with process CWD set to
`--cwd`; Codex uses `-C`. Neither downloads the project repository.

## Transfer And Content Matrix

| Area | Codex | Claude Code |
| --- | --- | --- |
| Native destination | `$CODEX_HOME/sessions` | `$CLAUDE_CONFIG_DIR/projects` |
| Default home | `~/.codex` | `~/.claude` |
| Identity | Fresh session UUID | Fresh session and graph-node UUIDs |
| Historical text/tool results | Preserved; modern UI events projected when needed | Preserved; graph references rebound, tools not rerun |
| Portable cleanup | Encrypted reasoning/state removed with aligned checkpoint metadata | Thinking/redacted-thinking removed; graph node retained with an omission marker if empty |
| System prompt snapshots | Native Codex replay behavior; local settings not copied | Sender's `system-prompt` snapshots omitted on import, including raw import |
| Compaction | Structured checkpoints retained; inconsistent metadata rejected | Native records/summary retained; summary may start without older ancestors; graph checked |
| Parent history / branches | Missing fork ancestry warned, not invented | Branch graph retained; missing ordinary parents, conflicts and cycles rejected; no branch selector |
| External attachments / repository / credentials | Not collected | Not collected |
| Embedded content | Existing JSONL content preserved; no fetching | Existing message blocks preserved; no fetching |
| Raw mode | Ciphertext may fail under a different account/model | Signed thinking may fail under a different account/model |
| Cross-CLI conversion | Codex -> Claude: not integrated | Claude -> Codex: not integrated |

Mixed-provider bundles are rejected; send each CLI's sessions separately. Codex
bundles remain format v1 for compatibility; Claude bundles/releases use v2 so
older CSL installations reject/skip them instead of importing as Codex. Both
sender and receiver must update CSL for Claude sharing. Repeat receives reuse the
latest local copy by provider; `--new` creates another independent copy.
Claude transcripts currently have a 64 MiB per-session adapter limit because
graph validation is in memory; Codex retains its 2 GiB per-session limit. ZIP
validation still applies the shared 4 GiB uncompressed total limit.

## Authentication And Backend Providers

CSL does not log into either service, copy credentials, select accounts, install
cloud resources or translate model names. Each CLI uses the recipient's own
authentication and configuration. Export/import without `--open` does not need
either CLI binary or a model login.

| Backend/account mode | What CSL does | Verification scope |
| --- | --- | --- |
| Codex ChatGPT login or API/custom endpoint | Transfers native Codex records, leaves local auth/config alone | No guarantee that a source model is available to the recipient |
| Claude subscription or Anthropic API | Transfers native Claude records, leaves local auth/config alone | Native loader tested against localhost with a fake key; no paid-provider claim |
| Claude Bedrock / Vertex / Foundry | Same native adapter; local CLI owns routing/auth | Backend-specific continuation not tested by CSL |
| Other CLIs | No adapter | Not supported |

Private GitHub access and checksums are not secret redaction. Conversation text
can contain secrets. No transcript, real ZIP, auth file or provider credential
belongs in this public repository.

## Verification And Maintenance

Native checks are opt-in, isolated and model-free. The Codex check reads loaded
turns through app-server; the Claude check captures a reconstructed message request
on localhost and stops before generation. They establish loader behavior, not
successful paid inference, universal context fit, or full recovery of hidden state.
This integration was checked with Codex 0.166.0 and Claude Code 2.1.308 on Linux.

```bash
python3 -m unittest discover -s tests -q
CSL_RUN_CODEX_NATIVE_TESTS=1 CSL_RUN_CLAUDE_NATIVE_TESTS=1 python3 -m unittest discover -s tests -q
csl capabilities --json
```

For any new command/provider route: update `csl_capabilities.py`, this matrix and
synthetic tests. Add native isolated evidence before claiming CLI compatibility;
add an explicitly authorized model continuation before claiming live recall.
Never test with private conversations in the public repository or pollute a
personal CLI history with synthetic sessions.

Native behavior references: [Claude session management](https://code.claude.com/docs/en/sessions)
and [Claude CLI flags](https://code.claude.com/docs/en/cli-reference). Local CLI
`--help` is authoritative for the installed version's argument set.
