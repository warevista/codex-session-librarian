# Canonical Conversion And Continuity

CSL integrates a bounded, experimental Codex/Claude conversion profile through
`csl-conversation` version 2. This is native transcript reconstruction, not a
generated summary pasted into a new conversation. It uses only the standard
library and never fetches credentials or buys model inference.

## Convert And Receive

```bash
csl convert SOURCE_UUID --to claude --cwd ~/work/project
csl convert SOURCE_UUID --to codex --cwd ~/work/project --open --model MODEL
csl import ~/Downloads/handoff.csl.zip --target claude --cwd ~/work/project --open
csl receive csl-xxxxxxxxxxxx --target codex --cwd ~/work/project --open
```

Sources are auto-detected. `--provider` restricts/asserts the source;
`--to` on convert and `--target` on import/receive explicitly choose a destination.
Without a target, existing native transfers stay native. All CSL options must
precede `--open`; subsequent arguments belong to the destination CLI.

Every conversion installs a new UUID and leaves originals unchanged. A converted
receive has its own receipt keyed by destination, history selection and repair
options; it never reuses the native copy by mistake. Repeat that same route to
reuse its copy, or use `--new` for another one. Source share ancestry is retained
when the converted session is later shared back.

Canonical JSON can be transferred privately and restored without the source CLI:

```bash
csl convert SOURCE_UUID --to canonical --out ~/Downloads/conversation.json
csl convert ~/Downloads/conversation.json --to claude --cwd ~/work/project
```

Canonical outputs are private `0600` files and cannot overwrite existing files.
The [v2 JSON Schema](canonical-v2.schema.json) describes the text/tools profile.
Canonical JSON is not itself a `.csl.zip` or GitHub release asset; use native
share/receive with `--target`, or transfer that JSON privately and use convert.

## Profile And Fail-Closed Behavior

Text, assistant phases, JSON/text tool calls, completed text results, error flags
and historical citation/reference metadata are preserved. References render as
explicit historical text without opening URLs; hashed projection spans retain
their original metadata and reject stale/edited projections on another conversion.
Tool names and arguments remain historical evidence, not instructions to retry.

Codex structured compactions select replacement history before normalization;
Claude selects its parent-linked active branch and stops at readable compaction
summaries. `--leaf UUID` explicitly selects another Claude branch. Rollback is
handled only when it does not cross a compaction; uncertain native windows fail.

System/developer policy, AGENTS/environment context and internal state are not
reinstalled as destination authority. Canonical `source_context` archives that
context separately; native destinations include only provenance hashes/counts.
Source auth, shell settings, MCP definitions and execution permissions are not
translated. The recipient's own CLI configuration and explicit launch flags apply.
Historical assistant model metadata uses a synthetic marker in Claude output;
select an available recipient model via `--open --model MODEL` when needed.

The following do not silently degrade into an apparently faithful conversion:

- Images, PDFs, external attachments and unknown content blocks are rejected in
  this profile. Native same-CLI transfers still preserve their existing JSONL.
- Opaque encrypted compaction cannot be decoded. Updated portable exports mark
  removed compaction state even if an entire checkpoint disappeared. Exact
  active-context conversion then fails, rather than resurrecting older history.
- `--history-policy visible` explicitly recovers readable history, labels it as
  recovery in the destination, and may be much larger than the source active
  context. It does not reproduce hidden summaries or recover missing parents.
- Unfinished tool calls fail by default. `--repair-interrupted` adds a marked
  historical error result with outcome UNKNOWN; it never executes the tool.
- Unknown versions/fields, conflicting graphs, orphan tool results and stale
  reference projections are rejected. No model-name or tool-name equivalence is
  invented between the two CLIs.

Conversion and diagnostic analysis currently have a 64 MiB per-transcript limit.
Large native Codex transfers retain their existing limits, but get an explicit
analysis-limit warning instead of a misleading continuity certification.

## Diagnose Before Continuing

```bash
csl doctor SOURCE_UUID --json
csl doctor ~/Downloads/handoff.csl.zip --json
csl doctor SOURCE_UUID --native --json --model MODEL --context-window 200000
csl doctor SOURCE_UUID --native --expect-text "latest agreed marker" --absent-text "excluded sibling marker"
```

The report separates four evidence levels:

| Level | Meaning |
| --- | --- |
| Structure | JSONL, graphs and paired compaction metadata were checked |
| Static active context | The supported checkpoint/branch was selected; not proof that the installed CLI will select it |
| Native replay | Installed CLI reconstructed a request against a rejecting localhost server, in disposable homes |
| Live continuation | Always `not-tested` here; no provider inference/semantic recall was performed |

Native replay reports the installed CLI version, item/text counts and transport
assertion failures by index, not message bodies. It uses fake keys, isolated
`HOME`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR` and an empty project, and stops before
generation. It does not read/copy your login or run historical tools. A native
compaction request can also be observed; its request path is reported, but a
successful compacted response is not fabricated.

The isolated environment intentionally excludes the recipient's real AGENTS,
plugins, MCP schemas, local settings and other prompt overhead. Thus even a
successful native reconstruction is **not** a guarantee of context fit or model
availability in the real environment. `--expect-text` checks transport, not what
a real model recalls. No compatibility claims extend to every historical CLI
version, account mode or backend provider.

Token estimates use text characters / 4 and are explicitly approximate. Images,
opaque state, tokenizer behavior and tool schemas can make them inaccurate.
`--context-window` is a user-supplied risk threshold, not a model catalog lookup
or a change to CLI configuration. No guessed token estimate blocks an import.

For an exact, explicit **text character** budget use `--max-context-chars N` on
convert/import/receive/doctor. It rejects oversized reconstructed text, including
cached receives. If static context cannot be reconstructed, the budget cannot
be enforced and the operation fails; doctor can use native measurement instead.
No hidden context is cut to satisfy a budget.

Native imports always print a continuity preflight and warnings. Invalid paired
metadata fails before installing any sessions. Missing ancestry, lost opaque
state, analysis limits and large-context risk remain visible limitations. An
opened UI alone is never reported as proof that the next model turn will work.

## Verification

```bash
python3 -m unittest discover -s tests -q
CSL_RUN_CODEX_NATIVE_TESTS=1 CSL_RUN_CLAUDE_NATIVE_TESTS=1 python3 -m unittest discover -s tests -q
```

Synthetic tests cover all four routes, corrected decisions, tool-only facts,
source-policy quarantine, canonical restore, explicit recovery/repair, receiver
cache separation and size guards. Native tests capture requests in both CLIs
without paid endpoints. The public coverage matrix remains synchronized by tests.
