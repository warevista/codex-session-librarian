# CSL Evolution

This file records why the Codex Session Librarian changes over time. It is intentionally written as an operator-facing design log, not a changelog of every small edit.

## 2026-06-23: Recall Mode For Fuzzy Conversation Memory

### Trigger

A remembered conversation about LinkedIn API setup, tokens, and creating a LinkedIn developer app could not be recovered cleanly with `csl search` or `csl pinpoint`.

The operator problem was not "run a bigger grep." It was that CSL did not provide a comfortable command for fuzzy human recall:

- `search` treats each shell argument as an exact required term, so a quoted memory like `"LinkedIn API token developer app"` becomes one long phrase.
- `pinpoint` is useful for work archaeology, but it is still too work/evidence-oriented for "I remember talking about X" cases.
- Raw scripts can answer the immediate question, but they bypass the tool and make the recall workflow inconsistent.

### Design Response

Added `csl recall`.

Example:

```bash
csl recall "LinkedIn API token developer app connections" --anchor linkedin
```

The command splits a remembered phrase into fuzzy clues, ranks sessions by clue coverage, prefers same-turn correlation, defaults to real user/assistant turns, groups fork-like duplicates, and prints covered/missing clues with evidence snippets.

This is meant to be the first command an assistant reaches for when the user is trying to recover a conversation from approximate memory.

Two details matter for noisy real archives:

- `--anchor` requires a core term such as `linkedin` to appear in each evidence hit used for scoring, which prevents unrelated `token` or `developer` text elsewhere in a session from inflating a match.
- Generated clean-clone/cut context packages are excluded by default because they can contain large copied summaries that are not the actual conversation being recalled.

### Operator Behavior

Use `recall` before ad hoc raw parsing.

If `recall` returns only low-confidence candidates or no candidates, the assistant should say that directly and ask for narrowing details such as:

- approximate date or week;
- project/repo/folder;
- whether there was actual file work or only discussion;
- one exact phrase, API scope, error string, URL, file path, or command.

### Limits

`recall` is still lexical. It is better than exact search for fuzzy memory, but it does not replace durable session summaries or embeddings. A future version should add semantic summaries for every session and use them as the first retrieval layer.

## 2026-06-05: Assertive Recall And Pinpointing

### Trigger

A MongoDB/Fedora troubleshooting conversation was hard to recover reliably. Broad searches such as `mongo fedora yaml` returned older rollback discussions, while the desired conversation involved a later workaround and a subtle distinction:

- The remembered clue was "keep Mongo 8.2 by changing a setting."
- The actual recovered patch added `GLIBC_TUNABLES=glibc.cpu.hwcaps=-SHSTK`.
- The same patch also changed `mongo:8.2` to `mongo:8.0`, so the memory was partly inaccurate.

This exposed a weakness: plain lexical search can find related sessions, but it does not assertively explain whether a candidate matches the remembered work or contradicts part of the memory.

### Design Response

Added `csl pinpoint`.

The command is meant for fuzzy operator memory:

```bash
csl pinpoint mongo 8.2 GLIBC_TUNABLES deployment.yaml --repo vitral_backend
```

It improves recall by:

- ranking concrete work evidence above broad prose;
- allowing partial clue coverage;
- printing covered and missing clues per candidate;
- grouping likely fork-chain duplicate rollouts;
- printing the evidence trace that explains the candidate;
- suggesting focused follow-up questions when confidence is weak.

### Operator Behavior

When using CSL interactively, the assistant should be willing to ask the user for narrowing details instead of repeatedly guessing. Good questions include:

- Do you remember the approximate day, week, or month?
- Which repo or folder was open?
- Was there a real patch, command, commit, or only a discussion?
- Do you remember an exact file path, error string, setting name, branch name, or commit message?
- Which clue is most reliable, and which might be approximate?

### Future Work

The next useful improvements are:

- richer fork ancestry grouping based on `session_meta` chains, not only thread/first-message similarity;
- time-window filters for "last time we worked on this";
- durable per-session rationale notes that can distinguish "discussed" from "implemented and verified."

## 2026-06-05: Patch Archaeology And Contradiction Warnings

### Trigger

After adding `pinpoint`, the next highest-value gap was distinguishing "this term was present" from "this term was removed." In implementation-recall searches, this matters because a remembered value may appear only on the removed side of a patch.

### Design Response

Added `csl patches`.

Example:

```bash
csl patches --file k8s/mongodb/deployment.yaml --contains GLIBC_TUNABLES
```

The command searches only indexed `apply_patch` edits and can be constrained by file, repo, session, and patch content. It is intentionally narrower than `search` or `find-work`.

Added lightweight contradiction warnings to `pinpoint` and `patches`. If a searched clue appears in removed patch content but not in added patch content, CSL prints a warning such as:

```text
`8.2` appears in removed patch content but not added content
```

### Limits

The first implementation is heuristic because patch hits are compacted into one-line snippets in the index. It works well for common replacement clues, but it is not yet a full unified-diff parser over raw session lines.

## 2026-06-06: Session Cockpit For Terminal Proliferation

### Trigger

Working with many Codex terminals made it hard to know which sessions were main branches, narrow side branches, deprecated forks, clean clones, or contaminated originals. `codex resume all` reopens terminals, but it does not provide a decision layer for understanding what each conversation represents before or after resuming.

### Design Response

Added `csl cockpit`, a dependency-free curses TUI for browsing grouped sessions inside the terminal.

The first version focuses on immediate operator value:

- group sessions by the current naming convention when possible;
- show role markers such as `MAIN`, `SIDE`, `DEPR`, `MIXD`, `FORK`, and `SESS`;
- provide three equal-width panes for topics, sessions, and details;
- make focus explicit between the topic and session panes, with left/right changing focus and up/down moving within the focused pane;
- provide a detail pane with id, path, cwd, first/last user message, and files;
- open focused topic or session details with `Enter`;
- resume a selected session with a second `Enter` from the session details popup;
- close details with `Esc`, `q`, or `Ctrl-C` without resuming;
- allow interactive filtering with `/`;
- close the cockpit and run `codex resume <session-id>` in the same terminal;
- close the cockpit and run `codex resume --all` when the operator wants the all-session picker flow;
- allow conservative title-marker updates with `m`, `s`, and `d`.

The command also supports `--print` for non-interactive previews and automated checks.

### Rationale

The cockpit is intentionally not just another list command. The problem is spatial and operational: the user needs a browsable map of projects, topics, session roles, and likely resume targets while terminals are proliferating. A TUI gives enough structure to inspect and act without leaving the terminal.

The first implementation stores role changes by appending native thread-name rows to `~/.codex/session_index.jsonl`, matching the existing rename workflow. A later version should add a separate relationship overlay for confirmed parent/child, side-branch, continuation, superseded, and clean-clone relationships.
