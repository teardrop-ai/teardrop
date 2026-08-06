---
name: repo-research
argument-hint: "Provide one focused research question or prompt."
description: "Use when the user wants query-driven, cited repository research for Teardrop security, roadmap, improvements, or competitive questions and a Git-tracked draft report."
disable-model-invocation: false
metadata: research, pipeline, gpt-researcher, security, roadmap, improvements, competitive, evidence, draft-report
user-invocable: true
---

# Repository Research

Run Teardrop's developer-only research pipeline from one focused question. This skill is an orchestration loop, not a shortcut around the pipeline's provenance and safety checks.

## Operating contract

- Always invoke `scripts.research_run`, never `scripts.research_repo` directly. The wrapper loads `.env` silently with `override=False` and the isolated worker inherits the resulting environment.
- Never open, print, echo, quote, paste, or otherwise place `.env` contents or credential values in agent context. Never pass a credential on a command line.
- Use Teardrop's `.venv` for the parent runner and `.research-venv` for GPT-Researcher. Never install GPT-Researcher into the shared `.venv`.
- A dry-run is mandatory before a provider call. It collects and reports the sanitized source manifest but does not call GPT-Researcher.
- Do not add `--allow-dirty`, `--force`, `--github-mcp`, or a non-default source mode unless the user requested or approved that option.
- Every output is a `draft`. Never promote it automatically or edit `docs/research/knowledge-index.md` as part of this skill.

## Future-value gate

Invoke this skill only when all conditions hold:

1. `docs/research/knowledge-index.md` and any current verified report do not already answer the question.
2. The question requires durable synthesis across multiple subsystems, revisions, or current external primary sources; targeted search, nearby tests, or one documentation lookup is insufficient.
3. The coordinator can name a concrete future consumer and expected shelf life, such as an implementation cycle, security baseline, architecture decision, roadmap choice, provider/dependency choice, or repeated review.
4. The report can change a future decision, implementation constraint, verification strategy, or risk posture.
5. The expected reuse justifies provider cost and human review.

Route narrow bug diagnosis, failing-test investigation, code explanation, and single-symbol verification through normal coding-agent search/read/test pathways. Record the future consumer, shelf life, and affected decision before starting a research run. A vague possibility of future usefulness is not sufficient.

The gate and safety contract apply identically when this skill is invoked by a coordinator or by a human. A coordinator may delegate up to three independent qualifying questions to the coordinator-only `repo-researcher` agent. Each subagent owns its dry-run and returns a structured success, approval-needed, or failure result. Do not fan out dependent questions.

## Consumption calibration

A produced draft is a hypothesis plus a citation map, not a fact. Any agent that later reads a draft must verify each cited file/symbol against live code and check `evidence_commit`/staleness before relying on a claim. Drafts are excluded from evidence collection and are not trusted context; only `knowledge-index.md` and promoted reports are trusted without re-verification.

## 1. Normalize the request

Require one non-empty, focused question of at most 800 characters and 100 words. Preserve the user's intent, but narrow a broad request into one question before running it. Put detailed paths, invariants, and test requirements in `--scope` and the topic contract, not in a checklist-heavy query. Split compound questions into separate runs. The CLI enforces these limits before source collection or any provider call. Accept an explicit topic when supplied; otherwise infer exactly one:

- Billing, settlement, auth, SSRF, marketplace, MCP/A2A, or migration exploit analysis -> `security`
- Product direction, capabilities, planned work, or dependencies -> `roadmap`
- Incremental correctness, reliability, security, performance, developer experience, or product expansion grounded in existing code -> `improvements`
- Comparisons with external products or open-source projects -> `competitive`

Ask when the topic is ambiguous. Keep any requested `--scope`, `--require-path`, `--report-source`, `--github-mcp`, timeout, or output options and use the same options for both dry-run and final run.

Use a shape like: `Does <path or symbol> preserve <invariant> under <condition>, and what one incremental change is justified?`

For cross-file questions, use `--require-path` for the mutation owner, invalidation/helper owner, and focused test file or directory. This is an explicit evidence contract, not a heuristic; the pipeline fails before the provider if a required path is missing from the collected manifest.

