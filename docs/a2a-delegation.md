# A2A Delegation & cross-agent revenue routing

Teardrop agents can delegate specialist tasks to remote A2A-compliant agents and charge those delegations back to the calling organisation. This enables:

- **Network effect**: Agents discover and call each other via published Agent Cards.
- **Specialisation**: Route complex tasks to domain-expert agents.
- **Revenue sharing**: Collect payments from delegations and distribute to specialist agent operators.
- **Budget control**: Per-agent cost caps, global delegation spending limits, and org-level pause/daily spend checks.

The public `/.well-known/agent-card.json` advertises the `/tools/mcp` gateway under `endpoints.mcp_tools`. When `MARKETPLACE_ENABLED=true`, it also includes `capabilities.marketplace` and `endpoints.marketplace_catalog` so external A2A clients can discover the paid marketplace catalog without hard-coding Teardrop-specific URLs.

The card also emits additive A2A v1.0 discovery fields such as `protocolVersion`, `supportedInterfaces`, `securitySchemes`, `defaultInputModes`, and `defaultOutputModes` while preserving Teardrop-specific `endpoints`, `tools`, and `authentication` metadata for current SDK consumers. `supportedInterfaces` now advertises both the streaming AG-UI surface (`/agent/run`) and the blocking inbound A2A surface (`/message:send`).

The `skills`/`tools` sections of the public card are curated: each `ToolDefinition` carries a `show_on_agent_card` flag (`tools/registry.py`), and commoditized utility/low-level RPC primitives (`calculate`, `get_datetime`, `count_text_stats`, `convert_currency`, `get_block`, `get_erc20_balance`, `get_eth_balance`, `get_transaction`, `read_contract`, `resolve_ens`) are excluded to keep the public discovery surface focused on Teardrop's differentiated capabilities. This does not affect tool availability — every tool remains callable via `/agent/run`, the full org inventory at `GET /agent/tools`, and the MCP catalogue at `/.well-known/mcp/server-card.json`.

Teardrop also publishes x402 discovery metadata at `/.well-known/x402` and `/.well-known/x402.json`. These public, cacheable aliases advertise the canonical paid entrypoints (`/message:send`, `/tools/mcp`) alongside the public pricing metadata at `/billing/pricing`.

---

## How It Works

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

---

## Configuration

In your environment or `.env` file:

```bash
# Enable A2A delegation
A2A_DELEGATION_ENABLED=true
A2A_DELEGATION_TIMEOUT_SECONDS=120
A2A_DELEGATION_MAX_PER_RUN=3         # Max delegations per agent run

# Enable billing for delegations
A2A_DELEGATION_BILLING_ENABLED=true
A2A_DELEGATION_PLATFORM_FEE_BPS=500  # Platform fee: 500 bps = 5%
A2A_DELEGATION_MAX_COST_USDC=100000  # Global delegation cost cap ($0.10)

# For x402 delegations (optional):
X402_TREASURY_PRIVATE_KEY=0x...      # Treasury wallet private key (hex-encoded)
```

---

## Inbound A2A Entrypoint

External agents can call Teardrop directly over `POST /message:send`.

- Anonymous callers may pay per request with x402 by retrying the call with `X-PAYMENT` after an initial `402 Payment Required` response. The challenge now uses the standard `PAYMENT-REQUIRED` header and also serves `X-PAYMENT-REQUIRED` as a legacy compatibility alias.
- Unpaid anonymous probes receive the `402 Payment Required` challenge before request-body validation, which keeps registry validators compatible with empty or malformed probe payloads.
- The `402` body is a full x402 v2 `PaymentRequired` payload with top-level `resource`, `accepts`, and `extensions`. On `POST /message:send`, `extensions.bazaar` advertises the A2A request and response shape for registries.
- Authenticated callers may present a Teardrop JWT and reuse the existing credit/x402 billing gate.
- The current implementation is a single-turn blocking endpoint: it accepts an A2A `message` payload (or JSON-RPC envelope) and returns a completed `Task` in a JSON-RPC envelope.
- Operators may disable the surface with `A2A_INBOUND_ENABLED=false`; the endpoint then returns `404` and the public agent card stops advertising `a2a_message`.

---

## Allowlist & Budget Control

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

### Payment Methods for Delegations

| Setting | Billing Method | When to Use |
|---------|---|---|
| `require_x402=false` | Org prepaid credits | Default: instant, requires upfront org credit balance |
| `require_x402=true` | x402 on-chain (USDC) | Agent requires on-chain payment; uses treasury wallet to sign |

---

## Delegation Events & Audit Trail

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

### Delegation in SSE Stream

When a delegation occurs during an agent run, the final `USAGE_SUMMARY` and `BILLING_SETTLEMENT` events include the delegation cost breakdown:

```json
{
  "event": "USAGE_SUMMARY",
  "data": {
    "run_id": "run-123",
    "tokens_in": 1500,
    "tokens_out": 800,
    "cache_read_tokens": 1200,
    "cache_creation_tokens": 300,
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
