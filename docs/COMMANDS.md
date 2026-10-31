# Commands

For native Claude export/import/share/receive, provider selection and forwarded
launch flags, read [the command/provider matrix](PROVIDER_MATRIX.md).
`csl capabilities --json` reports shipped coverage; `csl sessions --provider
claude` lists native Claude sessions. The indexed librarian commands below
remain Codex-only unless the matrix explicitly says otherwise.

`csl convert` implements explicit canonical v2 conversion. `csl doctor` reports
continuity evidence without claiming live inference. See [commands and safety
contract](CONVERSION.md); use `--target` on import/receive for destination conversion,
while `--provider` remains a source filter/assertion.

This is an LLM-native operator tool: the commands are designed so an assistant can inspect, summarize, trim, and reopen Codex sessions efficiently.

This project has two layers:

- `csl` / `codex_sessions.py`: local session librarian commands.
- `codex`: the native Codex CLI for opening, resuming, and forking sessions.

## Librarian Commands

### `index`

Parse session files into `~/.codex/session_librarian.sqlite`.

```bash
csl index --prune
csl index --force
```

Flags:

- `--sessions-dir`: override the raw Codex sessions directory
- `--session-index`: override the native Codex session index file
- `--force`: re-parse all sessions even if unchanged
- `--prune`: remove DB rows for deleted session files

### `search`

Search by topic, files, tags, thread name, messages, or commands.

```bash
csl search mongo
csl search "role charter"
csl search vitral gtm --project vitral
csl search --forks
csl search rubric sliders --turns-only --show-hits
csl search syntheval homepage --artifacts --show-hits
```

Flags:

- `--limit`: max rows returned
- `--project`: restrict to one inferred project
- `--forks`: only sessions with fork hints
- `--turns-only`: search only real user/assistant turns
- `--artifacts`: search only commands, commits, patches, and files
- `--type`: restrict evidence search to a hit type such as `commit_created`, `patch`, or `assistant_final`
- `--show-hits`: print matching evidence snippets under each result
- `--include-inherited`: include evidence inherited from fork ancestors
- `--no-dedupe`: show duplicate evidence across fork-chain copies
- `--verbose`: print more session detail

### `find-work`

Find sessions where work was actually done, instead of merely discussed.

```bash
csl find-work --repo syntheval_front --commit 1a64c9d
csl find-work --repo syntheval_front --file public/syntheval-home.css
csl find-work rubric sliders --repo syntheval_front
```

This command ranks artifact evidence higher than broad prose:

- `commit_created`: output from an actual `git commit`
- `patch`: `apply_patch` edits
- `file`: changed or referenced file paths
- `command`: shell commands
- `assistant_final`: final implementation summaries

Flags:

- `--file`: require a file/path fragment
- `--commit`: require a commit hash or message fragment
- `--repo`: restrict to a repo/path fragment
- `--type`: restrict evidence search to one hit type
- `--include-inherited`: include evidence inherited from fork ancestors
- `--no-dedupe`: show duplicate evidence across fork-chain copies
- `--limit`: max rows returned
- `--verbose`: print more session detail

### `pinpoint`

Assertively locate the most likely conversation when the memory is fuzzy.

```bash
csl pinpoint mongo 8.2 GLIBC_TUNABLES deployment.yaml --repo vitral_backend
csl pinpoint syntheval homepage rubric sliders --repo syntheval_front --ask
csl pinpoint --file k8s/mongodb/deployment.yaml SHSTK mongo
```

This command is designed for "I remember we worked on X" searches. It differs from `search` and `find-work` in four ways:

- It allows partial clue coverage and prints which clues were covered or missing.
- It ranks concrete work evidence, such as patches, commands, commits, and final verification, above general discussion.
- It groups likely fork-chain duplicates into one candidate family.
- It prints clarification questions when confidence is weak or the result set is ambiguous.

Flags:

- `--file`: file/path fragment that should appear in the evidence
- `--commit`: commit hash or message fragment that should appear in the evidence
- `--repo`: repo/project path fragment to constrain results
- `--require-all`: only show candidates covering every clue
- `--include-inherited`: include evidence inherited from fork ancestors
- `--ask`: always print clarification questions
- `--evidence`: number of evidence lines to show per candidate
- `--width`: max characters per evidence line
- `--limit`: max candidate families returned

### `recall`

Find conversations from approximate human memory.

```bash
csl recall "LinkedIn API token developer app connections" --anchor linkedin
csl recall "Syntheval homepage rubric sliders" --anchor syntheval
csl recall "Mongo Fedora yaml setting board" --anchor mongo --min-coverage 3
```

Use this before falling back to raw archive parsing. Unlike `search`, a quoted phrase is split into fuzzy clues and candidates are ranked by clue coverage.

