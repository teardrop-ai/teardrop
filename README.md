# teardrop
Teardrop is a streaming AI agent API. You send it a message; it reasons using your configured LLM (Anthropic, OpenAI, Google, or OpenRouter), optionally calls tools, builds a structured UI component tree, and streams everything back as Server-Sent Events. It implements four open protocols simultaneously: **AG-UI** (streaming events), **A2A** (agent discoverability), **MCP** (tool serving), and **x402** (per-request payments in USDC on Base, no subscription required).

---

## Core Features

### Agent-to-Agent (A2A) Delegation

Agents can securely delegate tasks to other agents via the `delegate_to_agent` tool (invoked during `/agent/run`). Features:
- **Allowlist control**: Restrict which agents your org can delegate to
- **JWT forwarding**: Automatically forward authentication context when delegating (set `jwt_forward: true` on agent rules)
- **Per-run quotas**: Limit delegation calls per agent run (configurable via `A2A_DELEGATION_MAX_PER_RUN`)
- **Optional billing**: Debit credits for delegations with per-agent cost caps plus org pause and 24h spend-limit enforcement

**Environment variables:**
```
A2A_INBOUND_ENABLED=true                         # Enable public inbound POST /message:send
A2A_INBOUND_TIMEOUT_SECONDS=60                  # Agent execution timeout for inbound A2A calls
A2A_DELEGATION_ENABLED=true                      # Enable agent-to-agent delegation
A2A_DELEGATION_REQUIRE_ALLOWLIST=true            # Enforce allowlist (default: true)
A2A_DELEGATION_MAX_PER_RUN=3                     # Max delegations per run (default: 3)
A2A_DELEGATION_BILLING_ENABLED=true              # Debit credits for delegations
A2A_DELEGATION_MAX_COST_USDC=100000              # Global delegation cost cap (atomic)
A2A_DELEGATION_PLATFORM_FEE_BPS=500              # Platform fee in basis points (5%)
A2A_DELEGATION_TIMEOUT_SECONDS=120               # HTTP timeout for delegation calls
```

### Platform Tool Marketplace

