# teardrop
Intelligence beyond the browser

Teardrop is a streaming AI agent API. You send it a message; it reasons using Claude, optionally calls tools, builds a UI component tree, and streams everything back as Server-Sent Events. It implements three open protocols simultaneously: **AG-UI** (streaming events), **A2A** (agent discoverability), and **MCP** (tool serving). Payments are handled by the **x402** protocol — agents and users pay per request in USDC on Base, with no subscription required.

---

## Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)
- A Postgres database (local via Docker, or [Neon](https://neon.tech) for production)

---

## Setup (PowerShell)

**1. Clone and enter the project**
```powershell
cd "C:\Users\<you>\Documents\Local Repositiories\teardrop"
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
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=postgresql://teardrop:teardrop@localhost:5432/teardrop
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
| `ANTHROPIC_API_KEY` | Required |
| `DATABASE_URL` | Neon Postgres connection string |
| `BILLING_ENABLED` | `true` to activate x402 payments |
| `X402_PAY_TO_ADDRESS` | Treasury wallet (USDC recipient) |
| `X402_NETWORK` | `eip155:8453` for Base mainnet |
| `SIWE_DOMAIN` | Your public domain (e.g. `teardrop.onrender.com`) |
| `CORS_ORIGINS` | Comma-separated allowed origins |

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
  │   BILLING_SETTLEMENT          │── settle on-chain ───────────►│
  │   { tx_hash, amount_usdc }    │◄─ tx confirmed ───────────────│
```

### Payment methods by auth type

| Auth method | Payment mechanism |
|-------------|-------------------|
| `siwe` | x402 on-chain (USDC, `exact` scheme, per-request) |
| `client_credentials` | Org prepaid credit balance (off-chain debit) |
| `email` | Org prepaid credit balance (off-chain debit) |

### Configuration

```
BILLING_ENABLED=true
X402_PAY_TO_ADDRESS=0xYourTreasuryWallet
X402_NETWORK=eip155:8453          # Base mainnet (eip155:84532 = Base Sepolia)
X402_FACILITATOR_URL=https://x402.org/facilitator
BILLABLE_AUTH_METHODS=["siwe"]    # Add "client_credentials","email" to bill those too
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

### Credit top-up (machine callers)

Admins can add prepaid USDC credit to an org's balance:

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/admin/credits/topup" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $adminToken" } `
    -Body '{"org_id":"org-123","amount_usdc":1000000}'   # $1.00
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

### Auth

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/token` | — | Issue JWT (client-creds, email, or SIWE) |
| `GET` | `/auth/siwe/nonce` | — | Generate single-use SIWE nonce |

### Billing

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/billing/pricing` | — | Current pricing rules |
| `GET` | `/billing/history` | Bearer | Settled payment history (cursor paginated) |
| `GET` | `/billing/invoices` | Bearer | All run records including pending (cursor paginated) |
| `GET` | `/billing/invoice/{run_id}` | Bearer | Single run receipt |
| `GET` | `/billing/balance` | Bearer | Org prepaid credit balance |
| `GET` | `/billing/export.csv` | Bearer | CSV export of all run records (max 10,000) |

### Wallets

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/wallets/link` | Bearer | Link additional wallet via SIWE |
| `GET` | `/wallets/me` | Bearer | List your linked wallets |
| `DELETE` | `/wallets/{wallet_id}` | Bearer | Unlink a wallet |

### Usage

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/usage/me` | Bearer | Aggregated token/tool usage for current user |

### Admin

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/admin/orgs` | Admin | Create organisation |
| `POST` | `/admin/users` | Admin | Create user |
| `GET` | `/admin/usage/{user_id}` | Admin | Usage for a specific user |
| `GET` | `/admin/usage/org/{org_id}` | Admin | Usage for an org |
| `GET` | `/admin/billing/revenue` | Admin | Aggregated revenue summary |
| `POST` | `/admin/credits/topup` | Admin | Add prepaid USDC credits to an org |

### Calling the agent (PowerShell)

```powershell
$token = (Invoke-RestMethod -Uri "http://localhost:8000/token" `
    -Method Post -ContentType "application/json" `
    -Body '{"client_id":"teardrop-client","client_secret":"<secret>"}').access_token

$body = '{"message": "What is 42 * 7?", "thread_id": "my-session-1"}'
Invoke-RestMethod -Uri "http://localhost:8000/agent/run" `
    -Method Post -ContentType "application/json" `
    -Headers @{ Authorization = "Bearer $token" } `
    -Body $body
```

For multi-turn conversation, reuse the same `thread_id` across requests.

### Pagination

`/billing/history` and `/billing/invoices` support cursor-based pagination:

```
GET /billing/invoices?limit=50
→ { "items": [...], "next_cursor": "2026-04-01T12:00:00.000Z" }

GET /billing/invoices?limit=50&cursor=2026-04-01T12:00:00.000Z
→ { "items": [...], "next_cursor": null }   # no more pages
```

---

## Running the MCP tool server (optional)

The tools can be served standalone over the MCP protocol for use with Claude Desktop or other MCP clients:

```powershell
# stdio transport (default – for Claude Desktop)
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

- **planner** — Sends the conversation to Claude with all tools bound. If Claude decides to call a tool, status is set to `EXECUTING`; otherwise it moves to UI generation.
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

Nine tools are available to the agent and served via MCP:

| Tool | Description |
|------|-------------|
| `calculate` | Evaluates arithmetic expressions safely (no `eval`). Supports `+`, `-`, `*`, `/`, `**`, `%`, `sqrt`, `abs`, `round`, `floor`, `ceil`, `log`, `sin`, `cos`, `tan`, `pi`, `e`. |
| `get_datetime` | Returns current UTC date/time. Accepts an optional `strftime` format string. |
| `web_search` | Web search via Tavily. Set `TAVILY_API_KEY` to activate. |
| `summarize_text` | Returns character, word, sentence, and paragraph counts for a given text. |
| `get_eth_balance` | ETH balance for an Ethereum address (mainnet or Base). Requires `ETHEREUM_RPC_URL` or `BASE_RPC_URL`. |
| `get_erc20_balance` | ERC-20 token balance for an address. |
| `get_transaction` | Transaction details and status by hash. |
| `resolve_ens` | Resolves ENS name → address or address → ENS primary name. |
| `get_block` | Block metadata (timestamp, gas, miner, tx count) by number or `"latest"`. |

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
| `002_billing.sql` | Adds `cost_usdc`, `settlement_tx`, `settlement_status` to `usage_events`; creates `pricing_rules` |
| `003_pricing_seed.sql` | Seeds usage-based pricing rule (tokens_in, tokens_out, tool_call rates) |
| `004_credits.sql` | Adds `org_credits` table for prepaid credit balances |

### Neon (production)

Set `DATABASE_URL` to your Neon connection string (no `+asyncpg` prefix needed — the app strips it automatically).

---

## Project structure

```
app.py              # FastAPI app, SSE streaming, billing gate, all route handlers
auth.py             # RS256 JWT: create_access_token, require_auth dependency
billing.py          # x402 billing layer, pricing, invoice queries, credit system
config.py           # Settings via pydantic-settings (reads .env)
usage.py            # UsageEvent model, usage recording and aggregation
users.py            # Org + User models, CRUD, PBKDF2-SHA256 password hashing
wallets.py          # Wallet management, SIWE nonce lifecycle
agent/
  graph.py          # LangGraph StateGraph definition and routing
  nodes.py          # planner, tool_executor, ui_generator implementations
  state.py          # AgentState, A2UIComponent, TaskStatus schemas
tools/
  registry.py       # ToolRegistry: versioned, with deprecation lifecycle
  mcp_server.py     # Standalone FastMCP server for MCP protocol clients
  definitions/      # One file per tool (calculate, get_datetime, web_search, …)
  __init__.py       # Global registry singleton, get_langchain_tools()
migrations/
  runner.py         # Applies SQL migrations in order
  versions/         # 001_baseline, 002_billing, 003_pricing_seed, 004_credits
scripts/
  generate_keys.py  # Generate RSA keypair → keys/private.pem + keys/public.pem
  init_neon.py      # Initialize Neon Postgres schema
  seed_users.py     # Create default org + admin user for local dev
```

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