This command is designed for "I remember talking about X" searches. It differs from `pinpoint` because it defaults to real user/assistant turns and is not primarily focused on concrete work artifacts.

Flags:

- `--anchor`: require the anchor term in every evidence hit used for scoring, useful for avoiding pollution from unrelated terms elsewhere in the same session
- `--must`: require a term somewhere in the candidate session
- `--min-coverage`: require a minimum number of covered clues
- `--turns-only`: search only real user/assistant turns; this is the default when no type flags are provided
- `--artifacts`: search only commands, commits, patches, and files
- `--all-types`: search every indexed evidence type
- `--include-context-packages`: include generated clean-clone/cut bootstrap context; excluded by default
- `--include-inherited`: include evidence inherited from fork ancestors
- `--ask`: always print clarification questions
- `--evidence`: number of evidence lines to show per candidate
- `--width`: max characters per evidence line
- `--limit`: max candidate families returned

### `patches`

Search only indexed `apply_patch` edits.

```bash
csl patches --file k8s/mongodb/deployment.yaml --contains GLIBC_TUNABLES
csl patches mongo:8.2 --repo vitral_backend
csl patches --session 019ddfeb --file deployment.yaml 8.2
```

This command is for file/change archaeology. It is narrower than `search` and `find-work`: every result is an actual patch hit, not a discussion or command.

When a searched term appears in removed patch content but not added content, CSL prints a warning. This helps catch remembered claims such as "we kept `mongo:8.2`" when the patch actually removed `mongo:8.2`.

Flags:

- `--file`: file/path fragment touched by the patch
- `--contains`: patch content term to require
- `--repo`: repo/project path fragment to constrain results
- `--session`: restrict to one session id, path fragment, or thread name fragment
- `--include-inherited`: include patches inherited from fork ancestors
- `--width`: max characters per patch snippet
- `--limit`: max patch hits returned

### `why`

Explain why one candidate session matched a query.

```bash
csl why 019e6eed compact homepage --artifacts
csl why 019e6eed rubric sliders --turns-only
```

Output includes identity details for forked sessions and the exact evidence hits.

### `recent`

Show recently updated indexed sessions.

```bash
csl recent --limit 20
csl recent --project vitral
csl recent --forks
```

Flags:

- `--limit`: max rows returned
- `--project`: restrict to one inferred project
- `--forks`: only sessions with fork hints
- `--verbose`: print more session detail

### `cockpit`

Open an interactive terminal UI for browsing grouped sessions before deciding what to resume.

```bash
csl cockpit
csl cockpit syntheval
csl cockpit --project projects
csl cockpit --print --limit 20
```

The cockpit groups sessions by the naming convention when possible, for example:

```text
Syntheval / Screenshot Readiness
  MAIN 019e92d4  Main implementation, seeded demo and candidate workspace routing
  SESS 019e9d4a  Seeded demo surfaces and candidate workspace routing
```

Controls:

- `Left` / `Right` or `h` / `l`: move focus between the topic and session columns.
- `Up` / `Down` or `j` / `k`: move within the focused column.
- `Enter` on a topic: show the topic's sessions in a details popup.
- `Enter` on a session: show session details; pressing `Enter` again from that details popup resumes the session.
- `Esc`, `q`, or `Ctrl-C` inside details: close details without resuming.
- `/`: filter by terms.
- `a`: close the cockpit and run `codex resume --all` in the same terminal.
- `m`: mark the selected session as `MAIN` by appending a native thread-name row.
- `s`: mark the selected session as `SIDE`.
- `d`: mark the selected session as `DEPRECATE`.
- `q`, `Esc`, or `Ctrl-C`: quit.

Flags:

- `--limit`: max sessions loaded into the cockpit
- `--project`: restrict to one inferred project
- `--print`: print the grouped cockpit view without starting the interactive UI

### `show`

Inspect one session in more detail.

```bash
csl show 019d203b
csl show 019d203b --messages 20 --include-assistant
```

Flags:

- `--messages`: number of recent user messages to print
- `--include-assistant`: include recent assistant messages

### `related`

Find likely related sessions and forks.

```bash
csl related 019cf844
csl related 019cf844 --limit 15
```

Flags:

- `--limit`: max related sessions returned

### `summarize`

Prepare compact context for a durable summary.

```bash
csl summarize 019d203b
csl summarize 019d203b --json --assistant-messages 6 --commands 20 --files 20
```

Flags:

- `--messages`: user-message count to include
- `--assistant-messages`: assistant-message count to include
- `--commands`: command count to include
- `--files`: file/path count to include
- `--json`: output machine-readable JSON context

### `annotate`

Persist a durable summary, tags, and notes.

