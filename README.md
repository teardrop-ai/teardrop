```
████████╗███████╗ █████╗ ██████╗ ██████╗ ██████╗  ██████╗ ██████╗
╚══██╔══╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗██╔═══██╗██╔══██╗
   ██║   █████╗  ███████║██████╔╝██║  ██║██████╔╝██║   ██║██████╔╝
   ██║   ██╔══╝  ██╔══██║██╔══██╗██║  ██║██╔══██╗██║   ██║██╔═══╝
   ██║   ███████╗██║  ██║██║  ██║██████╔╝██║  ██║╚██████╔╝██║
   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝
```

Teardrop is a streaming AI agent API. You send it a message; it reasons using your configured LLM (Anthropic, OpenAI, Google, or OpenRouter), optionally calls tools, builds a structured UI component tree, and streams everything back as Server-Sent Events. It implements four open protocols simultaneously: **AG-UI** (streaming events), **A2A** (agent discoverability), **MCP** (tool serving), and **x402** (per-request payments in USDC on Base, no subscription required).

---

## Core Features

### Agent-to-Agent (A2A) Delegation

Agents can securely delegate tasks to other agents via the `delegate_to_agent` tool (invoked during `/agent/run`). Features include allowlist control, JWT forwarding, per-run quotas, destination cost caps, and optional credit billing with org-level and caller-principal pause/24-hour spend-limit enforcement.

For the full protocol specification, environment variables, allowlists, and billing event payloads, see [docs/a2a-delegation.md](docs/a2a-delegation.md).

### Platform Tool Marketplace