## 2. Run zero-cost preflight

Run from the repository root. Stop and explain the repair if any check fails.

On Windows, verify the two interpreters and the isolated package without printing environment values:

```powershell
if (-not (Test-Path .venv\Scripts\python.exe)) { throw "Missing .venv\Scripts\python.exe" }
if (-not (Test-Path .research-venv\Scripts\python.exe)) { throw "Missing .research-venv\Scripts\python.exe" }
& .research-venv\Scripts\python.exe -c "import gpt_researcher; print('gpt_researcher: importable')"
```

If the isolated environment is missing or the import fails, stop. Tell the user to create or repair it with:

```powershell
py -3.12 -m venv .research-venv
.research-venv\Scripts\python.exe -m pip install -r requirements-research.txt
```

Before the final run, perform a presence-only credential check in a child process. Load `.env` silently and print only category status, never values or file contents. Derive the required LLM and embedding providers from GPT-Researcher's `FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM`, and `EMBEDDING` settings; when those settings are absent, use the pinned package defaults, which are OpenAI-backed. Map providers to their documented variables: `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, or `GOOGLE_API_KEY`. For `web` or `hybrid`, also check the configured retriever key; the default `tavily` retriever requires `TAVILY_API_KEY`. Require `GITHUB_TOKEN` when `--github-mcp` is enabled. Treat an unknown provider or missing credential as a stop condition rather than guessing.

The pinned GPT-Researcher release expects provider settings in `<provider>:<model>` form. Treat a slash-delimited or otherwise malformed setting as a configuration failure and stop before the final command.

Example Windows presence check. Keep the command on one line and replace the `SOURCE` and `GITHUB_MCP` literals with the non-secret values selected for this run when checking search or GitHub credentials:

```powershell
& .venv\Scripts\python.exe -c "from dotenv import load_dotenv; import os; load_dotenv('.env', override=False); keys={'openai':('OPENAI_API_KEY',),'openrouter':('OPENROUTER_API_KEY',),'anthropic':('ANTHROPIC_API_KEY',),'google_genai':('GOOGLE_API_KEY',),'google_vertexai':('GOOGLE_API_KEY','GOOGLE_APPLICATION_CREDENTIALS')}; defaults={'FAST_LLM':'openai:','SMART_LLM':'openai:','STRATEGIC_LLM':'openai:','EMBEDDING':'openai:'}; providers={os.getenv(name, default).split(':',1)[0].lower() for name,default in defaults.items()}; missing=sorted({key for provider in providers for key in keys.get(provider,()) if not os.environ.get(key)}); unknown=sorted(provider for provider in providers if provider not in keys); print('llm: unknown provider '+','.join(unknown) if unknown else ('llm: missing '+','.join(missing) if missing else 'llm: set'))"
```

Extend the child check with the selected `RETRIEVER` mapping and `GITHUB_TOKEN` check when applicable; do not print the values. If the provider cannot be identified from repository configuration or the user's request, ask before continuing.

## 3. Mandatory dry-run

Run the wrapper with the complete intended argument set plus `--dry-run`. Keep the query in a shell variable and pass it as an argument; do not build a command by interpolating credentials or arbitrary script text.

```powershell
$query = "<focused question>"
& .venv\Scripts\python.exe -m scripts.research_run `
   --topic <topic> `
   --query $query `
   --dry-run
