# Session Workflows

These workflows are optimized for an LLM-assisted operator. The main idea is not just "find chat history", but "recover the right working context for the next agentic step".

## 1. Find A Past Session

Search broadly first:

```bash
csl search vitral gtm
csl search "role charter"
csl search migration --project vitral
```

When the question is "where did we actually implement, commit, push, deploy, or edit this?", prefer artifact search:

```bash
csl find-work --repo syntheval_front --commit 1a64c9d
csl find-work --repo syntheval_front --file public/syntheval-home.css
csl find-work rubric sliders --repo syntheval_front
```

When the question is "where did we discuss this?", keep search scoped to real user/assistant turns:

```bash
csl search rubric sliders --turns-only --show-hits
```

Then inspect the best candidate:

```bash
csl show 019cf844 --messages 12
csl related 019cf844
csl why 019cf844 "role charter" --turns-only
```

If you want to continue the original:

```bash
codex resume --all 019cf844-a718-77b3-bbbb-5101ee47377d
```

If you want to branch from it:

```bash
codex fork --all 019cf844-a718-77b3-bbbb-5101ee47377d
```

## 2. Add Durable Memory To Important Sessions

Prepare compact context for the assistant:

```bash
csl summarize 019cf844
```

If you want machine-readable context:

```bash
csl summarize 019cf844 --json --assistant-messages 6 --commands 20 --files 20
```

Then save a durable summary:

```bash
csl annotate 019cf844 \
  --summary "Vitral GTM strategy thread covering positioning, first GTM hire, and role charter." \
  --tags "vitral, gtm, hiring, positioning, role-charter"
```

Optional operator notes:

```bash
csl annotate 019cf844 --notes "Primary session for early GTM and hiring decisions."
```

After that, search results will prefer the saved summary and tags.

## 3. Cut A Long Conversation Safely

Use the timeline to identify the turn number:

```bash
csl timeline 019cf844 --width 180
```

Create a safe cut package:

```bash
csl cut 019cf844 --to 14 --name vitral-gtm-before-bilingual-job-docs
```

You can also seed the next task directly:

```bash
csl cut 019cf844 --to 14 --name vitral-gtm-before-bilingual-job-docs \
  --next-prompt "Continue from this point and focus only on GTM strategy."
```

This does not modify Codex's native session files. It creates a fresh context package in:

```bash
~/.codex/session_cuts/
```

Open a new session from that package:

```bash
codex -C ~/Documents/projects "$(cat ~/.codex/session_cuts/vitral-gtm-before-bilingual-job-docs-cut-014.md)"
```

## 4. Create A Native-Looking Trimmed Clone

This is experimental and writes a new JSONL session under `~/.codex/sessions/`.

Use it only when you specifically want a new session that may be opened with `codex resume`.

```bash
csl native-cut 019cf844 \
  --before-text "Yes, provide those 3 documents/artifacts..." \
  --name "Vitral GTM cut before bilingual job docs"
```

If you need deterministic ids for external tracking:

```bash
csl native-cut 019cf844 \
  --before-text "Yes, provide those 3 documents/artifacts..." \
  --name "Vitral GTM cut before bilingual job docs" \
  --new-id 123e4567-e89b-12d3-a456-426614174000
```

If successful, it prints a new session id. Open it with:

```bash
codex resume --all <new-session-id>
```

## 5. Clone Between Local Codex Accounts

Use this workflow when the same PC has session history from more than one ChatGPT/Codex account, for example `lrueda@vitral.ai` and the current `lmrc` account, and you need a session from the other account to become an operative resumable session for the current account.

This is not a normal `native-clone` case. Some forked Codex JSONL files contain multiple `session_meta` rows from their lineage. In that situation, CSL's current parser may let the last inherited `session_meta` win, so a plain `csl native-clone <id>` can copy the right file but record the wrong source id, wrong `forked_from_id`, or stale working directory.

### Operator Checklist

1. Locate the source file:

```bash
find ~/.codex/sessions -type f -name '*<source-session-id>*.jsonl' -print
```

2. Inspect all lineage metadata rows:

```bash
python3 - <<'PY'
import json
from pathlib import Path

p = Path("<source-jsonl-path>")
for n, line in enumerate(p.open(encoding="utf-8", errors="replace"), start=1):
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        continue
    if row.get("type") == "session_meta":
        payload = row.get("payload") or {}
        print(n, payload.get("id"), payload.get("forked_from_id"), payload.get("cwd"))
PY
```

3. If there is exactly one `session_meta`, `csl native-clone <source-session-id> --name "<thread name>"` is usually enough.

4. If there are multiple `session_meta` rows, create a clean manual native clone:

- Generate a new UUID.
- Copy the source JSONL to today's `~/.codex/sessions/YYYY/MM/DD/rollout-...-<new-id>.jsonl`.
- Rewrite every `session_meta.payload.id` to the new id.
- Rewrite every `session_meta.payload.forked_from_id` to the exact source session id the user asked to clone.
- Rewrite every structured `payload.cwd` to the target account workspace, such as `~/Documents/projects`.
- Replace old account home paths in command arguments and message context when the clone must operate under the current Linux user, for example from one user's home directory to another user's home directory.
- Append the new id and thread name to `~/.codex/session_index.jsonl`.
- Insert or update the matching `threads` row in `~/.codex/state_5.sqlite` so Codex does not keep prompting for the old directory.
- Run `csl index --prune`.

5. Verify the clone:

```bash
csl show <new-session-id> --messages 1
codex resume --all <new-session-id>
```

The resume screen should show the target account directory and a fork line pointing to the exact source session id. If Codex asks whether to use an old account directory, inspect `~/.codex/state_5.sqlite` table `threads` for the new id and update its `cwd` to the current account path.

### Safety Notes

- Do not edit the original source JSONL.
- Do not delete old cloned sessions unless the user explicitly confirms deletion.
- Keep the clone's `forked_from_id` as the immediate source session id requested by the user, not the older ancestral id.
- Historical command output may still contain old usernames such as `luisrueda`; that is acceptable if they are only text logs, not active `cwd`, command `workdir`, or session metadata.

## 6. Understand The Safety Boundary

Prefer `cut` for safety.

Use `native-cut` only when you explicitly want a resumable cloned session and accept that this depends on Codex's current local session-file behavior.

The distinction matters:

- `cut` creates a fresh session from a Markdown context package
- `native-cut` creates a synthetic native session by rewriting local Codex session metadata

For cross-account clones on the same PC, use the checklist above. That workflow mutates Codex's local session catalog and state DB, so verify before handing the resume command back to the user.

## 7. Keep The Index Useful Over Time

Refresh the index after new work:

```bash
csl index --prune
```

Review sessions that still need durable memory:

```bash
csl queue --limit 20
```

Check aggregate coverage:

```bash
csl stats --limit 10
```

After CSL itself changes, or after many new sessions have accumulated, rebuild structured evidence:

```bash
csl index --force --prune
```
