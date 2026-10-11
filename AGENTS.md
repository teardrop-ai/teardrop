# Repository Guidelines

Use this guide for architecture, commands, and implementation patterns. Also read
[.github/copilot-instructions.md](.github/copilot-instructions.md) for the repository's
workflow, research, and testing policies. Keep overlapping guidance consistent;
maintain detailed workflow policy there rather than duplicating it here.

## Project Overview
Teardrop is a Python FastAPI service for autonomous agents: LangGraph execution, REST/SSE, A2A, MCP (`/tools/mcp`), and x402/MPP/prepaid-credit billing. Postgres with pgvector stores durable state. Redis provides shared caching and rate limits; startup requires it when `REDIS_REQUIRED=true`.

## Architecture & Data Flow
- **Entry:** `teardrop/app.py` builds `app = FastAPI(lifespan=_app_lifespan)`. `teardrop/main.py` is a compatibility alias. Run with `uvicorn teardrop.main:app`.
- **Lifecycle:** `teardrop/_lifespan.py::build_lifespan` opens the psycopg pool (`shared/db_pool.py::create_pool`), sets `app.state.pool`, applies migrations, initializes Redis, the graph checkpointer, and domain services, then starts background workers. Shutdown cancels workers and releases service references before closing the pools.
- **Routing:** `teardrop/routers/__init__.py::register_routers` registers REST routers; nested admin/org routers live in `teardrop/routers/admin/` and `teardrop/routers/org/`. MCP mounts and middleware are configured in `teardrop/app.py`.
- **Agent execution:** SSE: `teardrop/routers/agent.py` → `teardrop/agent_event_loop.py::stream_graph_events` → graph event streaming. Non-streaming: `teardrop/agent_runtime.py::run_agent_once` → `graph.ainvoke`. Both share run-context preparation and use `agent/graph.py`: planner ↔ tool executor → UI generator. Usage/settlement helpers live in `teardrop/usage.py` and `teardrop/agent_post_run.py`.
- **Tools:** `tools/__init__.py` holds a module-level `registry = ToolRegistry()`. Built-ins live in `tools/definitions/`. Marketplace tools use qualified `org/tool` names.
- **Billing:** `billing/__init__.py` exposes public operations and synchronizes compatibility state/hooks with submodules. Prefer its public API; preserve existing facade-level patching patterns in tests.
- **State:** `agent/state.py::AgentState` flows through the graph, backed by a Postgres checkpointer; `agent/runtime_context.py` supplies per-run `ContextVar` context. Redis rate limits can fall back to per-process counters (`teardrop/rate_limit.py`). Agent admission (`teardrop/concurrency.py`) is always process-local, not a Redis fallback.

## Key Directories
| Path | Purpose |
|---|---|
| `teardrop/` | App, config, auth, billing glue, usage, funnel counters, MCP gateway |
| `agent/` | LangGraph graph, planner, node executor, runtime context |
| `tools/` | Tool registry, executor, definitions, MCP server |
| `billing/` | x402/MPP/credit settlement facade |
| `teardrop/routers/` | FastAPI REST routers, including `admin/` and `org/` |
| `shared/` | DB pool and cross-cutting helpers |
| `marketplace/`, `org_tools/`, `mcp_client/` | Paid catalog/execution, custom org tools, and outbound MCP sessions |
| `scheduling/`, `labeling/`, `evals/` | Scheduled/event-driven runs, prediction outcomes, and evaluation harness |
| `migrations/versions/` | Active SQL migrations (`NNN_name.sql`). `migrations/archive/` is read-only history. |
| `scripts/` | Ops, seed, release, and research CLIs |
| `tests/` | `unit/`, `api/`, `integration/`, shared `conftest.py` |
| `docs/` | Long-form docs; `docs/research/` holds research drafts |
| `spec/` | Exported API spec (regenerate with `python -m scripts.export_api_spec`) |