Teardrop exposes 25+ built-in, metered tools through the marketplace catalog. Callers can invoke them:
- Via the **MCP gateway** at `GET /tools/mcp` (direct tool invocation, billed per call).
- As **tools called during agent runs** (via `POST /agent/run` when the agent decides to use them; billed in the run's usage cost).

For the complete list of tools, detailed descriptions, and their per-call prices, please refer to the [docs/tools-catalog.md](docs/tools-catalog.md).

Note: For agent runs, `tool_pricing_overrides` takes precedence over marketplace catalog prices when both exist for the same tool.

Enable with `MARKETPLACE_ENABLED=true`. When enabled:
- Tools appear in `GET /marketplace/catalog` with `qualified_name = "platform/{tool_name}"` and `tool_type = "platform"`
- Public catalog responses include `category`, `total_calls`, `health_status`, `is_healthy`, `display_name`, `tool_name`, and the full `input_schema`
- Catalog discovery supports `category` filtering, `sort=popularity`, single-tool detail pages at `GET /marketplace/catalog/{org_slug}/{tool_name}`, author profiles at `GET /marketplace/authors/{org_slug}`, and LLM-friendly discovery at `GET /marketplace/llms.txt`
- Marketplace authors can register external MCP servers and turn discovered tools into listings via `POST /marketplace/import/preview` and admin-only `POST /marketplace/import/publish`; publish may omit `input_schema` and `output_schema` to reuse the server's normalized discovery result
- Platform tools are always available during agent runs and are not subscribable via `POST /marketplace/subscriptions`
- Agent runs that call these tools incur their marketplace prices (in addition to token costs)
- Per-org pricing overrides are supported via `POST /admin/pricing/tools`; overrides apply to both MCP gateway and agent run calls

---

### Marketplace Settlement & USDC Sweeping

Organizations can monetize their agents via a Marketplace. Earned fees are settled to organization wallets on-chain via Coinbase Developer Platform (CDP).

**Auto-sweep settings** (configure in `.env`):
```
MARKETPLACE_SETTLEMENT_CDP_ACCOUNT=td-marketplace   # CDP account for settlement transfers
MARKETPLACE_SETTLEMENT_CHAIN_ID=84532               # Chain ID: 84532=Base Sepolia (testnet), 8453=Base mainnet (production)
MARKETPLACE_TX_CONFIRM_TIMEOUT_SECONDS=90          # Timeout for on-chain tx receipt (90s for mainnet congestion tolerance)
MARKETPLACE_AUTO_SWEEP_ENABLED=true                # Enable automatic earnings sweep
MARKETPLACE_SWEEP_INTERVAL_SECONDS=86400           # Sweep cadence (86400 = 1 day)
```

**Admin APIs:**
- `POST /admin/marketplace/sweep` — Manually trigger earnings sweep for pending orgs
- `GET /admin/marketplace/settlement-balance` — Query the settlement wallet USDC balance

When an org requests a withdrawal, Teardrop:
1. Settles earned fees to a ledger entry (pending)
2. Attempts on-chain USDC transfer via CDP to the org's specified address
3. Records the tx_hash on success, or reverts to pending on failure

---

### Verified-Email Onboarding Credit

Teardrop can grant a small prepaid credit balance to newly verified organizations so a first agent run is possible without immediately setting up a wallet or card. This is **disabled by default** and is intended only as a conversion aid, not as a source of withdrawable marketplace earnings.

**How it works:**
- The grant is awarded only after a user successfully consumes a single-use email verification token (`GET /auth/verify-email`).
- Token consumption, marking the user verified, and enqueueing onboarding-credit eligibility are committed atomically in one transaction, so a valid token is never "spent" without a durable record of the org's eligibility.
- The grant amount is immutable and recorded in `org_onboarding_credit_grants`.
- The balance, ledger row, and grant marker are written in one transaction.
- Duplicate verifications are harmless: the grant marker makes the operation idempotent.
- If the immediate grant attempt fails (e.g. a transient DB error), the request still returns `{ "verified": true }` and the failure is logged at warning level without a traceback. The durable outbox row created during verification ensures a background worker (`ONBOARDING_CREDIT_RETRY_INTERVAL_SECONDS`, default 60s) retries the grant until it succeeds — the credit can never be permanently lost due to a transient failure.

**Restrictions on promotional credit:**
- Promotional credit can be used for platform tools, org webhook tools, MCP tools, and the base agent run cost.
- It **cannot** call marketplace author tools (qualified names such as `acme/weather`) through `POST /agent/run`, the SSE streaming route, `GET /tools/mcp`, or `POST /mcp/v1`.
- Marketplace author earnings and usage statistics are suppressed for promotional runs even if a tool bypasses planner filtering.
- A real top-up (Stripe, on-chain USDC, admin top-up, or refund) converts the org to normal credit status and removes the marketplace restriction.
- x402-paid SIWE calls are unaffected and never treated as promotional.

**Environment variables:**
```
ONBOARDING_CREDIT_ENABLED=false   # Set to true to enable verified-email grants
ONBOARDING_CREDIT_USDC=500000     # Default $0.50 in atomic USDC (max $10.00)
ONBOARDING_CREDIT_RETRY_INTERVAL_SECONDS=60  # Background retry poll interval for failed grants
```

Check `GET /billing/balance` after verification to see the granted balance. Once the promotional balance is exhausted, use the existing Stripe or on-chain USDC top-up flows.

---

### Unattended (Scheduled) Agent Runs

Organizations can schedule recurring, unattended agent runs with integrated credit-only billing, stored execution history, and real-time status callbacks.

**Scheduled runs settings** (configure in `.env` or system environment):
```
SCHEDULED_RUNS_ENABLED=true                         # Toggle unattended scheduled run background worker
SCHEDULED_RUNS_TICK_INTERVAL_SECONDS=60             # Polling interval in seconds to claim due runs
SCHEDULED_RUNS_MIN_INTERVAL_SECONDS=300             # Minimum interval in seconds (default: 5 minutes)
SCHEDULED_RUNS_MAX_PER_ORG=20                       # Max schedules allowed per org (active + inactive)
SCHEDULED_RUNS_MAX_CONSECUTIVE_FAILURES=5           # Auto-disable consecutive execution failures threshold
SCHEDULED_RUNS_EXECUTION_TIMEOUT_SECONDS=120        # Timeout in seconds for a single scheduled agent execution
SCHEDULED_RUNS_MAX_CONCURRENCY=4                    # Max scheduled executions run concurrently per tick (<= pg pool headroom)
```

**Developer APIs (under `/agent/schedules`):**
- `POST /agent/schedules` — Register a new scheduled prompt and interval
- `GET /agent/schedules` — List current schedules for the authenticated org
- `GET /agent/schedules/{id}` — Get single schedule configuration
- `PATCH /agent/schedules/{id}` — Partially update schedule properties (e.g. toggle `enabled` or adjust `interval_seconds`)
- `DELETE /agent/schedules/{id}` — Permanently delete a schedule definition
- `GET /agent/schedules/{id}/runs` — Query run results (with cursor-based pagination)

When scheduled executions are due, the worker claims a batch using a row-locking query (`FOR UPDATE SKIP LOCKED`) and advances each run's `next_run_at` atomically at claim time. Claimed runs execute concurrently up to `SCHEDULED_RUNS_MAX_CONCURRENCY`, with per-run failure isolation so one error never aborts the rest of the batch. Each execution prepares a dedicated agent thread, verifies credits, and runs the agent. Completed, failed, timed-out, and credit-skipped runs are archived under `scheduled_run_results`. If configured, execution results are dispatched to an HTTPS-only, SSRF-checked callback URL. Because claims use `SKIP LOCKED`, multiple worker instances can run side by side to scale throughput horizontally.

---

### Event-Triggered (Reactive) Runs

Beyond fixed intervals, organizations can register **event triggers** that run the agent in response to inbound webhooks (e.g. an on-chain event, a CRM update, a CI signal). An event trigger stores a prompt *template*; the inbound JSON payload is interpolated into it at dispatch time. Event triggers reuse the same execution core, credit billing, result history, and callback delivery as scheduled runs.

**Event-trigger settings** (configure in `.env` or system environment):
```
EVENT_TRIGGERS_ENABLED=true                         # Toggle reactive event-triggered runs (inbound ingress)
EVENT_TRIGGERS_MAX_PER_ORG=20                       # Max event triggers allowed per org (active + inactive)
EVENT_TRIGGERS_MAX_CONCURRENCY=8                    # Max in-flight inbound runs per process (beyond this → HTTP 429)
EVENT_TRIGGERS_PROMPT_MAX_CHARS=12000               # Max rendered prompt length after payload interpolation
```

**Management APIs (org-scoped JWT, under `/agent/event-triggers`):**
- `POST /agent/event-triggers` — Create a trigger; the signing **secret is returned once** (only its SHA-256 hash is stored)
- `GET /agent/event-triggers` — List triggers for the authenticated org
- `GET /agent/event-triggers/{id}` — Get a single trigger
- `PATCH /agent/event-triggers/{id}` — Update name, prompt template, `enabled`, or `callback_url`
- `DELETE /agent/event-triggers/{id}` — Delete a trigger
- `POST /agent/event-triggers/{id}/rotate-secret` — Rotate the signing secret (returns the new secret once)
- `GET /agent/event-triggers/{id}/runs` — Query run results (cursor pagination)

**Inbound dispatch (public, secret-authenticated):**
- `POST /agent/events/{trigger_token}` — Fire the trigger. Authenticate with the per-trigger secret via the `X-Teardrop-Trigger-Secret` header (constant-time compared). Optional `X-Idempotency-Key` (or an `idempotency_key` body field) gives at-most-once execution across webhook retries. The JSON body is rendered into the prompt template via `{{field}}` placeholders (`{{event_json}}` injects the full payload); substitution is scalar-only and length-capped to resist prompt/format-string injection. The agent runs in the background and the endpoint returns `202 Accepted` with a `run_id`; results are retrievable via the runs endpoint and the optional callback. Inbound load is bounded by `EVENT_TRIGGERS_MAX_CONCURRENCY` (returns `429` when saturated) and a 64 KB payload cap.

Prompt templates interpolate untrusted payload data, so treat rendered prompts as untrusted input to the agent — scope event-trigger tools and credit limits accordingly.

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
# Default model is deepseek/deepseek-v4-flash.
# For OpenRouter DeepSeek models, Teardrop pins provider routing to NovitaAI/DeepInfra.
OPENROUTER_API_KEY=sk-or-...      # required if AGENT_PROVIDER=openrouter
# ANTHROPIC_API_KEY=sk-ant-...     # required if AGENT_PROVIDER=anthropic
# OPENAI_API_KEY=sk-...           # required if AGENT_PROVIDER=openai
# GOOGLE_API_KEY=...              # required if AGENT_PROVIDER=google

DATABASE_URL=postgresql://teardrop:teardrop@localhost:5432/teardrop

# Optional: Redis for distributed caching
# REDIS_URL=redis://localhost:6379/0
```

**5. Generate RSA keys**
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

Teardrop issues RS256 JWTs. All endpoints (except `/health`, `/docs`, `/billing/pricing`, `/.well-known/agent-card.json`, and the public payment-gated `POST /message:send` A2A endpoint when `A2A_INBOUND_ENABLED=true`) require a `Bearer` token.

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

SIWE tokens are the only auth method that can use x402 on-chain payments. New wallet addresses are auto-registered on first login.

### Token expiry and refresh tokens

All three auth methods issue access tokens with a **30-minute expiry** (`expires_in: 1800` seconds in the token response). For applications that need sessions longer than 30 minutes, use refresh tokens:

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

# Set provider + model + routing strategy
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

### Example: BYOK — use your own OpenAI key

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/llm-config" `
    -Method Put -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token" } `
    -Body @{
        provider = "openai"
        model = "gpt-4o"
        api_key = "sk-..."  # your key (encrypted at rest)
    } | ConvertTo-Json
```

### Example: Self-hosted vLLM or Ollama

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/llm-config" `
    -Method Put -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token" } `
    -Body @{
        provider = "openai"
        model = "meta-llama/Llama-2-7b-chat"
        api_base = "http://gpu-cluster.internal:8000/v1"
        api_key = "your-local-key-or-token"
    } | ConvertTo-Json
```

### Routing preferences

When you set `routing_preference` to a value other than `"default"`, Teardrop will automatically select a model from its standard pool based on your criteria:

| Preference | Behavior |
|------------|----------|
| `default` | Use the provider/model you configured |
| `cost` | Select the cheapest model (by tokens-in + tokens-out pricing) |
| `speed` | Select the fastest model (by p95 latency from live benchmarks; falls back to official specs for new deployments) |
| `quality` | Select the highest quality model (Claude Sonnet > Claude Haiku, etc.) |

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

### Example response

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/models/benchmarks" | ConvertTo-Json | ForEach-Object { $_ | Out-String }
```

Returns:
```json
{
  "models": [
    {
      "provider": "anthropic",
      "model": "claude-haiku-4-5-20251001",
      "display_name": "Claude Haiku 4.5",
      "context_window": 200000,
      "supports_tools": true,
      "quality_tier": 2,
      "pricing": {
        "tokens_in_cost_per_1k": 0.80,
        "tokens_out_cost_per_1k": 4.00,
        "tool_call_cost": 0.001
      },
      "benchmarks": {
        "total_runs_7d": 156,
        "avg_latency_ms": 450.2,
        "p95_latency_ms": 1200.5,
        "avg_cost_usdc_per_run": 0.015,
        "avg_tokens_per_sec": 52.3
      }
    },
    ...
  ],
  "updated_at": "2026-04-16T14:22:00Z"
}
```

### Understanding the metrics

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

```
Client                         Teardrop                     x402 Facilitator
  │                               │                               │
  │── POST /agent/run ───────────►│                               │
  │   (no payment header)         │                               │
  │◄── 402 Payment Required ──────│                               │
  │    PAYMENT-REQUIRED: <v2>     │                               │
  │    X-PAYMENT-REQUIRED: <reqs> │                               │
  │                               │                               │
  │── POST /agent/run ───────────►│                               │
  │   Payment-Signature: <signed> │── verify payment ────────────►│
  │                               │◄─ verified ───────────────────│
  │◄── SSE stream ────────────────│                               │
  │   (TEXT, TOOL, SURFACE...)    │                               │
  │   BILLING_SETTLEMENT          │── settle (actual cost*) ─────►│
  │   { tx_hash, amount_usdc }    │◄─ tx confirmed ───────────────│
```

\* **exact**: settles the signed amount. **upto**: settles actual usage cost ≤ client-signed ceiling.

### Payment methods by auth type

| Auth method | Payment mechanism |
|-------------|-------------------|
| `siwe` | x402 on-chain (USDC, `exact` or `upto` scheme, per-request) |
| `client_credentials` | Org prepaid credit balance (off-chain debit) |
| `email` | Org prepaid credit balance (off-chain debit) |

### x402 payment schemes

| Scheme | How it works | Config |
|--------|-------------|--------|
| `exact` (default) | Client signs the exact run price; facilitator settles that amount. | `X402_SCHEME=exact` |
| `upto` | Client signs a ceiling (`X402_UPTO_MAX_AMOUNT`); after the run, Teardrop settles the actual usage cost (≤ ceiling) via Permit2. | `X402_SCHEME=upto` |

### Configuration

```
BILLING_ENABLED=true
X402_PAY_TO_ADDRESS=0xYourTreasuryWallet
X402_NETWORK=eip155:8453          # Base mainnet (eip155:84532 = Base Sepolia)
X402_FACILITATOR_URL=https://x402.org/facilitator
X402_SCHEME=upto                  # "exact" (default) or "upto" (usage-based settlement)
X402_UPTO_MAX_AMOUNT=$0.50        # Max ceiling per-run for upto (ignored for exact)
BILLABLE_AUTH_METHODS=["siwe", "client_credentials", "email"]    # All three auth methods billed by default
```

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

> **upto client requirement**: Before using `X402_SCHEME=upto`, the paying wallet must approve Permit2 for USDC on the target chain once:
> `IERC20(USDC).approve(PERMIT2_ADDRESS, type(uint256).max)`. This is a one-time on-chain transaction per wallet. Clients that have not approved Permit2 can always use `scheme: exact` from the `accepts` array in the 402 response as a fallback.

### Credit top-up (machine callers)

Admins can add prepaid USDC credit to an org's balance:

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/admin/credits/topup" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $adminToken" } `
    -Body '{"org_id":"org-123","amount_usdc":1000000}'   # $1.00
```

