# teardrop
Teardrop is a streaming AI agent API. You send it a message; it reasons using your configured LLM (Anthropic, OpenAI, Google, or OpenRouter), optionally calls tools, builds a structured UI component tree, and streams everything back as Server-Sent Events. It implements four open protocols simultaneously: **AG-UI** (streaming events), **A2A** (agent discoverability), **MCP** (tool serving), and **x402** (per-request payments in USDC on Base, no subscription required).

---

## Core Features

### Agent-to-Agent (A2A) Delegation

Agents can securely delegate tasks to other agents via `POST /delegate` or the `delegate_to_agent` tool. Features:
- **Allowlist control**: Restrict which agents your org can delegate to
- **JWT forwarding**: Automatically forward authentication context when delegating (set `jwt_forward: true` on agent rules)
- **Per-run quotas**: Limit delegation calls per agent run (configurable via `A2A_DELEGATION_MAX_PER_RUN`)
- **Optional billing**: Debit credits for delegations with per-agent cost caps plus org pause and 24h spend-limit enforcement

**Environment variables:**
```
A2A_DELEGATION_ENABLED=true                      # Enable agent-to-agent delegation
A2A_DELEGATION_REQUIRE_ALLOWLIST=true            # Enforce allowlist (default: false)
A2A_DELEGATION_MAX_PER_RUN=3                     # Max delegations per run (default: 3)
A2A_DELEGATION_BILLING_ENABLED=true              # Debit credits for delegations
A2A_DELEGATION_MAX_COST_USDC=100000              # Global delegation cost cap (atomic)
A2A_DELEGATION_PLATFORM_FEE_BPS=500              # Platform fee in basis points (5%)
AGENT_MAX_TOOL_ITERATIONS=4                       # Max planner→tool cycles before forced synthesis (default: 4)
AGENT_TOOL_BILLING_ENABLED=true                # Only bill successful/complete tool calls (True/False)
AGENT_TOOL_MAX_CALLS_PER_RUN={"get_yield_rates":1,"resolve_ens":1}
```

### Platform Tool Marketplace