## Development Commands
Use Python 3.12 and `.venv`. Commands below use the Windows interpreter path;
on Unix replace it with `.venv/bin/python`. Activation is optional: PowerShell
can run `.\.venv\Scripts\Activate.ps1`, but an activation script is not a native executable.

```bash
# Install
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt

# Run
.venv/Scripts/python -m uvicorn teardrop.main:app --reload

# Lint / format (CI runs format --check)
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff check --fix .
.venv/Scripts/python -m ruff format .
.venv/Scripts/python -m ruff format --check .

# Migration prefixes must be unique
.venv/Scripts/python scripts/check_migration_prefixes.py

# API spec after route or event changes (CI fails if spec/ is stale)
.venv/Scripts/python -m scripts.export_api_spec
```

CI checks lint, formatting, migration prefixes, and exported-spec drift. Scope auto-fixes to the changed files to avoid unrelated rewrites.

## Code Conventions & Common Patterns
- **Formatting:** ruff, line length 130, target py312. Every `.py` file starts with the BUSL-1.1 SPDX and copyright header; `CPY001` enforces it, including under `tests/`.
- **Naming:** snake_case modules, `_private` helpers, `*Error` exception classes, `logger = logging.getLogger(__name__)`.
- **Config:** `teardrop/config.py::Settings` reads `.env`; `get_settings()` caches the result. Clear that cache when tests change environment variables, and account for modules holding import-time settings references. Billing and marketplace flags default off. Defaults are not proof that a deployment or feature is configured correctly.
- **Module pools:** common pattern: `_pool`, `init_<domain>_db(pool)`, `close_<domain>_db()`, and `_get_pool()` raising when uninitialized (`teardrop/usage.py`, `teardrop/wallets.py`, `teardrop/retention.py`). Lifecycle functions may be synchronous or asynchronous; match the existing module.
- **Dependency injection:** use existing FastAPI auth dependencies: `require_auth` (`teardrop/auth.py`), `require_admin`, `require_org_admin`, and `require_scope` (`teardrop/dependencies.py`). Tests also use `app.dependency_overrides`.
- **Async:** request paths commonly use `async def`; keep blocking work off the event loop (e.g., `asyncio.to_thread` in `teardrop/a2a_client.py`). Preserve existing locks, run-slot leases, and cancellation/finalization behavior.
- **Errors:** routers commonly raise `HTTPException`; `tools/executor.py::execute_tool` wraps raw implementation output in `ToolResult` (`error_class`, `retry_safe`, `billable`). Tool implementations do not need to return that envelope. `record_usage_event` catches/logs ordinary exceptions; this best-effort pattern is not a rule for financial mutations.
- **Funnel counters:** a bounded surface vocabulary in `teardrop/funnel_counters.py`. Use `record_discovery_hit` or `record_discovery_hit_if_not_bot`. Add a new surface to `VALID_SURFACES` or to a validated prefix, and never let caller-supplied strings through unbounded.
- **Migrations:** add a unique numeric-prefix `NNN_name.sql` under `migrations/versions/`; choose the prefix from the live directory, never from this guide. Each SQL migration and its tracking rows run in a transaction; the runner serializes application with a Postgres advisory lock.
- **Built-in tools:** export a `TOOL` definition and add its import/entry in `tools/definitions/__init__.py::_ALL_TOOLS`; registration is explicit, not automatic file discovery.

## Important Files
- Entry and lifecycle: `teardrop/app.py`, `teardrop/_lifespan.py`, `teardrop/routers/__init__.py`
- Agent: `teardrop/agent_runtime.py`, `agent/graph.py`, `tools/executor.py`
- Gateway: `teardrop/mcp_gateway.py` (free-tool allowlist `_ANON_FREE_TOOLS`), `teardrop/funnel_counters.py`
- Config: `teardrop/config.py`, `.env.example`, `docker-compose.yml`, `Dockerfile`, `render.yaml`
- Build and lint: `pyproject.toml` (ruff `required-version = ">=0.16.0"`, pytest `asyncio_mode = "auto"`, coverage `fail_under = 75`), `requirements.txt`, `requirements-dev.txt`
- Migrations: `migrations/runner.py`, `scripts/check_migration_prefixes.py`