---

## A2A Delegation & Cross-Agent Revenue Routing

Teardrop agents can delegate tasks to remote A2A-compliant agents and charge those delegations back to the calling organization. This unlocks decentralized specialisation, built-in spend orchestration, and automatic revenue routing.

For a detailed protocol specification, system configurations, allowlists, and billing event payload structures, see the [docs/a2a-delegation.md](docs/a2a-delegation.md) guide.

---

## Publishing to Agentic Market

Use the full public URL of the paid A2A surface, for example `https://api.teardrop.dev/message:send`, with method `POST`.

Do not register the bare origin `https://api.teardrop.dev/`. `GET /` redirects to `/docs`, and `POST /` is not a paid Teardrop entrypoint.

Agentic Market validators may probe `/.well-known/x402`, `/.well-known/x402.json`, and the configured endpoint URL. Teardrop serves those discovery aliases publicly and issues the x402 challenge on unpaid anonymous `POST /message:send` requests before body validation.

That challenge now includes:

- `PAYMENT-REQUIRED` with the full x402 v2 `PaymentRequired` envelope.
- `X-PAYMENT-REQUIRED` as a legacy alias for older clients that only decode the requirement list.
- Top-level `resource.url` describing the paid surface.
- Top-level `extensions.bazaar` describing the A2A request and response shape.

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

