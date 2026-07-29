# Team Install

This guide is for a teammate or another local agent using Codex Session Librarian on Fedora 43 or a similar Linux environment.

## What You Need

- Codex installed and usable on the machine
- Local Codex history under `~/.codex/`
- Python 3 available in the shell
- `~/.local/bin` on `PATH` if installing to the default location

This tool uses only the Python standard library. No virtualenv or external packages are required.

## Install

Clone or unpack the repository, then run:

```bash
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
