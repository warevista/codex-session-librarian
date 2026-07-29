# Codex Session Librarian

LLM-native local CLI for indexing, searching, summarizing, and trimming Codex conversation/session files.

It reads `~/.codex/sessions/**/*.jsonl`, stores compact metadata in `~/.codex/session_librarian.sqlite`, and avoids re-parsing unchanged session files.

It is designed primarily for LLM-assisted operators: the workflows optimize for an agent or assistant that needs to recall, summarize, trim, reopen, and compare past Codex sessions quickly.

## Architecture

The tool uses four local storage layers:

- Raw Codex sessions in `~/.codex/sessions/**/*.jsonl`
- Native Codex session catalog in `~/.codex/session_index.jsonl`
- Librarian index in `~/.codex/session_librarian.sqlite`
- Safe cut packages in `~/.codex/session_cuts/`

Operational flow:

1. `csl index` parses raw session JSONL files.
2. It extracts compact metadata and stores it in SQLite.
3. Search and recall commands read from SQLite, not from the raw archive.
4. `cut` writes a Markdown context package for a fresh session.
5. `native-cut` writes a synthetic session JSONL and registers it into Codex's session catalog.

## What It Covers

- Fast local search over Codex sessions and forks
- Durable summaries and tags for important conversations
- Safe cut packages for restarting from an earlier point
- Experimental native trimmed clones that may open with `codex resume`
- LLM-friendly workflows for recalling and reusing prior session context

## Data Model

Each indexed session stores:

- session id
- thread name
- started/updated timestamps
- cwd and inferred project
- heuristic summary and tags
- durable LLM summary and tags
- first and last user message
- aggregated user messages
- aggregated assistant messages
- extracted commands/tool calls
- extracted file/path references
- fork-hint flag
- file size, mtime, parse counters, and index timestamp

Heuristic metadata is derived automatically from the raw session. Durable metadata is added explicitly with `csl annotate`.

## Reliability Model

There are three operational levels:

- Stable: `index`, `search`, `recall`, `recent`, `show`, `related`, `summarize`, `annotate`, `queue`, `stats`
- Safe but indirect: `cut`
- Experimental and format-dependent: `native-cut`

`cut` is safe because it does not modify Codex's native session files.

`native-cut` is experimental because it depends on Codex's current local JSONL/session-index behavior. It rewrites session metadata, writes a new JSONL file, and appends a new entry to `~/.codex/session_index.jsonl` so the clone may appear in `codex resume`.

## Quick Start

Install the local launcher on this machine:

```bash
./install.sh
```

Or install to a custom prefix:

```bash
./install.sh /usr/local
```

Refresh the index:

```bash
csl index --prune
```

Search by topic:

```bash
csl search "role charter"
csl search vitral gtm
csl search vitral --project vitral
```

Recall a conversation from fuzzy memory:

```bash
csl recall "LinkedIn API token developer app connections" --anchor linkedin
```

Inspect one session:

```bash
csl show 019cf844
```

Create a safe cut:

```bash
csl timeline 019cf844 --width 180
csl cut 019cf844 --to 14 --name vitral-gtm-before-bilingual-job-docs
```

Create an experimental native resumable clone:

```bash
csl native-cut 019cf844 \
  --before-text "Yes, provide those 3 documents/artifacts..." \
  --name "Vitral GTM cut before bilingual job docs"
```

Create an experimental full native clone:

```bash
csl native-clone 019cf844 --name "Vitral GTM full clone"
```

## Docs

- [Commands](docs/COMMANDS.md)
- [Session Workflows](docs/SESSION_WORKFLOWS.md)
- [Evolution](docs/EVOLUTION.md)

## Notes

- No external Python packages are required.
- `csl` is a thin wrapper around `codex_sessions.py`.
- `install.sh` creates a symlinked `csl` launcher under `~/.local/bin` by default.
- This tool is intentionally LLM-native: it is built for an assistant/operator workflow, not for general chat-history browsing.
- Durable summaries are saved into the local SQLite index and improve future search quality.
- `cut` is the safe trim workflow. It creates a Markdown context package and leaves Codex's native session files untouched.
- `native-cut` is experimental. It writes a synthetic session JSONL plus a `session_index.jsonl` entry so the clone may be opened with `codex resume`.
- `native-clone` is experimental. It copies a full selected JSONL session, rewrites native session metadata with a new id, and registers the clone in `session_index.jsonl`.
- Cross-account clones on the same PC need extra care when a source JSONL contains multiple inherited `session_meta` rows. See `docs/SESSION_WORKFLOWS.md` section "Clone Between Local Codex Accounts".
- Native Codex fork relationships are exposed in JSONL via `forked_from_id`, but Codex does not currently expose an official "trim at turn N" command.
- Search is lexical and weighted, not embedding-based.
- Durable summaries and tags take precedence over heuristic summaries and tags when displaying and searching sessions.

## Common Commands

Build the index:

```bash
./install.sh
csl index --prune
```

Search by topic:

```bash
csl search mongo kernel
csl search "role charter"
csl search gke deployment
csl search vitral --project vitral
```

Pinpoint a fuzzy remembered work session:

```bash
csl pinpoint mongo 8.2 GLIBC_TUNABLES deployment.yaml --repo vitral_backend
```

Recall a fuzzy remembered discussion:

```bash
csl recall "LinkedIn API token developer app connections" --anchor linkedin
```

Show recent sessions:

```bash
csl recent --limit 20
csl recent --project vitral
```

Open the interactive session cockpit:

```bash
csl cockpit
csl cockpit syntheval
csl cockpit --print --limit 20
```

Inspect one session:

```bash
csl show 019d203b
csl show 019d203b --messages 20 --include-assistant
```

Find related sessions:

```bash
csl related 019d203b
```

Prepare a summary:

```bash
csl summarize 019d203b
csl summarize 019d203b --json --assistant-messages 6 --commands 20 --files 20
```

Persist durable annotations:

```bash
csl annotate 019d203b \
  --summary "Debugged stuck Minikube pods and MongoDB startup behavior." \
  --tags "minikube, mongodb, kubernetes"
csl annotate 019d203b --notes "Main fix was around startup sequencing."
csl annotate 019d203b --clear
```

List unsummarized sessions:

```bash
csl queue --limit 20
```

Safe cut workflow:

```bash
csl timeline 019cf844 --width 180
csl cut 019cf844 --to 12 --name vitral-gtm-before-candidate-eval \
  --next-prompt "Continue from this point and focus only on GTM strategy."
csl cut 019cf844 --to 12 --output-dir ~/.codex/session_cuts --message-chars 3000
```

Experimental native clone:

```bash
csl native-cut 019cf844 \
  --before-text "Yes, provide those 3 documents/artifacts..." \
  --name "Vitral GTM cut before bilingual job docs" \
  --new-id 123e4567-e89b-12d3-a456-426614174000
```

Show sessions with fork hints:

```bash
csl search --forks
```

Show aggregate stats:

```bash
csl stats
csl stats --limit 10
```

## Search And Relatedness

Search is weighted across:

- thread name
- heuristic summary
- durable summary
- heuristic tags
- durable tags
- user messages
- commands
- files

Related-session detection uses a simple score based on:

- same session id
- same thread name
- same inferred project
- overlapping tags

Fork detection is heuristic. A fork hint is set when the raw text or thread name suggests a fork or branch.