```bash
csl annotate 019d203b \
  --summary "Debugged stuck Minikube pods and MongoDB startup behavior." \
  --tags "minikube, mongodb, kubernetes"
csl annotate 019d203b --notes "Main fix was around startup sequencing."
csl annotate 019d203b --clear
```

Flags:

- `--summary`: durable summary
- `--tags`: durable comma-separated tags
- `--notes`: durable operator notes
- `--clear`: remove durable summary, tags, notes, and summary timestamp

### `queue`

List sessions that still need durable summaries.

```bash
csl queue --limit 20
```

Flags:

- `--limit`: max rows returned

### `timeline`

Show numbered user/assistant turns for choosing a cut point.

```bash
csl timeline 019cf844 --width 180
```

Flags:

- `--width`: max characters shown per timeline entry

### `cut`

Create a safe cut package for a fresh session.

```bash
csl cut 019cf844 --to 14 --name vitral-gtm-before-candidates \
  --next-prompt "Continue from this point and focus only on GTM strategy."
```

Flags:

- `--to`: include messages up to this timeline index
- `--name`: filename-friendly label for the cut
- `--next-prompt`: optional task to append to the context package
- `--output-dir`: where to write the Markdown cut package
- `--message-chars`: max characters per included message
- `--commands`: number of early commands to include
- `--command-chars`: max characters per included command

Output:

- Writes a Markdown context package under `~/.codex/session_cuts/` by default
- Prints a `codex -C ... "$(cat ...)"` command for starting a fresh session

### `native-cut`

Create an experimental native trimmed clone that may open with `codex resume`.

```bash
csl native-cut 019cf844 \
  --before-text "Yes, provide those 3 documents/artifacts..." \
  --name "Vitral GTM cut before bilingual job docs"
```

Flags:

- `--before-text`: cut before the first user message containing this text
- `--name`: thread name for the synthetic cloned session
- `--new-id`: optional UUID override for the new cloned session

Behavior:

- Writes a synthetic JSONL session under `~/.codex/sessions/YYYY/MM/DD/`
- Rewrites session metadata with a new id and `forked_from_id`
- Appends a new entry to `~/.codex/session_index.jsonl`
- Reindexes the new session into the librarian DB
- Prints a `codex resume --all <new-session-id>` command

### `export` And `import`

For automatic private GitHub transfer, use:

```bash
csl team connect warevista/codex-session-exchange
csl share <uuid> --title "Team context"
csl inbox --json
csl receive csl-xxxxxxxxxxxx --cwd ~/Documents/projects --open
```

`team status` checks the connection. `share` supports several selectors and
`--raw`. `inbox --limit N` bounds the metadata listing. `receive --new` makes a
fresh copy instead of reusing an existing import. See [SHARING.md](SHARING.md).

Transfer native conversation history between teammates without transferring their
accounts or local Codex databases:

```bash
csl export <uuid> --to ~/Downloads
csl export <uuid> <another-uuid> --to ~/Downloads/handoff.csl.zip
csl import ~/Downloads/handoff.csl.zip --list
csl import ~/Downloads/handoff.csl.zip --cwd ~/Documents/projects --open
```

Export defaults to portable mode; add `--raw` to retain encrypted reasoning.
Import always creates fresh UUIDs. Full details: [TRANSFERS.md](TRANSFERS.md).

### `native-clone`

Create an experimental full native clone that may open with `codex resume`.

```bash
csl native-clone 019cf844 \
  --name "Vitral GTM full clone"
```

Flags:

- `--name`: optional thread name for the cloned session
- `--new-id`: optional UUID override for the new cloned session

Behavior:

- Copies the full selected JSONL session
- Rewrites session metadata with a new id and `forked_from_id`
- Appends a new entry to `~/.codex/session_index.jsonl`
- Reindexes the new session into the librarian DB
- Prints a `codex resume --all <new-session-id>` command

Account-switch note:

- If the source session came from another local ChatGPT/Codex account on the same PC, inspect the JSONL first. Forked sessions can contain multiple inherited `session_meta` rows, and a plain `native-clone` may preserve stale ancestry or working-directory state. Use the "Clone Between Local Codex Accounts" workflow in `docs/SESSION_WORKFLOWS.md`.

### `stats`

Show aggregate statistics for the local index.

```bash
csl stats
csl stats --limit 10
```

Flags:

- `--limit`: how many top projects/tags to show

## Search And Matching Model

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

Fork hints are heuristic and come from raw text or thread-name signals suggesting a fork or branch.

## Native Codex Commands

Resume a specific session or fork:

```bash
codex resume --all <session-id>
```

Fork a specific session:

```bash
codex fork --all <session-id>
```

Open the interactive picker across all working directories:

```bash
codex resume --all
```

Start a fresh session from a safe cut package:

```bash
codex -C /path/to/project "$(cat ~/.codex/session_cuts/some-cut.md)"
```