Teardrop exposes 30 built-in, metered tools through the marketplace catalog. Callers can invoke them:
- Via the **MCP gateway** at `GET /tools/mcp` (direct tool invocation, billed per call).
- As **tools called during agent runs** (via `POST /agent/run` when the agent decides to use them; billed in the run's usage cost).

For the complete list of tools, detailed descriptions, and their per-call prices, please refer to the [docs/tools-catalog.md](docs/tools-catalog.md).

Enable with `MARKETPLACE_ENABLED=true`. When enabled, tools appear in `GET /marketplace/catalog` with `qualified_name = "platform/{tool_name}"` and `tool_type = "platform"`. Catalog discovery supports `category` filtering, `sort=popularity`, single-tool detail pages, an author index at `GET /marketplace/authors`, author profiles, LLM-friendly discovery at `GET /marketplace/llms.txt`, and current effective prices at `GET /marketplace/quote?tool={qualified_name}`. The public catalog is the complete active published inventory for an author; authenticated agents can inspect their current org inventory at `GET /agent/tools`. The author index reports active published authors and aggregate call counts, then links clients to the existing author profile for tool details; it is not a list of remote A2A delegation URLs. Aggregate quality metrics are available at `GET /.well-known/reputation.json`. Marketplace authors can register external MCP servers and publish discovered tools as listings via `POST /marketplace/import/preview` and admin-only `POST /marketplace/import/publish`.

Organizations can optionally publish one remote A2A endpoint in the public directory with admin-only `PUT /marketplace/agent-registration`. Teardrop accepts HTTPS base URLs only, SSRF-checks them, discovers the remote Agent Card, and requires the `/message:send` endpoint used by the A2A client. The registry is separate from the outbound delegation allowlist: publishing makes an endpoint discoverable but does not authorize calls to it. `GET /marketplace/agents` lists opted-in endpoints with reputation metrics only after five distinct calling organizations; self-traffic, local failures, and ambiguous `possibly_delivered` outcomes are excluded.

Agent-owned webhook tools can be registered through `POST /tools`. Machine-provisioned organizations configure marketplace payouts through `POST /marketplace/author-config` with their owning SIWE wallet; the wallet is pinned to that identity unless an organization admin configures a treasury wallet.

Platform tools are always available during agent runs, are not subscribable via `POST /marketplace/subscriptions`, and incur their marketplace prices (in addition to token costs). Per-org pricing overrides are supported via `POST /admin/pricing/tools`; for agent runs, `tool_pricing_overrides` takes precedence over marketplace catalog prices when both exist for the same tool.

### Marketplace Settlement & USDC Sweeping

Organizations can monetize their agents via a Marketplace. Earned fees are settled to organization wallets on-chain via Coinbase Developer Platform (CDP). When an org requests a withdrawal, Teardrop settles earned fees to a ledger entry (pending), attempts an on-chain USDC transfer via CDP to the org's specified address, and records the `tx_hash` on success (or reverts to pending on failure).

Auto-sweep is configured via `MARKETPLACE_AUTO_SWEEP_ENABLED` and related `MARKETPLACE_*` settings (see [docs/configuration.md](docs/configuration.md)). Admin APIs: `POST /admin/marketplace/sweep` (manual sweep) and `GET /admin/marketplace/settlement-balance`.

### Verified-Email Onboarding Credit

Teardrop can grant a small prepaid credit balance to newly verified organizations so a first agent run is possible without immediately setting up a wallet or card. This is **disabled by default** (`ONBOARDING_CREDIT_ENABLED=false`) and is intended only as a conversion aid, not as a source of withdrawable marketplace earnings.

The grant is awarded after a user consumes a single-use email verification token (`GET /auth/verify-email`). Token consumption, marking the user verified, and enqueueing eligibility are committed atomically; the grant is idempotent and retried by a background worker until it succeeds. Promotional credit can be used for platform tools, org webhook tools, MCP tools, and the base agent run cost, but **cannot** call marketplace author tools. A real top-up (Stripe, on-chain USDC, admin top-up, or refund) converts the org to normal credit status and removes the marketplace restriction. x402-paid SIWE calls are unaffected.

Check `GET /billing/balance` after verification to see the granted balance. See [docs/configuration.md](docs/configuration.md) for the `ONBOARDING_CREDIT_*` settings.

### Unattended (Scheduled) Agent Runs

Organizations can schedule recurring, unattended agent runs with integrated credit-only billing, stored execution history, and real-time status callbacks. Managed via the `/agent/schedules` API (`POST`/`GET`/`PATCH`/`DELETE`, plus `GET /agent/schedules/{id}/runs` for cursor-paginated results).

Due runs are claimed with a row-locking query (`FOR UPDATE SKIP LOCKED`) and execute concurrently with per-run failure isolation, so multiple worker instances can scale horizontally. Results are archived under `scheduled_run_results` and can be dispatched to an HTTPS-only, SSRF-checked callback URL. Use `callback_format=text` for a plain-text mobile notification; JSON remains the default. Configure via `SCHEDULED_RUNS_*` settings (see [docs/configuration.md](docs/configuration.md)).

Scheduled analysis prompts can call the internal `record_predictions` tool with their exact structured payload and return only the human-readable report. Teardrop labels those predictions asynchronously against future observations through the generalized labeling data plane; callbacks can therefore send clean reports to mobile notifications while structured values remain available for ML evaluation. See [docs/architecture.md](docs/architecture.md) and the `LABELING_*` settings in [docs/configuration.md](docs/configuration.md).

### Event-Triggered (Reactive) Runs

Beyond fixed intervals, organizations can register **event triggers** that run the agent in response to inbound webhooks (e.g. an on-chain event, a CRM update, a CI signal). An event trigger stores a prompt *template*; the inbound JSON payload is interpolated into it at dispatch time. Event triggers reuse the same execution core, credit billing, result history, and callback delivery as scheduled runs.

Managed via the `/agent/event-triggers` API (CRUD, secret rotation, and run polling). Inbound dispatch is `POST /agent/events/{trigger_token}`, authenticated with a per-trigger secret via the `X-Teardrop-Trigger-Secret` header, with optional idempotency keys and scalar-only, length-capped prompt interpolation to resist injection. Postgres execution leases enforce global and per-org concurrency (returning `429` when saturated).

Prompt templates interpolate untrusted payload data, so treat rendered prompts as untrusted input to the agent — scope event-trigger tools and credit limits accordingly. Configure via `EVENT_TRIGGERS_*` settings (see [docs/configuration.md](docs/configuration.md)).

---

## Requirements

- Python 3.12+
- An API key for your chosen LLM provider: [Anthropic](https://console.anthropic.com/), [OpenAI](https://platform.openai.com/), or [Google AI](https://aistudio.google.com/) (optional if using BYOK or self-hosted)
- A Postgres database (local via Docker, or [Neon](https://neon.tech) for production)
- Redis (optional, for caching — falls back to in-memory with TTL)

---

## Setup (PowerShell)

**1. Clone and enter the project**
```powershell
git clone https://github.com/teardrop-ai/teardrop.git
cd teardrop
```

**2. Create and activate a virtual environment**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

> If you get a script execution error, run first:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

**3. Install dependencies**
```powershell
pip install -r requirements.txt
```

**4. Configure environment**

```powershell
Copy-Item .env.example .env
```

Minimum required contents:
```
# Global LLM provider fallback: anthropic | openai | google | openrouter (default: openrouter)
# Note: Each org can override via PUT /llm-config
AGENT_PROVIDER=openrouter
# Default model is deepseek/deepseek-v4-flash-0731.
# For OpenRouter DeepSeek models, Teardrop delegates provider eligibility to the API key's OpenRouter data policy.
OPENROUTER_API_KEY=sk-or-...      # required if AGENT_PROVIDER=openrouter
# ANTHROPIC_API_KEY=sk-ant-...     # required if AGENT_PROVIDER=anthropic
# OPENAI_API_KEY=sk-...           # required if AGENT_PROVIDER=openai
# GOOGLE_API_KEY=...              # required if AGENT_PROVIDER=google

DATABASE_URL=postgresql://teardrop:teardrop@localhost:5432/teardrop

# Optional: Redis for distributed caching
# REDIS_URL=redis://localhost:6379/0
```

**5. Generate RSA keys**

The RSA keypair is generated automatically at app startup (into `keys/`).
To generate it manually first (optional), run:
```powershell
python scripts/generate_keys.py
```

**6. Run database migrations**
```powershell
python -m migrations.runner
```

**7. Seed default org and admin user**
```powershell
python scripts/seed_users.py
```

**8. Run the API server**
```powershell
uvicorn teardrop.main:app --reload
```

Server starts at `http://localhost:8000`. Visit `http://localhost:8000/docs` for the interactive API explorer.

---

## Deployment

### Docker (local full stack)

```powershell
docker compose build --pull
docker compose up
```

Starts Postgres + Teardrop API. Migrations run automatically at startup. Keys are generated at build time and mounted from `./keys/`. Use `docker compose build --pull` before rebuilds so refreshed base images are picked up, and add `--no-cache` when you want a fully fresh rebuild.

### Render (production)

The repo includes a `render.yaml` that configures a Render web service. 

For the comprehensive list of environment variables, security credentials, database configurations, and rate-limiting limits, please refer to the dedicated [docs/configuration.md](docs/configuration.md) reference guide.

---

## Authentication

Teardrop issues RS256 JWTs. Protected business endpoints require a `Bearer` token. Public auth, registration, billing-pricing, health, documentation, agent-card, and the payment-gated `POST /message:send` A2A surfaces remain unauthenticated as described below.

### 1. Client credentials (machine-to-machine)

```powershell
$resp = Invoke-RestMethod -Uri "http://localhost:8000/token" `
    -Method Post -ContentType "application/json" `
    -Body '{"client_id":"teardrop-client","client_secret":"<JWT_CLIENT_SECRET>"}'
$token = $resp.access_token
```

The resulting JWT includes `auth_method: "client_credentials"`. Set `JWT_CLIENT_ID` and `JWT_CLIENT_SECRET` in `.env`.

### 2. Email + password

```powershell
$resp = Invoke-RestMethod -Uri "http://localhost:8000/token" `
    -Method Post -ContentType "application/json" `
    -Body '{"email":"admin@example.com","secret":"<password>"}'
```

The resulting JWT includes `auth_method: "email"`. Create users via `POST /admin/users`.

### 3. SIWE — Sign-In with Ethereum

SIWE lets Ethereum wallet holders authenticate without a password. The JWT issued includes `auth_method: "siwe"` and the caller's `address`.

```
1. GET  /auth/siwe/nonce   → { "nonce": "abc123..." }
2. Construct an EIP-4361 SIWE message with that nonce
3. Sign with your wallet (EIP-191)
4. POST /token  { "siwe_message": "...", "siwe_signature": "0x..." }
   → { "access_token": "..." }
```

SIWE tokens authenticate wallet identity, and new wallet addresses are auto-registered on first login. Machine-provisioned SIWE orgs use prepaid credit for agent runs; fund that credit through the agent funding loop below.

### 4. x402 payment-first bootstrap

An external agent can bootstrap an account without email, a human signup, or a prior JWT. Discover the flow from `/.well-known/agent-card.json`, then send `POST /token` with `{"grant_type":"x402"}`. The first response is a `402 Payment Required` containing the x402 requirements for enough prepaid credit to pass the client-credential run reserve. Retry with the signed `Payment-Signature` (or legacy `X-Payment`) header.

On successful first settlement, Teardrop returns a short-lived access token plus a one-time `client_id` and `client_secret`. Store the secret immediately; it cannot be retrieved later. Use those credentials with the existing client-credentials `/token` flow for subsequent billable runs. Machine-provisioned orgs receive no promotional credit and start with the configured `MACHINE_ORG_DAILY_SPEND_LIMIT_USDC` rolling cap (default: $5.00 per 24 hours). An explicit operator-set org limit is preserved for trusted orgs; a zero stored limit resolves to the configured default.

#### Agent funding loop

Bootstrap once, then use the returned client credential to call `GET /billing/topup/usdc/requirements?amount_usdc=...` and sign the returned x402 requirements. Submit the signed header to `POST /billing/topup/usdc` before the prepaid balance is exhausted. Repeat `grant_type=x402` payments are also accepted for an existing machine org; they reuse its client ID and omit `client_secret`. If the original secret is lost, authenticate with SIWE and use `POST /org/credentials/regenerate`.

### Token expiry and refresh tokens

The email, client-credentials, and SIWE auth methods issue access tokens with a **30-minute expiry** (`expires_in: 1800` seconds in the token response). The x402 bootstrap also returns a 30-minute access token, but does not issue a refresh token. For applications that need sessions longer than 30 minutes, use refresh tokens from the email or SIWE flows:

- **Refresh tokens** expire after **30 days** and can be exchanged for a new access token + rotated refresh token.
- **Refresh token rotation** is atomic with idempotency replay protection — if the same refresh token is submitted twice within the replay window, you'll receive the same new token pair instead of creating duplicates.
- **Single logout** via `POST /auth/logout` revokes your refresh token, ending the session immediately.

```powershell
# 1. Exchange refresh token for new access token + rotated refresh token
$resp = Invoke-RestMethod -Uri "http://localhost:8000/auth/refresh" `
    -Method Post -ContentType "application/json" `
    -Headers @{ "Cookie" = "refresh_token=<your-refresh-token>" }
    # OR pass as body: -Body '{"refresh_token":"<your-refresh-token>"}'

# New access token and rotated refresh token are in the response
$newAccessToken = $resp.access_token
$newRefreshToken = $resp.refresh_token  # Use this on your next refresh

# 2. Logout (revoke refresh token)
Invoke-RestMethod -Uri "http://localhost:8000/auth/logout" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $accessToken" }
```

---

## LLM Configuration (Per-Org)

Organizations can configure their preferred LLM provider, model, routing strategy, and optionally bring their own API keys (BYOK). This unlocks:

- **Multi-provider choice**: Use Anthropic, OpenAI, Google, or point at self-hosted endpoints (vLLM, Ollama, OpenRouter)
- **Bring Your Own Key (BYOK)**: Encrypt and store your own API credentials — Teardrop never sees your keys. BYOK orgs pay only platform orchestration fees (per-token when `BYOK_TIER_PRICING_ENABLED=true`, or a flat fee otherwise); the LLM provider is billed directly to their own key.
- **Smart routing**: Automatically select models based on cost, speed, or quality
- **Self-hosted support**: Use any OpenAI-compatible endpoint via `api_base` parameter

### Org LLM config endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/llm-config` | Bearer | Get your org's LLM config (or global defaults if not configured) |
| `PUT` | `/llm-config` | Bearer | Set or update org LLM config |
| `DELETE` | `/llm-config` | Bearer | Delete config, revert to global defaults |

### Example: Set org's LLM to GPT-4o with cost-based routing

```powershell
$token = (Invoke-RestMethod -Uri "http://localhost:8000/token" `
    -Method Post -ContentType "application/json" `
    -Body '{"client_id":"teardrop-client","client_secret":"<secret>"}').access_token

Invoke-RestMethod -Uri "http://localhost:8000/llm-config" `
    -Method Put -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token" } `
    -Body @{
        provider = "openai"
        model = "gpt-4o"
        routing_preference = "cost"  # or "speed", "quality", "default"
        max_tokens = 4096
        temperature = 0.7
    } | ConvertTo-Json
```

For BYOK, add your own `api_key` (encrypted at rest). For self-hosted endpoints (vLLM, Ollama, or any OpenAI-compatible server), set `api_base` to your endpoint URL.

### Routing preferences

When you set `routing_preference` to a value other than `"default"`, Teardrop will automatically select a model from its standard pool based on your criteria:

| Preference | Behavior |
|------------|----------|
| `default` | Use the provider/model you configured |
| `cost` | Select the cheapest model (by tokens-in + tokens-out pricing) |
| `speed` | Select the fastest model (by p95 latency from live benchmarks; falls back to official specs for new deployments) |
| `quality` | Select the highest quality model |

**Note**: If you set BYOK (custom API key), routing is disabled — you always use your configured model.

### LLM credit model

Teardrop prepaid credits (or on-chain x402 settlement) cover the full cost of every run, but the line item depends on whether the org uses platform LLM keys or BYOK:

| Mode | Who pays the LLM provider | What Teardrop debits from the org |
|------|---------------------------|-----------------------------------|
| Platform keys (default) | Teardrop | Full model cost from `pricing_rules` (token-in + token-out + run fee) |
| BYOK | The org, directly through their own key | Platform orchestration fee only: flat `BYOK_PLATFORM_FEE_USDC` (default $0.001/run), or per-token orchestration pricing when `BYOK_TIER_PRICING_ENABLED=true` floored at that flat fee. The fee appears as `platform_fee_usdc` in usage events and SSE billing events. |

In other words, BYOK does not eliminate the need for Teardrop credits or x402 settlement — it only removes the LLM model cost from the Teardrop bill.

---

## Model Benchmarks

Teardrop continuously tracks operational metrics for every LLM deployed. These benchmarks help you make informed routing decisions.

### Benchmarks endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/models/benchmarks` | — | Public: all models with catalogue metadata + live metrics |
| `GET` | `/models/benchmarks/org` | Bearer | Org-scoped: metrics for your org's usage only |

### Understanding the metrics

Each model entry includes catalogue metadata (`provider`, `model`, `display_name`, `context_window`, `supports_tools`, `quality_tier`, `pricing`) plus live `benchmarks`:

- **total_runs_7d**: Number of runs using this model in the last 7 days (benchmarks only included if >= 10 runs)
- **avg_latency_ms**: Average time (ms) from start to completion
- **p95_latency_ms**: 95th percentile latency — the slowest 5% of runs
- **avg_cost_usdc_per_run**: Average cost per run (input + output tokens + tools)
- **avg_tokens_per_sec**: Streaming throughput (useful for real-time applications)
- **quality_tier**: Static tier (1=best, 2=good) for quality-based routing

---

## Billing & Payments (x402)

Teardrop implements the [x402 payment protocol](https://x402.org). When `BILLING_ENABLED=true`, requests must include payment. Set `BILLING_ENABLED=false` (default) to run without billing during development.

### How it works

Teardrop implements the [x402 payment protocol](https://x402.org). When `BILLING_ENABLED=true`, requests must include payment. Set `BILLING_ENABLED=false` (default) to run without billing during development.

An unpaid `POST /agent/run` returns a `402 Payment Required` with the x402 v2 `PaymentRequired` envelope (`PAYMENT-REQUIRED` header, plus legacy `X-PAYMENT-REQUIRED`). The client signs the payment and retries with a `Payment-Signature` header; Teardrop verifies it with the facilitator, streams the SSE response, and emits a `BILLING_SETTLEMENT` event with the on-chain `tx_hash` after the run.

### Payment methods by auth type

| Auth method | Payment mechanism |
|-------------|-------------------|
| `siwe` | Human-owned orgs: x402 on-chain (USDC, `exact` or `upto` scheme, per-request); machine-provisioned orgs: prepaid credit only, subject to the hard daily cap |
| `client_credentials` | Org prepaid credit balance (off-chain debit) |
| `email` | Org prepaid credit balance (off-chain debit) |

The x402 payment-first bootstrap charges a reserve-sized x402 payment, settles it before issuing credentials, and credits the resulting atomic-USDC amount to the new org. It does not use the verified-email promotional grant. Authenticated machine credentials can use the larger, less frequent `/billing/topup/usdc` balance top-up path instead of paying per run.

### x402 payment schemes

| Scheme | How it works | Config |
|--------|-------------|--------|
| `exact` (default) | Client signs the exact run price; facilitator settles that amount. | `X402_SCHEME=exact` |
| `upto` | Client signs a ceiling (`X402_UPTO_MAX_AMOUNT`); after the run, Teardrop settles the actual usage cost (≤ ceiling) via Permit2. | `X402_SCHEME=upto` |

> **upto client requirement**: Before using `X402_SCHEME=upto`, the paying wallet must approve Permit2 for USDC on the target chain once:
> `IERC20(USDC).approve(PERMIT2_ADDRESS, type(uint256).max)`. This is a one-time on-chain transaction per wallet. Clients that have not approved Permit2 can always use `scheme: exact` from the `accepts` array in the 402 response as a fallback.

### Pricing

Pricing is dynamic via the `pricing_rules` database table. Current rates (usage-based v1):

| Metric | Rate |
|--------|------|
| Input tokens | $0.0015 / 1k tokens |
| Output tokens | $0.0075 / 1k tokens |
| Tool calls | $0.001 / call |
| Minimum per run | $0.01 |

Check live pricing: `GET /billing/pricing`

### Running as x402 client (SIWE payments)

```powershell
# 1. Get a SIWE JWT (see Authentication above)
# 2. Call /agent/run — you'll get a 402 with payment requirements
# 3. Construct and sign the x402 transferWithAuthorization (EIP-3009)
# 4. Retry with the signed payment header

Invoke-RestMethod -Uri "http://localhost:8000/agent/run" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token"; "Payment-Signature" = "<x402-header>" } `
    -Body '{"message":"What is the ETH balance of vitalik.eth?","thread_id":"session-1"}'
```

The stream will include a `BILLING_SETTLEMENT` event with the on-chain `tx_hash` after the run completes.

### Credit top-up (machine callers)

Admins can add prepaid USDC credit to an org's balance:

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/admin/credits/topup" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $adminToken" } `
    -Body '{"org_id":"org-123","amount_usdc":1000000}'   # $1.00
```

For the full set of billing environment variables (`BILLING_ENABLED`, `X402_*`, `BILLABLE_AUTH_METHODS`, etc.), see [docs/configuration.md](docs/configuration.md).

---

## A2A Delegation & Cross-Agent Revenue Routing

Teardrop agents can delegate tasks to remote A2A-compliant agents and charge those delegations back to the calling organization. This unlocks decentralized specialisation, built-in spend orchestration, and automatic revenue routing.

For a detailed protocol specification, system configurations, allowlists, and billing event payload structures, see the [docs/a2a-delegation.md](docs/a2a-delegation.md) guide.

---

## Publishing to Agentic Market

Use the full public URL of the paid A2A surface, for example `https://api.teardrop.dev/message:send`, with method `POST`. Do not register the bare origin `https://api.teardrop.dev/` (`GET /` redirects to `/docs`, and `POST /` is not a paid entrypoint).

Agentic Market validators may probe `/.well-known/x402`, `/.well-known/x402.json`, and the configured endpoint URL. Teardrop serves those discovery aliases publicly and issues the x402 challenge on unpaid anonymous `POST /message:send` requests before body validation. The challenge includes the full x402 v2 `PaymentRequired` envelope (`PAYMENT-REQUIRED`), a legacy `X-PAYMENT-REQUIRED` alias, a top-level `resource.url` describing the paid surface, and `extensions.bazaar` describing the A2A request/response shape.

## Publishing to Smithery

Teardrop automatically advertises its MCP tools via `/.well-known/mcp/server-card.json`. To distribute on Smithery:

1. Copy the public base URL of your Teardrop instance into the Smithery **URL Deployment** wizard.
2. Provide the following **Configuration Schema (JSON)** inside the Smithery CLI or publish wizard to expose x402 anonymous capability:
   ```json
   {
     "type": "object",
     "properties": {
       "apiKey": {
         "type": "string",
         "title": "API Key",
         "x-from": { "header": "x-teardrop-key" },
         "x-to": { "header": "Authorization" }
       }
     }
   }
   ```
3. Set the Display Name, Description, Homepage, and Icon within the Smithery dashboard to achieve the maximum 100/100 quality score.

## API Reference

Teardrop provides interactive visual API explorers via OpenAPI at `/docs` (Swagger UI) and `/redoc` (ReDoc UI).

For a complete tabular list of all endpoints across Core, Auth, Billing, Marketplace, Wallets, Admin, Custom Tools, Memory, MCP Federation, and A2A Delegation surfaces, consult the [docs/api-reference.md](docs/api-reference.md) guide.

---

## Running the MCP tool server (optional)

The tools can be served standalone over the MCP protocol for use with Claude Desktop, VS Code, or any MCP-compatible client:

```powershell
# stdio transport (default – for Claude Desktop / VS Code)
python tools/mcp_server.py

# HTTP SSE transport
python tools/mcp_server.py --transport=sse
```

---

## How It Works

Teardrop uses portable agent nodes behind a LangGraph routing/checkpoint adapter and emits framework-neutral runtime events to its Server-Sent Events (SSE) layer.

For the complete architectural design, execution flow state diagrams, SSE stream event descriptions, and structured A2UI component schemas, consult the [docs/architecture.md](docs/architecture.md) reference.

---

## Database

Teardrop uses Postgres (Neon recommended for production, local via Docker for development).

### Migrations

All schema changes are in `migrations/versions/`. Run them with:

```powershell
python -m migrations.runner
```

For the complete tracking list of database schema migrations, seed entries, and indexes, please refer to the [docs/migrations.md](docs/migrations.md) catalog.

### Neon (production)

Set `DATABASE_URL` to your Neon connection string. Plain `postgresql://` is preferred; legacy `postgresql+asyncpg://` values remain accepted.

---

## Project structure

```
teardrop/
  app.py              # FastAPI app, lifespan, middleware, background workers, router registration
  main.py             # Compatibility entrypoint (re-exports teardrop.app)
  routers/            # APIRouter modules (agent.py, billing.py, marketplace.py, auth.py, admin/, org/, …)
  config.py           # Settings via pydantic-settings (reads .env)
  auth.py             # RS256 JWT & refresh tokens (email, client_credentials, SIWE)
  users/              # Org + User models, CRUD, PBKDF2-SHA256 password hashing
  billing/            # x402 billing layer, pricing, invoice queries, credit system
  marketplace/        # Marketplace catalog, earnings, reputation, subscriptions, withdrawals
  mcp_client/         # Per-org MCP client: CRUD, session pool, tool discovery
  org_tools/          # Per-org custom webhook tools: CRUD, caching, execution
  memory.py           # Per-org pgvector memory: LLM extraction, recall, CRUD
  agent/              # LangGraph graph, nodes, LLM factory, runtime context/events
  agent_runtime.py    # Agent run orchestration (scheduled/event-triggered runs)
  mcp_gateway.py      # MCP gateway middleware (direct tool invocation + billing)
tools/
  registry.py         # ToolRegistry: versioned, with deprecation lifecycle
  mcp_server.py       # Standalone MCPServer for MCP protocol clients
  definitions/        # One file per tool (calculate, get_datetime, web_search, …)
migrations/
  runner.py           # Applies SQL migrations in order
  versions/           # 001_baseline through 088_withdrawal_in_flight
shared/               # Internal shared utilities: db pool registry, audit inserts, webhook caller
scripts/              # generate_keys.py, seed_users.py, audit_dependencies.py, export_api_spec.py, …
```

For the complete architectural design, execution flow state diagrams, SSE stream event descriptions, and structured A2UI component schemas, consult the [docs/architecture.md](docs/architecture.md) reference.

---

## Coinbase Developer Platform Integration

Teardrop can provision per-org USDC wallets via Coinbase Developer Platform (CDP) for receiving delegation payments and marketplace earnings. This requires a CDP account ([cdp.coinbase.com](https://cdp.coinbase.com)) and an API key with `wallet:create` permission (Key ID, Key Secret, and Wallet Secret). Configure via the `AGENT_WALLET_*` and `CDP_*` environment variables (see [docs/configuration.md](docs/configuration.md)). CDP charges $0.005 per operation; the free tier includes 5,000 ops/month.

Each org can hold one wallet per chain (e.g., Base Sepolia testnet, Base mainnet). Wallets auto-receive delegation payments and MCP marketplace earnings.

---

## License

Teardrop is licensed under the [Business Source License 1.1](LICENSE).

- **Free to use** for non-production evaluation and development.
- **Commercial production use** requires a commercial license from the maintainer.
- **Change Date:** April 3, 2030 — on this date the code automatically converts
  to [AGPL-3.0-only](https://www.gnu.org/licenses/agpl-3.0.html).

See [LICENSE](LICENSE) for full terms. For commercial licensing enquiries, see
the contact address in the LICENSE file.

Contributions are welcome under the same license — see [CONTRIBUTING.md](CONTRIBUTING.md).
To report a security vulnerability, see [SECURITY.md](SECURITY.md).
