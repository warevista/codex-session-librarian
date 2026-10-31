# Team Install

This guide is for a teammate or another local agent using Codex Session Librarian on Fedora 43 or a similar Linux environment.

## What You Need

- Codex and/or Claude Code installed and usable for opening native sessions
- Local history under `~/.codex/` and/or `~/.claude/` (home overrides supported)
- Python 3.11 or newer available in the shell
- `~/.local/bin` on `PATH` if installing to the default location

This tool uses only the Python standard library. No virtualenv or external packages are required.

## Install

Puerta (`puertajs`) and Daniela (`danielaz7`) should accept their GitHub collaborator
invitation to get push access. The repository is public, so cloning/pulling does
not require an invitation.

Clone the shared repository, then run:

```bash
git clone https://github.com/warevista/codex-session-librarian.git
cd codex-session-librarian
./install.sh
```

That installs a symlinked launcher at:

```bash
~/.local/bin/csl
```

If you want a different prefix:

```bash
./install.sh /usr/local
```

## First Run

Build the local index:

```bash
csl index --prune
```

This reads:

- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/session_index.jsonl`

And writes:

- `~/.codex/session_librarian.sqlite`
- `~/.codex/session_cuts/` when using `cut`

## Receive A Shared Session

For the private GitHub inbox, install GitHub CLI and connect once per computer:

```bash
gh auth login
csl team connect warevista/codex-session-exchange
```

Accept the separate invitation to the private exchange first. Then:

```bash
csl share <session-uuid> --title "Context for the team"
csl inbox
csl receive csl-xxxxxxxxxxxx --cwd ~/Documents/projects --open
```

Use the ID printed by `share`/`inbox`. Receiving again reuses your existing copy;
`--new` requests an independent copy. Read [SHARING.md](SHARING.md) for details.

For Claude Code, discover and send with:

```bash
csl sessions --provider claude
csl share CLAUDE_UUID --title "Claude context"
```

Share/export detect the source automatically; specify `--provider claude` or
`--provider codex` only to restrict lookup or resolve cross-CLI ambiguity.
Receive auto-detects the native CLI. Both teammates must update CSL for Claude
packages. Put CSL options before `--open`, then pass native CLI flags after it.
Experimental text/tool conversion uses `--target codex` / `--target claude` on
receive/import, or `csl convert UUID --to CLI`. Check before continuing with
`csl doctor UUID --native --json`; this reconstructs a request only against
localhost, without a paid model call. See [conversion limits](CONVERSION.md)
and [coverage matrix](PROVIDER_MATRIX.md)
and `csl capabilities --json` before assuming a command supports both CLIs.

### Manual ZIP Transfer

Your agent can import the native conversation without first indexing your own history:

```bash
csl import ~/Downloads/team-handoff.csl.zip --list
csl import ~/Downloads/team-handoff.csl.zip --cwd ~/Documents/projects --open
```

The directory must exist on your computer. Your own Codex login is used; neither
the sender's account nor its credentials are copied. The session receives a new
UUID, so importing it again never overwrites your previous copy.

To send sessions back:

```bash
csl export <session-uuid> --to ~/Downloads
```

Transfer the ZIP privately. Do not commit session packages to this public repository.
Read [TRANSFERS.md](TRANSFERS.md) for limits and `--raw` behavior.

## Update And Contribute

```bash
cd codex-session-librarian
git pull --ff-only
```

No reinstall is needed: `csl` points to this checkout. To contribute code:

```bash
git switch -c feat/my-change
python3 -m unittest discover -s tests -v
git add <changed-files>
git commit -m "Describe the change"
git push -u origin feat/my-change
```

Use a pull request to integrate changes into `main`.

## Basic Use

Search:

```bash
csl search "role charter"
csl search vitral gtm
```

Inspect a session:

```bash
csl show 019cf844 --messages 20 --include-assistant
```

Find related sessions:

```bash
csl related 019cf844
```

Prepare a durable summary:

```bash
csl summarize 019cf844
```

Persist durable annotations:

```bash
csl annotate 019cf844 \
  --summary "Vitral GTM strategy thread covering positioning and hiring." \
  --tags "vitral, gtm, hiring"
```

## Cut And Clone Workflows

Safe cut:

```bash
csl timeline 019cf844 --width 180
csl cut 019cf844 --to 14 --name vitral-gtm-before-bilingual-job-docs
```

This creates a Markdown context package for a fresh Codex session. It does not modify native Codex session files.

Experimental native clone:

```bash
csl native-cut 019cf844 \
  --before-text "Yes, provide those 3 documents/artifacts..." \
  --name "Vitral GTM cut before bilingual job docs"
```

This writes a synthetic session into Codex's local session store so it may open with:

```bash
codex resume --all <new-session-id>
```

Use `native-cut` only if you explicitly want a resumable cloned session and accept that it depends on Codex's current local session-file behavior.

## Recommended Reading

Read these docs in this order:

1. `README.md`
2. `docs/COMMANDS.md`
3. `docs/SESSION_WORKFLOWS.md`

## Notes

- The tool is local-user scoped. Each teammate indexes their own `~/.codex/` data.
- Updates are simple: pull the repo changes and keep using `csl`.
- This is an internal operator tool, not a packaged end-user product.