```

Use `--json` with `--dry-run` when a wrapper or script needs to parse the manifest. The JSON includes `revision`, `evidence_dirty`, `source_manifest_sha256`, `source_file_count`, `source_files`, and `required_paths`.

The dry-run must exit successfully. Inspect its structured lines and record:

- `revision`, which must not be `unknown`;
- `evidence_dirty`, which must be exactly `true` or `false`;
- `report_source`;
- `source_files`, which must be greater than zero for `local` or `hybrid` research;
- `source_manifest_sha256`, which must be present for `local` or `hybrid` research;
- `relevant_dirty_paths`, which must contain only selected, eligible evidence paths;
- the listed repository-relative source paths.

Do not run the provider if the dry-run fails, has no eligible local sources, has an unknown revision, or produces malformed evidence metadata. Treat a suspicious source path as a stop condition and investigate the collector rather than widening scopes silently.

On Windows, prefer `--json` and keep diagnostic redirection outside the repository (for example, `$env:TEMP`) because the dirty-tree check includes untracked files created by shell redirection.

## 4. Resolve evidence state before spending tokens

If `evidence_dirty: false`, retain the dry-run `revision` and manifest hash for comparison with the final report.

If `evidence_dirty: true`, stop before the final command and present the user with exactly these choices:

1. Commit the changes, rerun the dry-run, and produce a promotable clean-evidence draft.
2. Explicitly approve an exploratory run with `--allow-dirty`; explain that the report will record `evidence_dirty: true` and is not eligible for promotion.

Never choose the second option silently. A coordinator-only subagent must return `needs_approval` rather than ask an unavailable user. If the user chooses it, add `--allow-dirty` to both the final command's intended arguments and the recorded run summary, after rerunning the dry-run against the same working tree.

## 5. Execute the final research run

For a clean tree, the user's original request to run this skill authorizes the final provider call after the successful dry-run. For a dirty tree, require the explicit choice above. Use the same topic, query, source mode, scopes, MCP option, timeout, and worker interpreter as the dry-run. Do not add `--force` unless the user explicitly requests replacing an existing draft.

```powershell
& .venv\Scripts\python.exe -m scripts.research_run `
   --topic <topic> `
   --query $query `
   --expect-revision <dry-run revision> `
   --expect-manifest-sha256 <dry-run manifest hash> `
   --expect-evidence-dirty <true-or-false>
```

The command must fail closed on an existing output, missing credentials, a provider error, timeout, or invalid report. Report the error and the smallest recovery action; do not retry a paid run automatically.

## 6. Validate and hand off the draft

After a successful command, verify that the reported output is under `docs/research/<topic>/` and exists. Read its metadata and confirm:

- `status` is `draft`;
- `evidence_commit` equals the dry-run `revision`;
- `evidence_dirty` matches the approved decision;
- `source_manifest_sha256` equals the dry-run hash when local or hybrid sources were used;
- the report has the required sections, a non-empty `Sources` section, and citations to collected repository paths for local or hybrid research.

If any comparison fails, treat the run as unsuccessful and do not promote it. Otherwise report the draft path, topic, query, evidence commit, dirty state, manifest hash, redaction count if present, and that the findings remain unverified. Tell the user to inspect cited live files and symbols, reproduce security claims or add a focused test where practical, then update report status and `knowledge-index.md` only through human review.

For `improvements` reports, apply the `.github/skills/ruthless-critic-verifier/SKILL.md` checklist before promotion. Verify current behavior against live code, distinguish facts from proposals, check user/business value and dependencies, preserve backward compatibility with `spec/`, require additive migrations for schema changes, and confirm no secrets in logs/errors, immutable financial ledgers, SSRF/auth/org isolation, OWASP controls, and focused tests. Do not mark a candidate verified solely because GPT-Researcher proposed it. After implementation, commit the change and rerun research against the new evidence revision.

## Recovery and manual checks

- Missing `.research-venv` or import failure: install only from `requirements-research.txt` into `.research-venv`.
- Missing credential: configure the provider in the user's environment or `.env`; report only the missing variable/category name, never its value.
- Dirty evidence: commit and rerun, or obtain explicit approval for `--allow-dirty`.
- Existing draft: ask whether to use an explicit `--force`; never overwrite by default.
- Query rejected as too long: rewrite it as one question within 800 characters and 100 words; do not retry the same compound prompt.
- Required evidence path rejected: add the owning mutation/helper/test path to `--scope`, or correct the path before any provider retry.
- Stale archive: run `.venv\Scripts\python.exe -m scripts.research_run --check-stale` and review each reported mismatch.
- Manual free check: run the complete intended command with `--dry-run`; this is the only verification path that must not contact an LLM.