Teardrop utilizes a LangGraph state machine paired with a streaming Server-Sent Events (SSE) framework. 

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

Set `DATABASE_URL` to your Neon connection string (no `+asyncpg` prefix needed — the app strips it automatically).

---

## Project structure

```
app.py              # FastAPI app, lifespan, middleware, background workers, router registration
routers/            # APIRouter modules (agent.py: POST /agent/run SSE + GET /agent/tools)
agent_stream.py     # AG-UI SSE framing and A2UI stream-filter helpers
auth.py             # RS256 JWT & refresh tokens (auth methods: email, client_credentials, SIWE)
billing/            # x402 billing layer, pricing, invoice queries, credit system
cache.py            # Redis cache helpers
config.py           # Settings via pydantic-settings (reads .env)
memory.py           # Per-org pgvector memory: LLM extraction, recall, CRUD
mcp_client/         # Per-org MCP client: CRUD, session pool, tool discovery
org_tools/          # Per-org custom webhook tools: CRUD, caching, execution
usage.py            # UsageEvent model, usage recording and aggregation
users.py            # Org + User models, CRUD, PBKDF2-SHA256 password hashing
wallets.py          # User wallet management, SIWE nonce lifecycle
agent_wallets.py    # CDP-backed agent wallet provisioning, balance queries, audit
agent/
  graph.py          # LangGraph StateGraph definition and routing
  llm.py            # Multi-provider LLM factory (Anthropic, OpenAI, Google, OpenRouter)
  nodes.py          # planner, tool_executor, ui_generator implementations
  state.py          # AgentState, A2UIComponent, TaskStatus schemas
tools/
  registry.py       # ToolRegistry: versioned, with deprecation lifecycle
  mcp_server.py     # Standalone FastMCP server for MCP protocol clients
  definitions/      # One file per tool (calculate, get_datetime, web_search, …)
  __init__.py       # Global registry singleton, get_langchain_tools()
migrations/
  runner.py         # Applies SQL migrations in order
  versions/         # 001_baseline through 039_new_model_pricing_seed
shared/             # Internal shared utilities: db pool registry, audit inserts, webhook caller
scripts/
  generate_keys.py  # Generate RSA keypair → keys/private.pem + keys/public.pem
  audit_dependencies.py  # Review direct Python dependencies for OSV vulnerabilities and upgrade drift
  init_neon.py      # Initialize Neon Postgres schema
  seed_users.py     # Create default org + admin user for local dev
```

---

## Coinbase Developer Platform Integration

Teardrop can provision per-org USDC wallets via Coinbase Developer Platform (CDP) for receiving delegation payments and marketplace earnings. This requires:

1. **CDP Account**: Create one at [https://cdp.coinbase.com](https://cdp.coinbase.com)
2. **API Credentials**:
   - Go to Developer Settings → API Keys
   - Create a key with `wallet:create` permission
   - Note the **Key ID** and **Key Secret** (Ed25519 or ECDSA)
   - Note the **Wallet Secret** (used to decrypt keys stored in AWS Nitro Enclaves)
3. **Environment variables**:
   ```
   AGENT_WALLET_ENABLED=true
   CDP_API_KEY_ID=<your-key-id>
   CDP_API_KEY_SECRET=<your-key-secret>
   CDP_WALLET_SECRET=<your-wallet-secret>
   CDP_NETWORK=base-sepolia       # or 'base' for mainnet
   AGENT_WALLET_MAX_BALANCE_USDC=100000000  # $100 balance cap (optional)
   ```
4. **Pricing**: CDP charges $0.005 per operation. Free tier includes 5,000 ops/month.

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
