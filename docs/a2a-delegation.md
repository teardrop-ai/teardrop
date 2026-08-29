# A2A Delegation & cross-agent revenue routing

Teardrop agents can delegate specialist tasks to remote A2A-compliant agents and charge those delegations back to the calling organisation. This enables:

- **Network effect**: Agents discover and call each other via published Agent Cards.
- **Specialisation**: Route complex tasks to domain-expert agents.
- **Revenue sharing**: Collect payments from delegations and distribute to specialist agent operators.
- **Budget control**: Per-agent cost caps, global delegation spending limits, and org-level pause/daily spend checks.

The public `/.well-known/agent-card.json` advertises the `/tools/mcp` gateway under `endpoints.mcp_tools`. When `MARKETPLACE_ENABLED=true`, it also includes `capabilities.marketplace`, `endpoints.marketplace_catalog`, and `endpoints.marketplace_authors` so external clients can discover the paid marketplace catalog and its active author index without hard-coding Teardrop-specific URLs. The author index is a catalog metadata surface, not a directory of remote A2A URLs; `delegate_to_agent` still requires an explicit URL and the normal SSRF, allowlist, and budget controls. Its additive `capabilities.marketplace.registration` metadata identifies `/tools` for agent-owned tool registration and `/marketplace/author-config` for payout setup; SIWE sessions are restricted to their own wallet, while organization admins retain treasury-wallet control. MCP import publishing remains admin-only.

The card also emits additive A2A v1.0 discovery fields such as `protocolVersion`, `supportedInterfaces`, `securitySchemes`, `defaultInputModes`, and `defaultOutputModes` while preserving Teardrop-specific `endpoints`, `tools`, and `authentication` metadata for current SDK consumers. Platform tool entries include cached aggregate reputation when available; the complete active-tool index is published at `/.well-known/reputation.json`. `supportedInterfaces` advertises both the streaming AG-UI surface (`/agent/run`) and the inbound A2A surface (`/message:send`). When enabled, `capabilities.asyncTasks` advertises the opt-in `Prefer: respond-async` flow and its polling endpoint.

The `skills`/`tools` sections of the public card are curated: each `ToolDefinition` carries a `show_on_agent_card` flag (`tools/registry.py`), and commoditized utility/low-level RPC primitives (`calculate`, `get_datetime`, `count_text_stats`, `convert_currency`, `get_block`, `get_erc20_balance`, `get_eth_balance`, `get_transaction`, `read_contract`, `resolve_ens`) are excluded to keep the public discovery surface focused on Teardrop's differentiated capabilities. This does not affect tool availability — every tool remains callable via `/agent/run`, the full org inventory at `GET /agent/tools`, and the MCP catalogue at `/.well-known/mcp/server-card.json`.

Each `ToolDefinition` may also carry agent-commerce guidance fields — `use_when`, `limitations`, and `alternatives` — that are emitted on the A2A skills/tools sections and the MCP server card when present. These help external agents decide when to select a tool, what constraints apply, and which related tools to consider instead. They are additive and omitted when empty, so existing consumers see no change.

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
A2A_DELEGATION_MAX_CONCURRENT_PER_RUN=2 # Max simultaneous delegations per agent run
A2A_DELEGATION_REQUIRE_ALLOWLIST=true # Fail closed without org context or trust entry

# Enable billing for delegations
A2A_DELEGATION_BILLING_ENABLED=true
A2A_DELEGATION_PLATFORM_FEE_BPS=500  # Platform fee: 500 bps = 5%
A2A_DELEGATION_MAX_COST_USDC=100000  # Global delegation cost cap ($0.10)

# Optional asynchronous inbound execution
A2A_INBOUND_ASYNC_ENABLED=true
A2A_INBOUND_ASYNC_MAX_CONCURRENCY=8
A2A_INBOUND_ASYNC_QUEUE_SIZE=100
A2A_INBOUND_TASK_TTL_DAYS=7