Teardrop exposes built-in, metered tools through the marketplace catalog. Callers can invoke them:
- Via the **MCP gateway** at `GET /tools/mcp` (direct tool invocation, billed per call).
- As **tools called during agent runs** (via `POST /agent/run` when the agent decides to use them; billed in the run's usage cost).

Pricing is fixed per call in atomic USDC (1,000,000 = $1.00):

| Tool | Price/call |
|------|------------|
| `get_wallet_portfolio` | $0.004 (4,000 atomic) |
| `web_search` | $0.010 (10,000 atomic) |
| `get_token_price` | $0.002 (2,000 atomic) |
| `get_token_price_historical` | $0.004 (4,000 atomic) |
| `get_protocol_tvl` | $0.003 (3,000 atomic) |
| `get_yield_rates` | $0.004 (4,000 atomic) |
| `http_fetch` | $0.002 (2,000 atomic) |
| `convert_currency` | $0.002 (2,000 atomic) |
| `get_eth_balance` | $0.001 (1,000 atomic) |
| `get_erc20_balance` | $0.002 (2,000 atomic) |
| `get_block` | $0.001 (1,000 atomic) |
| `get_transaction` | $0.002 (2,000 atomic) |

Enable with `MARKETPLACE_ENABLED=true`. When enabled:
- Tools appear in `GET /marketplace/catalog` with `qualified_name = "platform/{tool_name}"`
- Agent runs that call these tools incur their marketplace prices (in addition to token costs)
- Per-org pricing overrides are supported via `POST /admin/pricing/tools`; overrides apply to both MCP gateway and agent run calls

---

### Marketplace Settlement & USDC Sweeping

Organizations can monetize their agents via a Marketplace. Earned fees are settled to organization wallets on-chain via Coinbase Developer Platform (CDP).

**Auto-sweep settings** (configure in `.env`):
```
MARKETPLACE_SETTLEMENT_CDP_ACCOUNT=td-marketplace   # CDP account for settlement transfers
MARKETPLACE_SETTLEMENT_CHAIN_ID=8453                # Chain ID: 8453=Base mainnet, 84532=Base Sepolia (testnet)
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
uvicorn app:app --reload
```

Server starts at `http://localhost:8000`. Visit `http://localhost:8000/docs` for the interactive API explorer.

---

## Deployment

### Docker (local full stack)

```powershell
docker-compose up --build
```

Starts Postgres + Teardrop API. Migrations run automatically at startup. Keys are generated at build time and mounted from `./keys/`.

### Render (production)

The repo includes a `render.yaml` that configures a Render web service. Set these environment variables in the Render dashboard:

| Variable | Description |
|----------|-------------|
| `AGENT_PROVIDER` | `anthropic`, `openai`, `google`, or `openrouter` (default: `openrouter`) |
| `AGENT_MODEL` | Optional global model override (default: `deepseek/deepseek-v4-flash`). When using OpenRouter DeepSeek models, provider routing is pinned to `NovitaAI` and `DeepInfra`. |
| `AGENT_SINGLE_TOOL_TIMEOUT_SECONDS` | Per-tool deadline in seconds (default: `30`). Slow tools are converted into timeout tool messages so synthesis proceeds with partial data. |
| `ANTHROPIC_API_KEY` | Required if `AGENT_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | Required if `AGENT_PROVIDER=openai` |
| `GOOGLE_API_KEY` | Required if `AGENT_PROVIDER=google` |
| `DATABASE_URL` | Neon Postgres connection string |
| `BILLING_ENABLED` | `true` to activate x402 payments |
| `X402_PAY_TO_ADDRESS` | Treasury wallet (USDC recipient) |
| `X402_NETWORK` | `eip155:8453` for Base mainnet |
| `X402_SCHEME` | Payment scheme: `exact` (default) or `upto` (usage-based via Permit2) |
| `X402_UPTO_MAX_AMOUNT` | Max ceiling per run for upto scheme (default: `$0.50`) |
| `SIWE_DOMAIN` | Your public domain (e.g. `api.teardrop.dev`) |
| `CORS_ORIGINS` | Comma-separated allowed origins |
| `AGENT_WALLET_ENABLED` | `true` to enable per-org CDP-backed wallets |
| `CDP_API_KEY_ID` | Coinbase Developer Platform API key ID |
| `CDP_API_KEY_SECRET` | CDP API key secret (Ed25519 / ECDSA) |
| `CDP_WALLET_SECRET` | CDP wallet secret (decrypts TEE-stored keys) |
| `CDP_NETWORK` | CDP network: `base-sepolia` (testnet) or `base` (mainnet) |
| `AGENT_WALLET_MAX_BALANCE_USDC` | Max USDC per agent wallet (default: 100000000 = $100) |
| `MARKETPLACE_ENABLED` | `true` to activate the tool marketplace catalog and platform tool billing in the MCP gateway |
| `MARKETPLACE_SETTLEMENT_CDP_ACCOUNT` | CDP account name for settlement transfers (default: `td-marketplace`) |
| `MARKETPLACE_SETTLEMENT_CHAIN_ID` | Chain for USDC sweeps: `8453` = Base mainnet (production), `84532` = Base Sepolia (testnet). Must match `CDP_NETWORK`. |
| `MARKETPLACE_TX_CONFIRM_TIMEOUT_SECONDS` | Seconds to wait for on-chain tx receipt after CDP transfer (default: `90`). Base mainnet can experience 60–90s delays under congestion. |
| `MARKETPLACE_AUTO_SWEEP_ENABLED` | `true` to auto-sweep org earnings on a schedule |
| `MARKETPLACE_SWEEP_INTERVAL_SECONDS` | Sweep cadence in seconds (default: `86400` = 1 day) |
| `MARKETPLACE_CATALOG_URL` | Public URL of the marketplace catalog used in tool-deactivation emails (optional) |
| `TOOL_BREAKER_ENABLED` | `true` to auto-deactivate marketplace tools whose webhooks repeatedly fail (default: `true`) |
| `TOOL_BREAKER_THRESHOLD` | Consecutive failures within the window that trip the breaker (default: `5`) |
| `TOOL_BREAKER_WINDOW_SECONDS` | Sliding-window duration in seconds for failure counting (default: `600`) |
| `BYOK_TIER_PRICING_ENABLED` | `true` to use per-token orchestration pricing for BYOK orgs (seeded by migration 041). When `false`, uses legacy flat `byok_platform_fee_usdc`. Default: `false` for backward compatibility. |
| `OPENROUTER_API_KEY` | Required if `AGENT_PROVIDER=openrouter` |
| `COINGECKO_API_KEY` | CoinGecko API key for live price data (optional; rate-limited without key) |
| `ORG_TOOL_ENCRYPTION_KEY` | Fernet key for encrypting webhook `auth_header_value` at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `LLM_CONFIG_ENCRYPTION_KEY` | Fernet key for encrypting BYOK API keys at rest (same format as above) |
| `REQUIRE_EMAIL_VERIFICATION` | `true` to require email verification before login (default: `false`) |
| `RESEND_API_KEY` | Resend API key for sending verification / invite emails |
| `RESEND_FROM_EMAIL` | Sender address for transactional emails (e.g. `noreply@yourdomain.com`) |
| `APP_BASE_URL` | Public URL of this deployment (used in email links, e.g. `https://api.teardrop.dev`) |
| `MARKETPLACE_DEFAULT_REVENUE_SHARE_BPS` | Author revenue share in basis points (default: `7000` = 70% to author, 30% to platform). Hard-coded split; per-author overrides are not supported. |
| `MCP_AUTH_ENABLED` | `true` to require authentication on the `/tools/mcp` MCP gateway |
| `MCP_AUTH_AUDIENCE` | JWT audience for MCP gateway tokens (default: `teardrop-mcp`) |
| `MCP_BILLING_ENABLED` | `true` to enable credit billing for MCP tool calls |
| `MCP_X402_ENABLED` | `true` to accept x402 payments on the MCP gateway |

---

## Authentication

Teardrop issues RS256 JWTs. All endpoints (except `/health`, `/docs`, `/billing/pricing`, `/.well-known/agent-card.json`) require a `Bearer` token.

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

---

## LLM Configuration (Per-Org)

Organizations can configure their preferred LLM provider, model, routing strategy, and optionally bring their own API keys (BYOK). This unlocks:

- **Multi-provider choice**: Use Anthropic, OpenAI, Google, or point at self-hosted endpoints (vLLM, Ollama, OpenRouter)
- **Bring Your Own Key (BYOK)**: Encrypt and store your own API credentials — Teardrop never sees your keys. BYOK orgs pay platform fees for orchestration (per-token when `BYOK_TIER_PRICING_ENABLED=true`, or flat fee otherwise) in addition to their LLM provider costs.
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

**BYOK Platform Fee**: BYOK orgs are charged a flat per-run infrastructure fee (`BYOK_PLATFORM_FEE_USDC`, default `1000` = $0.001) instead of LLM token costs. The fee appears as `platform_fee_usdc` in usage events and SSE billing events.

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

Teardrop agents can delegate specialist tasks to remote A2A-compliant agents and charge those delegations back to the calling organisation. This enables:

- **Network effect**: Agents discover and call each other via published Agent Cards
- **Specialisation**: Route complex tasks to domain-expert agents
- **Revenue sharing**: Collect payments from delegations and distribute to specialist agent operators
- **Budget control**: Per-agent cost caps, global delegation spending limits, and org-level pause/daily spend checks

### How it works

```
Local Agent                     Teardrop                          Remote Agent
  │                               │                                    │
  │ calls delegate_to_agent ──────│                                    │
  │  + agent_url                  │                                    │
  │  + task_description           │                                    │
  │                               │─ GET /.well-known/agent-card.json ►│
  │                               │◄─ agent capabilities ──────────────│
  │                               │                                    │
  │                               │─ POST /message:send ──────────────►│
  │                               │   (with optional x402 payment)      │
  │                               │◄─ task result ────────────────────│
  │◄──────────  result ───────────│                                    │
  │  + cost_usdc (debited)        │                                    │
  │                               │                                    │
  └─ Credits debited from org ────│                                    │
```

### Configuration

```
# Enable A2A delegation
A2A_DELEGATION_ENABLED=true
A2A_DELEGATION_TIMEOUT_SECONDS=120
A2A_DELEGATION_MAX_PER_RUN=3         # Max delegations per agent run
AGENT_MAX_TOOL_ITERATIONS=4                       # Max planner?tool cycles before forced synthesis (default: 4)
AGENT_TOOL_BILLING_ENABLED=true                # Only bill successful/complete tool calls (True/False)
AGENT_TOOL_MAX_CALLS_PER_RUN={"get_yield_rates":1,"resolve_ens":1}

# Enable billing for delegations
A2A_DELEGATION_BILLING_ENABLED=true
A2A_DELEGATION_PLATFORM_FEE_BPS=500  # Platform fee: 500 bps = 5%
A2A_DELEGATION_MAX_COST_USDC=100000  # Global delegation cost cap ($0.10)

# For x402 delegations (optional):
X402_TREASURY_PRIVATE_KEY=0x...      # Treasury wallet private key (hex-encoded)
```

### Allowlist & Budget Control

Organisations must explicitly add remote agents to their allowlist before delegating to them:

```powershell
# Add a trusted agent to the allowlist
$token = (Invoke-RestMethod -Uri "http://localhost:8000/token" `
    -Method Post -ContentType "application/json" `
    -Body '{"client_id":"teardrop-client","client_secret":"<secret>"}').access_token

Invoke-RestMethod -Uri "http://localhost:8000/a2a/agents" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token" } `
    -Body @{
        agent_url = "https://specialist.agents.example.com"
        label = "Code Review Specialist"
        max_cost_usdc = 50_000           # Per-delegation cap: $0.05
        require_x402 = $false            # Use org credits (not x402)
    } | ConvertTo-Json
```

### Payment methods for delegations

| Setting | Billing Method | When to use |
|---------|---|---|
| `require_x402=false` | Org prepaid credits | Default: instant, requires upfront org credit balance |
| `require_x402=true` | x402 on-chain (USDC) | Agent requires on-chain payment; uses treasury wallet to sign |

### Delegation events & audit trail

Every delegation is recorded in the `a2a_delegation_events` table:

```powershell
# List delegation events for your org
Invoke-RestMethod -Uri "http://localhost:8000/a2a/delegations?limit=50" `
    -Method Get `
    -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json
```

Response:
```json
[
  {
    "id": "evt-abc123",
    "run_id": "run-xyz",
    "agent_url": "https://specialist.agents.example.com",
    "agent_name": "CodeReviewBot",
    "task_status": "completed",
    "cost_usdc": 52500,
    "billing_method": "credit",
    "settlement_tx": "",
    "error": null,
    "created_at": "2026-04-16T14:22:00Z"
  }
]
```

### Delegation in SSE stream

When a delegation occurs during an agent run, the final `USAGE_SUMMARY` and `BILLING_SETTLEMENT` events include the delegation cost breakdown:

```json
{
  "event": "USAGE_SUMMARY",
  "data": {
    "run_id": "run-123",
    "tokens_in": 1500,
    "tokens_out": 800,
    "tool_calls": 3,
    "cost_usdc": 15000,
    "delegation_cost_usdc": 52500
  }
}

{
  "event": "BILLING_SETTLEMENT",
  "data": {
    "run_id": "run-123",
    "amount_usdc": 67500,
    "tx_hash": "",
    "network": "credit",
    "delegation_cost_usdc": 52500
  }
}
```

---

## API reference

### Core

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | — | Redirects to `/docs` |
| `GET` | `/health` | — | Liveness probe |
| `POST` | `/agent/run` | Bearer | Main streaming endpoint (SSE) |
| `GET` | `/.well-known/agent-card.json` | — | A2A agent card |
| `GET` | `/docs` | — | Swagger UI |
| `GET` | `/redoc` | — | ReDoc UI |

### LLM Configuration

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/llm-config` | Bearer | Get org's LLM config (or defaults if not set) |
| `PUT` | `/llm-config` | Bearer | Set or update org's LLM configuration |
| `DELETE` | `/llm-config` | Bearer | Delete org config, revert to global defaults |

### Models

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/models/benchmarks` | — | Public: all models with benchmarks |
| `GET` | `/models/benchmarks/org` | Bearer | Org-scoped: benchmarks for your org's usage |

### Auth

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/token` | — | Issue JWT (client-creds, email, or SIWE) |
| `GET` | `/auth/me` | Bearer | Return the authenticated user's identity |
| `GET` | `/auth/siwe/nonce` | — | Generate single-use SIWE nonce |

### Billing

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/billing/pricing` | — | Current pricing rules |
| `GET` | `/billing/history` | Bearer | Settled payment history (cursor paginated) |
| `GET` | `/billing/invoices` | Bearer | All run records including pending (cursor paginated) |
| `GET` | `/billing/invoice/{run_id}` | Bearer | Single run receipt |
| `GET` | `/billing/balance` | Bearer | Org prepaid credit balance |
| `GET` | `/billing/credit-history` | Bearer | Credit ledger — top-ups and debits (cursor paginated) |
| `POST` | `/billing/topup/stripe` | Bearer | Start a Stripe checkout session to add credits |
| `GET` | `/billing/topup/stripe/status` | Bearer | Check Stripe checkout session status |
| `GET` | `/billing/topup/usdc/requirements` | Bearer | Get on-chain USDC top-up payment requirements |
| `POST` | `/billing/topup/usdc` | Bearer | Submit and verify an on-chain USDC top-up |

### Wallets

#### User Wallets (SIWE-linked)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/wallets/link` | Bearer | Link additional wallet via SIWE |
| `GET` | `/wallets/me` | Bearer | List your linked wallets |
| `DELETE` | `/wallets/{wallet_id}` | Bearer | Unlink a wallet |

#### Agent Wallets (CDP-managed, per-org)

Each org can provision a single CDP-backed USDC wallet per chain for receiving delegation payments and marketplace earnings. Enable with `AGENT_WALLET_ENABLED=true` and set CDP credentials.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/wallets/agent` | Bearer | Provision a CDP-backed agent wallet for your org |
| `GET` | `/wallets/agent` | Bearer | Get org's agent wallet; optionally include on-chain USDC balance |
| `DELETE` | `/wallets/agent` | Admin | Deactivate the org's agent wallet |

**Example: Provision an agent wallet**

```powershell
$token = (Invoke-RestMethod -Uri "http://localhost:8000/token" `
    -Method Post -ContentType "application/json" `
    -Body '{"client_id":"teardrop-client","client_secret":"<secret>"}').access_token

Invoke-RestMethod -Uri "http://localhost:8000/wallets/agent" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json
```

**Example: Get agent wallet with balance**

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/wallets/agent?include_balance=true" `
    -Method Get `
    -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json
```

Response includes `balance_usdc` (atomic units, 6 decimals: 50000000 = $50.00).


### Usage

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/usage/me` | Bearer | Aggregated token/tool usage for current user |

### Admin

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/admin/orgs` | Admin | Create organisation |
| `POST` | `/admin/users` | Admin | Create user |
| `POST` | `/admin/client-credentials` | Admin | Create M2M client credentials for an org |
| `GET` | `/admin/usage/{user_id}` | Admin | Usage for a specific user |
| `GET` | `/admin/usage/org/{org_id}` | Admin | Usage for an org |
| `GET` | `/admin/billing/revenue` | Admin | Aggregated revenue summary |
| `POST` | `/admin/credits/topup` | Admin | Add prepaid USDC credits to an org |
| `POST` | `/admin/pricing/tools` | Admin | Create or update a per-tool pricing override |
| `DELETE` | `/admin/pricing/tools/{tool_name}` | Admin | Remove a per-tool pricing override |
| `GET` | `/admin/tools/{org_id}` | Admin | List custom tools for an org |
| `GET` | `/admin/memories/org/{org_id}` | Admin | List memories for an org |
| `DELETE` | `/admin/memories/org/{org_id}` | Admin | Delete all memories for an org |
| `GET` | `/admin/mcp/servers/{org_id}` | Admin | List MCP servers for an org |

### Custom Tools

Per-org webhook-backed tools are injected into the agent at run-time and never appear in the public Agent Card or MCP server.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/tools` | Bearer | Register a custom webhook tool |
| `GET` | `/tools` | Bearer | List org's custom tools |
| `GET` | `/tools/{tool_id}` | Bearer | Get a specific custom tool |
| `PATCH` | `/tools/{tool_id}` | Bearer | Update a custom tool |
| `DELETE` | `/tools/{tool_id}` | Bearer | Delete a custom tool |

### Memory

Per-org persistent memory backed by pgvector. Memories are extracted automatically during agent runs and recalled as context on subsequent turns.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/memories` | Bearer | List org memories (cursor paginated) |
| `POST` | `/memories` | Bearer | Store a memory manually |
| `DELETE` | `/memories/{memory_id}` | Bearer | Delete a specific memory |

### MCP Federation

Connect external MCP servers to your org. Their tools are discovered and made available to the agent alongside the built-in tool set.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/mcp/servers` | Bearer | Register an external MCP server |
| `GET` | `/mcp/servers` | Bearer | List org's MCP servers |
| `GET` | `/mcp/servers/{server_id}` | Bearer | Get a specific MCP server |
| `PATCH` | `/mcp/servers/{server_id}` | Bearer | Update an MCP server |
| `DELETE` | `/mcp/servers/{server_id}` | Bearer | Remove an MCP server |
| `POST` | `/mcp/servers/{server_id}/discover` | Bearer | Trigger tool re-discovery from an MCP server |

### A2A Delegation

Agent allowlist and delegation history. Agents must be added to the allowlist before delegating to them.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/a2a/agents` | Bearer | Add a trusted A2A agent to your org's allowlist |
| `GET` | `/a2a/agents` | Bearer | List all trusted agents in your allowlist |
| `DELETE` | `/a2a/agents/{agent_id}` | Bearer | Remove an agent from your allowlist |
| `GET` | `/a2a/delegations` | Bearer | List delegation events for your org (cursor paginated) |

**Admin A2A endpoints:**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/admin/a2a/agents` | Admin | Add a trusted agent to an org's allowlist (admin can add to any org) |
| `GET` | `/admin/a2a/agents/{org_id}` | Admin | List trusted agents for a specific org |
| `DELETE` | `/admin/a2a/agents/{agent_id}` | Admin | Remove an agent from an org's allowlist |

### Calling the agent (PowerShell)

```powershell
$token = (Invoke-RestMethod -Uri "http://localhost:8000/token" `
    -Method Post -ContentType "application/json" `
    -Body '{"client_id":"teardrop-client","client_secret":"<secret>"}').access_token

$body = '{"message": "What is 42 * 7?", "thread_id": "my-session-1", "emit_ui": false}'
Invoke-RestMethod -Uri "http://localhost:8000/agent/run" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token" } `
    -Body $body
```

For multi-turn conversation, reuse the same `thread_id` across requests.
Set `emit_ui` to `false` for CLI and machine-to-machine callers to skip the UI generation pass and reduce latency.

### Pagination

`/billing/history`, `/billing/invoices`, `/billing/credit-history`, and `/memories` support cursor-based pagination:

```
GET /billing/invoices?limit=50
→ { "items": [...], "next_cursor": "2026-04-01T12:00:00.000Z" }

GET /billing/invoices?limit=50&cursor=2026-04-01T12:00:00.000Z
→ { "items": [...], "next_cursor": null }   # no more pages
```

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

## How it works

### Agent graph (`agent/graph.py`)

The agent runs as a LangGraph state machine with three nodes:

```
START → planner → [tool_executor ↩] → ui_generator → END
```

- **planner** — Sends the conversation to the configured LLM with all tools bound. If the LLM decides to call a tool, status is set to `EXECUTING`; otherwise it moves to UI generation.
- **tool_executor** — Runs all pending tool calls in parallel, appends `ToolMessage` results, then loops back to the planner for further reasoning.
- **ui_generator** — Extracts or generates A2UI component JSON from the final assistant message and attaches it to the state.

Conversation history persists across turns via `AsyncPostgresSaver` (Postgres-backed LangGraph checkpointer).

### Streaming (`app.py`)

`POST /agent/run` returns a live SSE stream. Event types emitted:

| Event | When |
|-------|------|
| `RUN_STARTED` | Immediately on request |
| `TEXT_MESSAGE_CONTENT` | Each LLM token chunk |
| `TOOL_CALL_START` | Before a tool executes |
| `TOOL_CALL_END` | After a tool returns |
| `SURFACE_UPDATE` | When A2UI components are ready |
| `BILLING_SETTLEMENT` | After on-chain payment settles |
| `USAGE_SUMMARY` | Total tokens, tools, cost for the run |
| `RUN_FINISHED` | Agent completed normally |
| `ERROR` | Unhandled exception |
| `DONE` | Stream closed |

### Tools (`tools/definitions/`)

Twenty-one tools are available to the agent and served via MCP:

| Tool | Description |
|------|-------------|
| `calculate` | Evaluates arithmetic expressions safely (no `eval`). Supports `+`, `-`, `*`, `/`, `**`, `%`, `sqrt`, `abs`, `round`, `floor`, `ceil`, `log`, `sin`, `cos`, `tan`, `pi`, `e`. |
| `convert_currency` | Converts between fiat and crypto currencies using CoinGecko and live fiat exchange rates. |
| `decode_transaction` | Decodes transaction calldata into human-readable form using the supplied ABI or 4byte.directory. |
| `delegate_to_agent` | Delegate a task to a remote A2A-compliant agent. Discovers capabilities, sends a message, handles optional x402 payment, debits org credits, and records audit events. |
| `get_block` | Block metadata (timestamp, gas, miner, tx count) by number or `"latest"`. |
| `get_datetime` | Returns current UTC date/time. Accepts an optional `strftime` format string. |
| `get_erc20_balance` | ERC-20 token balance for an address. |
| `get_eth_balance` | ETH balance for an Ethereum address (mainnet or Base). Requires `ETHEREUM_RPC_URL` or `BASE_RPC_URL`. |
| `get_gas_price` | Current gas price (gwei) and EIP-1559 fee components on Ethereum or Base. |
| `get_token_price` | Crypto asset price in USD (or any supported currency) via CoinGecko. |
| `get_transaction` | Transaction details and status by hash. |
| `get_wallet_portfolio` | Aggregated token holdings and USD value for an Ethereum or Base wallet. |
| `http_fetch` | Fetches and extracts content from a URL. Includes SSRF protection — private/cloud-metadata IPs are blocked. |
| `read_contract` | Calls `view`/`pure` functions on any smart contract by ABI fragment. |
| `resolve_ens` | Resolves ENS name → address or address → ENS primary name. |
| `summarize_text` | Returns character, word, sentence, and paragraph counts for a given text. |
| `web_search` | Web search via Tavily. Set `TAVILY_API_KEY` to activate. |
| `get_defi_positions` | Aggregate DeFi positions (Aave v3, Compound v3, Uniswap v3 LP) for a wallet on Ethereum or Base. |
| `get_dex_quote` | Best Uniswap v3 swap quote across all fee tiers on Ethereum or Base via on-chain QuoterV2. |
| `get_liquidation_risk` | Assess DeFi liquidation risk for up to 50 wallets across Aave v3 and Compound v3. |
| `get_token_approvals` | Audit ERC-20 token allowances and flag risky unlimited approvals across major DeFi spenders. Returns an `error` field when the full RPC approval batch fails so consumers can treat results as incomplete instead of "clean". |

### A2UI components (`agent/state.py`)

The agent can return structured UI alongside text. Supported component types:

| Type | Props |
|------|-------|
| `text` | `content`, `variant` (`body`\|`heading`\|`caption`) |
| `table` | `columns`, `rows` |
| `columns` | `children` |
| `rows` | `children` |
| `form` | `fields`, `submit_label` |
| `button` | `label`, `action` |
| `progress` | `value` (0–100), `label` |

---

## Database

Teardrop uses Postgres (Neon recommended for production, local via Docker for development).

### Migrations

All schema changes are in `migrations/versions/`. Run them with:

```powershell
python -m migrations.runner
```

| File | Contents |
|------|----------|
| `001_baseline.sql` | Core tables: `orgs`, `users`, `wallets`, `siwe_nonces`, `usage_events` |
| `002_billing.sql` | Adds billing fields to `usage_events`; creates `pricing_rules` |
| `003_pricing_seed.sql` | Seeds default usage-based pricing (tokens_in, tokens_out, tool_call rates) |
| `004_credits.sql` | Adds `org_credits` table for prepaid credit balances |
| `005_org_client_credentials.sql` | Per-org M2M client credentials (`org_client_credentials`) |
| `006_credit_ledger.sql` | Immutable debit/top-up audit trail (`org_credit_ledger`) |
| `007_stripe_webhook_events.sql` | Stripe webhook idempotency table (`stripe_webhook_events`) |
| `008_usdc_topup_events.sql` | USDC on-chain top-up events (`usdc_topup_events`) |
| `009_tool_pricing_overrides.sql` | Per-tool pricing overrides; seeds web_search, get_token_price, get_wallet_portfolio rates |
| `010_org_tools.sql` | Per-org custom webhook tools (`org_tools`) and audit events |
| `011_org_memories.sql` | Enables `pgvector`; creates `org_memories` table with HNSW index |
| `012_org_mcp_servers.sql` | Per-org MCP server connections (`org_mcp_servers`) and audit events |
| `013_mcp_marketplace.sql` | Marketplace visibility flags (`publish_as_mcp`, `marketplace_description`, `base_price_usdc`) on org tools |
| `013_settlement_retry.sql` | Settlement retry tracking columns for auto-sweep background worker |
| `014_org_spending_limits.sql` | Per-org spending caps and pause/resume controls (`org_spending_limits`) |
| `015_memory_ttl_dedup.sql` | Memory TTL expiry and near-duplicate deduplication support |
| `016_email_verification.sql` | Email verification tokens and `email_verified` flag on users |
| `017_org_invites.sql` | Org invite tokens and acceptance flow (`org_invites`) |
| `018_refresh_tokens.sql` | Persistent refresh tokens for 30-day sessions (`refresh_tokens`) |
| `019_org_llm_config.sql` | Per-org LLM provider/model/BYOK config (`org_llm_config`) |
| `020_usage_provider_model.sql` | Adds `provider` and `model` columns to `usage_events` for per-model billing |
| `021_model_pricing.sql` | Dynamic per-model pricing table (`model_pricing`) |
| `022_model_pricing_seed.sql` | Seeds default pricing for all models in the catalogue |
| `023_siwe_login_sessions.sql` | SIWE session persistence for nonce replay protection |
| `024_a2a_delegation_billing.sql` | A2A delegation billing: extends `a2a_allowed_agents` with cost caps; creates `a2a_delegation_events` audit table |
| `025_org_agent_wallets.sql` | CDP-backed agent wallets (`org_agent_wallets`) and audit events (`agent_wallet_events`) |
| `026_a2a_jwt_forward.sql` | JWT forwarding flag on A2A delegation rules |
| `026_normalize_revenue_share.sql` | Backfills and normalises revenue share in basis points |
| `027_marketplace_tool_pricing.sql` | Per-tool pricing overrides for marketplace authors |
| `028_marketplace_subscriptions.sql` | Org marketplace tool subscriptions (`marketplace_subscriptions`) |
| `029_marketplace_platform_tools.sql` | Platform built-in metered tool enablement in marketplace |
| `029_sweep_retry_columns.sql` | Auto-sweep retry tracking and backoff columns on withdrawal records |
| `030_siwe_nonce_address_binding.sql` | Binds SIWE nonces to the signing address to prevent cross-wallet replay |
| `031_activate_bench_tools.sql` | Activates benchmark tooling entries |
| `031_byok_platform_fee.sql` | BYOK flat platform fee column on `org_llm_config` |
| `032_refresh_token_successor.sql` | Refresh token successor chaining for atomic rotation |
| `033_get_token_approvals.sql` | Schema support for `get_token_approvals` tool |
| `034_get_defi_positions.sql` | Schema support for `get_defi_positions` tool |
| `035_get_liquidation_risk.sql` | Schema support for `get_liquidation_risk` tool |
| `036_get_dex_quote.sql` | Schema support for `get_dex_quote` tool |
| `037_fix_haiku_pricing.sql` | Corrects Claude Haiku 4.5 token pricing to $0.80/$4.00 per 1k |
| `038_org_llm_config_allow_openrouter.sql` | Expands provider CHECK constraint to allow `openrouter` in `org_llm_config` |
| `039_new_model_pricing_seed.sql` | Pricing for DeepSeek V3.2 (superseded), Gemini 3 Flash Preview, and Claude Sonnet 4.6 |
| `040_v4_flash_pricing.sql` | Replaces DeepSeek V3.2 pricing with V4 Flash (same Teardrop rates, lower provider cost) |

### Neon (production)

Set `DATABASE_URL` to your Neon connection string (no `+asyncpg` prefix needed — the app strips it automatically).

---

## Project structure

```
app.py              # FastAPI app, SSE streaming, billing gate, all route handlers
auth.py             # RS256 JWT: create_access_token, require_auth dependency
billing.py          # x402 billing layer, pricing, invoice queries, credit system
cache.py            # Redis cache helpers
config.py           # Settings via pydantic-settings (reads .env)
memory.py           # Per-org pgvector memory: LLM extraction, recall, CRUD
mcp_client.py       # Per-org MCP client: CRUD, session pool, tool discovery
org_tools.py        # Per-org custom webhook tools: CRUD, caching, execution
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
scripts/
  generate_keys.py  # Generate RSA keypair → keys/private.pem + keys/public.pem
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

