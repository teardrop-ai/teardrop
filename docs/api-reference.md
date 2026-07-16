# API Reference

Teardrop issued RS256 JWTs are required for authorization on most endpoints. All endpoints (except `/health`, `/docs`, `/billing/pricing`, `/.well-known/agent-card.json`, and the public payment-gated `POST /message:send` A2A endpoint when `A2A_INBOUND_ENABLED=true`) require a `Bearer` token.

---

### Core

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | — | Redirects to `/docs` |
| `GET` | `/health` | — | Liveness probe |
| `GET` | `/llms.txt` | — | Root LLM-friendly discovery index for public Teardrop surfaces |
| `GET` | `/robots.txt` | — | Public crawler directives with `llms.txt` pointer |
| `POST` | `/agent/run` | Bearer | Main streaming endpoint (SSE) |
| `POST` | `/message:send` | Bearer or x402 | Blocking inbound A2A endpoint for external agents (when enabled) |
| `GET` | `/agent/tools` | Bearer | Tool inventory for current org (platform, org, and subscribed marketplace tools) |
| `GET` | `/agent/tool-exclusions` | Bearer | List the org's persisted tool exclusions |
| `POST` | `/agent/tool-exclusions` | Bearer | Persist a tool exclusion (merged with per-request `tool_policy.exclude_names` on every run) |
| `DELETE` | `/agent/tool-exclusions/{tool_name}` | Bearer | Remove a persisted tool exclusion |
| `GET` | `/.well-known/agent-card.json` | — | A2A agent card with MCP discovery and optional marketplace metadata |
| `GET` | `/.well-known/x402` | — | Public x402 discovery metadata for registries and validators |
| `GET` | `/.well-known/x402.json` | — | Legacy JSON alias for x402 discovery metadata |
| `GET` | `/.well-known/mcp/server-card.json` | — | Static MCP tool catalogue for Smithery |
| `GET` | `/.well-known/agent.json` | — | Legacy alias for the agent card used by older crawlers |
| `GET` | `/.well-known/jwks.json` | — | RS256 public key in JWKS format (for external JWT verification) |
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
| `POST` | `/token` | — | Issue JWT (client-creds, email, or SIWE); returns `access_token` + `refresh_token` |
| `GET` | `/auth/me` | Bearer | Return the authenticated user's identity |
| `GET` | `/auth/siwe/nonce` | — | Generate single-use SIWE nonce |
| `POST` | `/register` | — | Self-serve org + user registration (optional invite-only + CAPTCHA gates) |
| `GET` | `/auth/verify-email` | — | Consume one-time email verification token |
| `POST` | `/auth/resend-verification` | Bearer | Re-send verification email |
| `POST` | `/auth/refresh` | — | Exchange refresh token for new access + rotated refresh token |
| `POST` | `/auth/logout` | Bearer | Revoke refresh token (end session) |
| `POST` | `/org/invite` | Bearer | Create org invite link (any authenticated member) |
| `POST` | `/register/invite` | — | Accept invite token + create user account |
| `GET` | `/org/credentials` | Bearer | List org M2M client credentials |
| `POST` | `/org/credentials/regenerate` | Bearer | Rotate all org M2M credentials (admin-only) |

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

`GET /billing/balance` returns atomic USDC fields. A `spending_limit_usdc` value of `0` means unlimited daily spend (`spending_limit_active=false`).

### Marketplace

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/marketplace/catalog` | — | Public catalog with optional `org_slug`, `category`, `sort`, `limit`, and `cursor` query params |
| `GET` | `/marketplace/catalog/{org_slug}/{tool_name}` | — | Public detail for one published catalog tool |
| `GET` | `/marketplace/authors/{org_slug}` | — | Public author profile with aggregate calls and paginated tools |
| `GET` | `/marketplace/llms.txt` | — | Plain-text catalog index for LLM crawlers and SEO surfaces |
| `POST` | `/marketplace/author-config` | Bearer | Create or update author settlement wallet |
| `GET` | `/marketplace/author-config` | Bearer | Get author settlement wallet config |
| `POST` | `/marketplace/import/preview` | Bearer | Preview importable MCP tools, normalized schemas, and publish blockers |
| `POST` | `/marketplace/import/publish` | Bearer | Admin-only publish of MCP-backed marketplace tools |
| `GET` | `/marketplace/balance` | Bearer | Author earnings balance |
| `GET` | `/marketplace/earnings` | Bearer | Author earnings history |
| `GET` | `/marketplace/earnings/by-tool` | Bearer | Author earnings grouped by tool |
| `POST` | `/marketplace/withdraw` | Bearer | Request an author payout |
| `GET` | `/marketplace/withdrawals` | Bearer | Withdrawal history |
| `POST` | `/marketplace/subscriptions` | Bearer | Subscribe to a community marketplace tool |
| `GET` | `/marketplace/subscriptions` | Bearer | List active marketplace subscriptions |
| `DELETE` | `/marketplace/subscriptions/{id}` | Bearer | Unsubscribe from a marketplace tool |

`GET /marketplace/catalog` sorts by `name`, `price_asc`, `price_desc`, or `popularity`. Categories are `defi`, `search`, `data`, `communication`, and `utility`; an empty category is allowed for uncategorized tools. `total_calls` is sourced from non-financial aggregate stats and is recorded only after successful paid tool calls, not from the immutable earnings ledger.

`POST /marketplace/author-config` accepts any valid `0x` + 40-hex Ethereum/Base address and stores the canonical EIP-55 checksummed form. `POST /marketplace/import/publish` may omit `input_schema` and `output_schema`; when omitted, Teardrop reuses the normalized or synthesized schemas from live MCP discovery.

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
| `GET` | `/admin/billing/pending` | Admin | List pending settlements |
| `POST` | `/admin/billing/pending/{id}/retry` | Admin | Retry a specific failed settlement |
| `GET` | `/admin/orgs/{org_id}/spending` | Admin | Get org spending config (caps, pause status) |
| `PATCH` | `/admin/orgs/{org_id}/spending` | Admin | Update org spending caps and pause status |
| `GET` | `/admin/marketplace/sweep-status` | Admin | Status of all pending withdrawals |
| `POST` | `/admin/marketplace/sweep-retry/{id}` | Admin | Reset an exhausted withdrawal for retry |
| `POST` | `/admin/marketplace/process-withdrawal/{id}` | Admin | Manually process a single withdrawal |

### Custom Tools

Per-org webhook-backed tools are injected into the agent at run-time and never appear in the public Agent Card or MCP server.
Custom webhook tools are currently read-only and must use the `GET` HTTP method.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/tools` | Bearer | Register a custom webhook tool |
| `GET` | `/tools` | Bearer | List org's custom tools |
| `GET` | `/tools/{tool_id}` | Bearer | Get a specific custom tool |
| `PATCH` | `/tools/{tool_id}` | Bearer | Update a custom tool |
| `DELETE` | `/tools/{tool_id}` | Bearer | Delete a custom tool |
| `POST` | `/tools/test-webhook` | Bearer | Fire a test request to a webhook URL |

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
| `POST` | `/mcp/servers/{server_id}/test-tool` | Bearer | Diagnostic: invoke one MCP tool without billing, audit, or circuit-breaker effects |

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

---

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