## Runtime/Tooling Preferences
- **Runtime:** use Python 3.12, matching CI, Docker, and Ruff's target. Install with pip and `requirements.txt` + `requirements-dev.txt`; research uses a separate `.research-venv` and `requirements-research.txt`.
- **Ruff:** `pyproject.toml` requires `>=0.16.0`. Use the venv interpreter to avoid a different global installation; reinstall dev requirements when tooling is outdated.
- **Environment:** copy `.env.example` to `.env`, configure a development Postgres DSN and credentials for the enabled providers/features. Some older setup examples mention Anthropic or `venv`; verify against live `teardrop/config.py` and use `.venv`.
- **Docker:** `docker compose up --build` runs the app and pgvector Postgres; Compose needs `.env`. The runtime image uses a non-root user and excludes keys, tests, scripts, and docs from its build context.
- **Keys:** startup generates keys when needed; optional manual command: `.venv/Scripts/python -m scripts.generate_keys`. The manual command aborts if the private key exists. Do not delete live keys to rerun it.
- **`notes/`** is gitignored. Don't commit anything there.
- **Scripts:** inspect flags and target environments before execution. `scripts/research_repo.py` may call a paid provider unless `--dry-run` is used; `scripts/bump_version.py` writes unless `--dry-run`. Bazaar seeding defaults to quoting, but `--execute` pays; reputation seeding requires `--execute --confirm-live-calls` for real calls.

## Testing & QA
- **Frameworks:** requirements specify `pytest>=8`, pytest-asyncio (`asyncio_mode = "auto"`), xdist, pytest-cov, `anyio[trio]`, httpx, and asgi-lifespan; installed versions can differ.
- **Async tests:** follow nearby `@pytest.mark.asyncio`, `@pytest.mark.anyio`, or plain async conventions. API/integration fixtures select the asyncio AnyIO backend. Root pytest-asyncio auto mode also claims async tests: a `[trio]` test label alone does not prove Trio execution.
- **Fixtures:** `tests/conftest.py::test_settings` creates test keys, sets `DATABASE_URL=""`, and resets cached settings; it runs indirectly through an autouse RPC fixture. `tests/api/conftest.py` provides HTTPX `ASGITransport` clients and isolates app state. Integration fixtures live in `tests/integration/conftest.py`.
- **Mocks:** `monkeypatch.setattr("teardrop.routers.<mod>.<symbol>", ...)` by dotted string. Unit tests use `unittest.mock.patch`.
- **Naming:** `test_<module>.py`, `Test<Thing>` classes, `test_<behavior>` functions. Duplicate basenames across directories are safe because every test dir has `__init__.py`.
- **Commands:**
  ```bash
  .venv/Scripts/python -m pytest tests/unit/ tests/api/ -n auto --dist load
  .venv/Scripts/python -m pytest tests/integration/ -n 0 -p no:cacheprovider
  ```
- **Integration safety:** use only a disposable database: a nonempty `DATABASE_URL` is used directly and fixtures `TRUNCATE ... CASCADE`. Otherwise Docker starts pgvector Postgres on port 5433. Never use `-n auto`; workers would collide on the port/shared tables. Tests skip if no database/Docker is available or `SKIP_INTEGRATION_TESTS` is nonempty; check the summary.
- **Quality rules:** `tests/unit/test_tool_definition_standard.py` `_EXEMPT` must stay empty. `tests/unit/test_retention.py` guards the `tests/retention_settings.py` stub. Changed behaviour needs tests (`CONTRIBUTING.md`). Don't re-pin wording or incidental defaults.
- **Coverage:** CI combines unit and API coverage, then enforces `fail_under = 75` (`pyproject.toml`, `.github/workflows/test.yml`). Ordinary runs without `--cov` do not enforce coverage. Live agent evaluation guidance is in `docs/eval-harness.md`; such runs can spend real provider/tool credits.