# For x402 delegations (optional):
X402_TREASURY_PRIVATE_KEY=0x...      # Treasury wallet private key (hex-encoded)
```

Outbound x402 challenges are accepted only on `X402_NETWORK` and only when the
integer atomic amount is at or below the effective delegation cap (the per-agent
cap after the platform fee). Missing, malformed, or over-cap requirements are
rejected before the treasury signs a payment.

---

## Inbound A2A Entrypoint

External agents can call Teardrop directly over `POST /message:send`.

- Anonymous callers may pay per request with x402 by retrying the call with `X-PAYMENT` after an initial `402 Payment Required` response. The challenge now uses the standard `PAYMENT-REQUIRED` header and also serves `X-PAYMENT-REQUIRED` as a legacy compatibility alias.
- Unpaid anonymous probes receive the `402 Payment Required` challenge before request-body validation, which keeps registry validators compatible with empty or malformed probe payloads.
- The `402` body is a full x402 v2 `PaymentRequired` payload with top-level `resource`, `accepts`, and `extensions`. On `POST /message:send`, `extensions.bazaar` advertises the A2A request and response shape for registries.
- Authenticated callers may present a Teardrop JWT and reuse the existing credit/x402 billing gate.
- By default, the endpoint remains single-turn and blocking: it accepts an A2A `message` payload (or JSON-RPC envelope) and returns a completed `Task` in a JSON-RPC envelope.
- Callers that send `Prefer: respond-async` receive `202 Accepted`, an internal task ID, a `Location` header, and `metadata.statusPath`. Poll `GET /message:status/{task_id}` for the submitted, working, or terminal task projection. Authenticated polling is scoped to the JWT organization and user; anonymous polling requires the internal task ID as a capability.
- Asynchronous execution uses a bounded per-process worker pool. A full queue returns `503 Service Unavailable` with `Retry-After: 1`. x402 verification and credit billing remain in the worker so a queued request does not consume a single-use payment claim before it can execute.
- Each active task carries a Postgres process lease renewed by its owning instance. A healthy sibling instance leaves that task alone; after the lease expires, the recovery loop terminalizes the task as failed with an unknown billing outcome and never automatically re-executes it. The queue itself remains local rather than a durable distributed broker.
- Operators may disable the surface with `A2A_INBOUND_ENABLED=false`; the endpoint then returns `404` and the public agent card stops advertising `a2a_message`.

## A2A Event-Trigger Control Plane

When `EVENT_TRIGGERS_ENABLED=true`, the public Agent Card advertises an `event_trigger_ingress` skill with registration, dispatch, and task-polling endpoint templates. This is a control-plane capability, not a generic read-only tool call:

1. An authenticated, org-scoped caller registers a prompt template through `POST /agent/event-triggers` and receives a secret once.
2. An event source posts a JSON object to `/agent/events/{trigger_token}` with `X-Teardrop-Trigger-Secret`.
3. Teardrop rate-limits the source, verifies the secret in constant time, renders the bounded prompt, atomically reserves a durable run identity and cluster execution lease, and returns `202 Accepted`.
4. The caller polls `/agent/event-triggers/{id}/runs/{run_id}` for an A2A-shaped task. The initial state is `TASK_STATE_SUBMITTED`; persisted outcomes map to `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, or `TASK_STATE_REJECTED` for credit-skipped runs.

Dispatch payloads must be valid JSON objects and are capped at 64 KiB. Idempotency keys remain supported, and every dispatch receives a durable internal reservation so polling works even when the caller did not provide an idempotency key. Postgres leases enforce global and per-org concurrency across instances; healthy workers renew ownership, while expired leases are reconciled into terminal failures without re-executing potentially billable work. `pushNotifications` remains false until Teardrop implements authenticated task webhooks.

Trigger lifecycle, dispatch, secret rejection, and settlement metadata are written to the insert-only `event_trigger_events` audit table. The table excludes payloads, prompts, callback URLs, secrets, and secret hashes; financial truth remains in the existing credit, usage, and settlement ledgers.

---

## Allowlist & Budget Control

Organisations must explicitly add remote agents to their allowlist before delegating to them:

The default `A2A_DELEGATION_REQUIRE_ALLOWLIST=true` also requires authenticated
organisation runtime context. Direct tool invocation without `org_id` and the
database pool fails closed; setting it to `false` is an explicit operator choice
for trusted non-billed development flows.

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

Credit-funded delegations create a durable refund record in
`a2a_delegation_refund_outbox` in the same transaction as the debit. Successful
delegations cancel that record. Dispatch failures and non-completed remote tasks
request a refund; the compensating top-up and immutable credit-ledger row are
written atomically. A background retry worker reconciles requested refunds and
pending rows with deterministic delegation event IDs after process failure.

When an x402 payment header has been signed and the paid retry begins but the
remote outcome cannot be determined, the delegation is recorded as
`possibly_delivered`. Its pre-debit remains held and the refund worker skips it;
Teardrop never automatically re-dispatches an ambiguous task. An administrator
must inspect the remote task or payment record and resolve it explicitly:

- `confirmed` marks delivery complete and cancels the pending refund.
- `failed` records definitive non-delivery and applies the durable credit refund.

Both resolutions are org-scoped, idempotent, and reject an incompatible
already-terminal resolution. Use `GET /admin/a2a/delegations/possibly-delivered`
to list cases and `POST /admin/a2a/delegations/{delegation_id}/resolve` with an
`org_id` and `outcome` of `confirmed` or `failed` to resolve one. A confirmed
resolution may include a validated EVM transaction hash in `settlement_tx`.
The immutable `a2a_delegation_events` row is never updated; org history exposes
the current `delivery_status` projection separately.

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
    "delivery_status": "not_attempted",
    "delivery_resolved_at": null,
    "delivery_settlement_tx": null,
    "delivery_error": null,
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
