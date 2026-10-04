# API Reference

Teardrop issued RS256 JWTs are required for authorization on most endpoints. Public discovery endpoints, `/health`, `/docs`, `/billing/pricing`, and the payment-gated `POST /message:send` A2A endpoint when `A2A_INBOUND_ENABLED=true` do not require a `Bearer` token.

---

### Core

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | — | Redirects to `/docs` |
| `GET` | `/health` | — | Liveness probe |
| `GET` | `/llms.txt` | — | Root LLM-friendly discovery index for public Teardrop surfaces; a `Tools` section lists callable platform tools (with per-call price when positive) from the shared capability manifest |
| `GET` | `/robots.txt` | — | Public crawler directives with `llms.txt` pointer |
| `POST` | `/agent/run` | Bearer | Main streaming endpoint (SSE) |
| `POST` | `/message:send` | Bearer or x402 | Inbound A2A endpoint; blocking by default, or `202` with `Prefer: respond-async` (when enabled) |
| `GET` | `/message:status/{task_id}` | Bearer or capability | Poll an asynchronous inbound A2A task (when enabled) |
| `GET` | `/agent/tools` | Bearer | Tool inventory for current org (platform, org, and subscribed marketplace tools) |
| `GET` | `/agent/tool-exclusions` | Bearer | List the org's persisted tool exclusions |
| `POST` | `/agent/tool-exclusions` | Bearer | Persist a tool exclusion (merged with per-request `tool_policy.exclude_names` on every run) |
| `DELETE` | `/agent/tool-exclusions/{tool_name}` | Bearer | Remove a persisted tool exclusion |
| `GET` | `/.well-known/agent-card.json` | — | A2A agent card with MCP discovery and optional marketplace metadata |
| `GET` | `/.well-known/reputation.json` | — | Aggregate quality metrics for active marketplace tools; caller counts are omitted below five distinct orgs |
| `GET` | `/.well-known/x402` | — | Public x402 discovery metadata for registries and validators; advertises the `grant_type=x402` bootstrap entrypoint when enabled. When `MCP_X402_ENABLED`, the `/tools/mcp` resource lists `tools` (`name`, `description`, `scheme: exact`, `amount_usdc`) for each positively priced, x402-payable tool from the shared capability manifest — the same per-call amount the gateway challenges for |
| `GET` | `/.well-known/x402.json` | — | Legacy JSON alias for x402 discovery metadata |
| `GET` | `/.well-known/mcp/server-card.json` | — | Static MCP tool catalogue for Smithery; lists platform tools from the shared capability manifest (`tools/capabilities.py`) with the same `_meta` (`teardrop/reputation`, `teardrop/price`) as live `tools/list`; advertises `bearer` + `x402` auth schemes and an `x402` capability block (network, scheme, discovery URL, bootstrap endpoint) when `MCP_X402_ENABLED` |
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
| `POST` | `/token` | — | Issue JWT (client-creds, email, or SIWE), or use `grant_type=x402` for payment-first org bootstrap |
| `GET` | `/auth/me` | Bearer | Return the authenticated user's identity |
| `GET` | `/auth/siwe/nonce` | — | Generate single-use SIWE nonce |
| `POST` | `/register` | — | Self-serve org + user registration (optional normalized `acquisition_source`, invite-only + CAPTCHA gates) |
| `GET` | `/auth/verify-email` | — | Consume one-time email verification token |
| `POST` | `/auth/resend-verification` | Bearer | Re-send verification email |
| `POST` | `/auth/refresh` | — | Exchange refresh token for new access + rotated refresh token |
| `POST` | `/auth/logout` | Bearer | Revoke refresh token (end session) |
| `POST` | `/org/invite` | Bearer | Create org invite link (any authenticated member) |
| `POST` | `/register/invite` | — | Accept invite token + create user account |
| `GET` | `/org/credentials` | Bearer | List org M2M client credentials with their `scope` (`read`/`publish`/`withdraw`) and `disabled_at` |
| `POST` | `/org/credentials/regenerate` | Bearer | Rotate all org M2M credentials (admin or owning SIWE wallet for machine orgs); optional `scope` query parameter (default `publish`) |
| `POST` | `/org/credentials/{client_id}/disable` | Bearer | Disable a single M2M credential immediately (admin or owning SIWE wallet); idempotent |

Protected endpoints return a `401` challenge with `bootstrap_uri="/token"` and `bootstrap_grant="x402"` when bearer authentication is missing or invalid. Agents can follow that hint through payment-first bootstrap, then retry with the issued bearer token.

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
| `GET` | `/org/principals/spend-limits` | Admin Bearer | List per-principal credit spend limits for the authenticated org |
| `PUT` | `/org/principals/{principal_id}/spend-limit` | Admin Bearer | Idempotently set a principal's 24-hour credit limit and pause state |
| `DELETE` | `/org/principals/{principal_id}/spend-limit` | Admin Bearer | Idempotently remove a principal-specific limit |

`GET /billing/balance` returns atomic USDC fields. For human orgs, a `spending_limit_usdc` value of `0` means unlimited daily spend (`spending_limit_active=false`). For machine-provisioned orgs, zero resolves to `MACHINE_ORG_DAILY_SPEND_LIMIT_USDC`; an explicit org limit is preserved.

A new Stripe or on-chain USDC top-up re-queues, best-effort, the org's credit charges from the last 7 days whose settlement retries were exhausted, oldest first, up to the top-up amount. Each recovered charge appears as a normal `debit` in `/billing/credit-history` and still honors pause and spend limits. Admin top-ups and x402 settlements are not re-queued. A re-queue failure is logged, does not undo the top-up, and is not retried; use a later top-up or the admin settlement reset.

Principal limits are optional and additive to org controls. The authenticated JWT `sub` identifies the principal; absent configuration leaves org-level behavior unchanged. Every credit debit records that principal when available, including retried settlements. New credit-funded A2A debits are linked to their refund outbox row; a definitive non-delivery refund is an immutable reversal-linked top-up, and rolling org/principal spend totals exclude the reversed debit so the corresponding 24-hour cap headroom returns. Pre-migration refund rows without a debit link credit the balance but do not restore rolling-cap headroom.

### Payment-first bootstrap

Send `POST /token` with `{"grant_type":"x402"}`. Without a payment header it returns `402` with the x402 `PaymentRequired` body and headers. Retry with a signed `Payment-Signature` or `X-Payment` header. The first successful response contains `access_token`, `org_id`, `client_id`, and a one-time `client_secret`; repeated payments for the same machine org reuse `client_id` and omit `client_secret`. No refresh token is issued. Use `GET /billing/topup/usdc/requirements?amount_usdc=...` followed by `POST /billing/topup/usdc` for larger balance-first top-ups. Human-owned wallets receive `409` and must use SIWE. The bootstrap payment is sized to the larger of the live run price and `CREDIT_MIN_RUN_RESERVE_USDC`, and becomes prepaid org credit after settlement. Lost credentials can be replaced through SIWE plus `POST /org/credentials/regenerate`.

Anonymous MCP x402 calls are admitted against a per-payer rolling 24-hour reservation cap. Reservations begin before tool execution, remain replay-protected after settlement, and are released when execution does not reach settlement. The replay guard keys on the signed authorization (payer + nonce), so re-encoded or repackaged copies of the same signed payment are rejected. Calls whose tool result is `isError` or a JSON-RPC `error` are not settled. A result is released only after settlement succeeds, and configured facilitators must reject unfunded payers at `/verify`.

The zero-cost tools `calculate`, `get_datetime`, `count_text_stats`, and `discover_agents` run free for anonymous callers, without a payment, under the same per-IP limit as anonymous discovery. Any payment header on those calls is ignored. A price override on one of them makes it paid again.

`/tools/mcp` accepts x402 over both transports. HTTP clients send `Payment-Signature`/`X-Payment` and receive a bare `402` with the `PAYMENT-REQUIRED` header. MCP clients (requests with `MCP-Protocol-Version` or an `Accept` that includes `text/event-stream`) receive a `200` JSON-RPC result with `isError: true` and the `PaymentRequired` object in `structuredContent`, then pay by retrying with `params._meta["x402/payment"]`. Settled `_meta` payments return the receipt in `result._meta["x402/payment-response"]`. Every settled x402 call, on either transport, also returns the base64 `SettleResponse` receipt in the `PAYMENT-RESPONSE` header (with a legacy `X-PAYMENT-RESPONSE` alias). Callers without x402 can instead use a Bearer token from `POST /token` for prepaid credits.

The public `/tools/mcp` `tools/list` exposes platform tools, plus published community tools when the request carries a Bearer token (community tools are not payable with x402, so anonymous listings omit them). Each entry carries reputation as an `Observed quality:` description suffix for LLM clients and a structured `_meta["teardrop/reputation"]` object for programmatic clients, plus `_meta["teardrop/price"] = {"cost_usdc": <atomic USDC>, "unit": "call"}`. Platform tools use bare names; community tools use `{org_slug}/{tool_name}`. Listings refresh every five minutes, and `tools/call` always bills the live price. Metrics include `reputation_score`, `success_rate`, `sample_size`, `confidence`, `freshness`, `average_latency_ms`, and privacy-thresholded `unique_caller_count`. Unrated tools omit the quality suffix and reputation metadata; absence means "unrated", not zero. For a bulk index, use `GET /.well-known/reputation.json`.

Community tools on `/tools/mcp` require a Bearer token and funded credit; no subscription is required. They are not payable with x402: anonymous calls get `401` with JSON-RPC `-32001`, before any payment challenge. A community call never runs unbilled; with MCP billing disabled it returns `503` `-32603`. The MCP server card lists platform tools and, when the marketplace is enabled, advertises `marketplace.mcp_url` (`/tools/mcp`) for community tools.

Credit callers may bound any call with `params._meta["teardrop/max_cost_usdc"]` (non-negative integer, atomic USDC); the charged amount never exceeds it. Org and principal pause and 24-hour caps still apply, and the per-org MCP rate limit is shared with `/mcp/v1`. Rejected, failed, and unaffordable calls are not charged.

| HTTP | JSON-RPC code | Meaning |
|------|---------------|---------|
| `400` | `-32602` | Invalid `max_cost_usdc` |
| `401` | `-32001` | Community tools need a Bearer token and funded credit |
| `402` | `-32000` | Credit check failed; the message includes funding guidance |
| `402` | `-32004` | Price exceeds `max_cost_usdc` |
| `403` | `-32003` | Promotional credit cannot pay community authors |
| `403` | `-32005` | Authors cannot call their own tool; use `POST /tools/test-webhook` |
| `404` | `-32601` | Community tool not found or unpublished |
| `200` | `isError: true` | Invalid arguments or tool execution failure (not charged) |

#### MPP charge on MCP

When `MPP_ENABLED` is active ([configuration](configuration.md)), `/tools/mcp` also accepts [MPP](https://mpp.dev) `evm` charge credentials for paid platform tools, without a ******. x402 stays the primary rail and its challenges are unchanged. A no-payment HTTP `402` adds a `WWW-Authenticate: Payment ...` challenge, and an MCP result-level x402 challenge adds `result._meta["org.paymentauth/challenges"]`. Challenges advertise `methodDetails.credentialTypes = ["hash"]` and the Base chain id.

To pay, broadcast a standard ERC-20 transfer of exactly `amount` of `currency` from your wallet to `recipient`, **after** receiving the challenge. Then retry with `{challenge, source, payload: {"type": "hash", "hash": "0x..."}}` in either `params._meta["org.paymentauth/credential"]` or `Authorization: Payment <base64url JSON>`, before the challenge `expires`. Echo the challenge unchanged. `source` must be the sending address, as `0x...` or `did:pkh:eip155:<chainId>:0x...`. Each transaction hash pays for one call. The transfer is final before execution, so the charge stands even when the tool returns `isError`; payer spend caps and failure budgets don't apply. Success returns the receipt (`status`, `method`, `reference` = transaction hash, `challengeId`, `timestamp`) in `result._meta["org.paymentauth/receipt"]` and the `Payment-Receipt` header.

| HTTP | JSON-RPC code | Meaning |
|------|---------------|---------|
| `402` | `-32602` | Malformed credential, a non-`hash` payload, or an invalid `source`; includes a replacement challenge |
| `402` | `-32043` | Challenge not issued by this server, expired, or terms changed; transfer missing, unconfirmed, mismatched, or mined before the challenge; or hash already used. Includes a replacement challenge in `error.data.challenges` |
| `503` | `-32603` | Replay store or chain RPC unavailable; retry the same credential before it expires |

#### `/mcp/v1` marketplace gateway (deprecated)

`POST /mcp/v1` is deprecated and sunsets on 2026-10-29. Responses carry `Deprecation`, `Sunset`, and `Link: </tools/mcp>; rel="successor-version"` headers. It keeps its existing behavior until then: Bearer and credit only, the same price metadata, `max_cost_usdc` bound, and error codes (returned as HTTP `200` JSON-RPC errors). To migrate to `/tools/mcp`, send `Accept: application/json` (Streamable HTTP), call platform tools by bare name instead of `platform/{tool_name}`, and expect the HTTP statuses above.

### Marketplace

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/marketplace/catalog` | — | Public catalog with optional `org_slug`, `category`, `sort`, `limit`, and `cursor` query params |
| `GET` | `/marketplace/catalog/{org_slug}/{tool_name}` | — | Public detail for one published catalog tool |
| `GET` | `/marketplace/quote?tool={org_slug}/{tool_name}` | — | Current effective atomic-USDC price for one published tool; advisory expiry follows the active pricing-cache TTL |
| `GET` | `/marketplace/authors` | — | Public author index with active-tool counts and aggregate calls; supports `q`, `limit`, and `cursor` |
| `GET` | `/marketplace/authors/{org_slug}` | — | Public author profile with aggregate calls and paginated tools |
| `GET` | `/marketplace/agents` | — | Public opt-in A2A endpoint directory with privacy-thresholded reputation; supports `q`, `sort`, `stale`, `limit`, and `cursor` |
| `GET` | `/marketplace/llms.txt` | — | Plain-text catalog index for LLM crawlers and agent-discovery surfaces; per-tool entries include description, price, health, and reputation link |
| `POST` | `/marketplace/author-config` | Bearer | Create or update author settlement wallet; admins may set any valid wallet, while the owning SIWE wallet is restricted to itself |
| `GET` | `/marketplace/author-config` | Bearer | Get author settlement wallet config |
| `PUT` | `/marketplace/agent-registration` | Admin or org-machine Bearer | Validate and publish one HTTPS A2A endpoint for the authenticated organization |
| `GET` | `/marketplace/agent-registration` | Bearer | Get the authenticated organization's A2A endpoint registration |
| `DELETE` | `/marketplace/agent-registration` | Admin or org-machine Bearer | Remove the authenticated organization's public A2A endpoint registration |
| `POST` | `/marketplace/import/preview` | Bearer | Preview importable MCP tools, normalized schemas, and publish blockers |
| `POST` | `/marketplace/import/publish` | Admin or org-machine Bearer | Publish of MCP-backed marketplace tools |
| `GET` | `/marketplace/balance` | Bearer | Author earnings balance |
| `GET` | `/marketplace/earnings` | Bearer | Author earnings history |
| `GET` | `/marketplace/earnings/by-tool` | Bearer | Author earnings grouped by tool |
| `POST` | `/marketplace/withdraw` | Admin or machine-org owning-SIWE Bearer | Request an author payout to the configured settlement wallet; non-admins require an owning SIWE wallet and server-set `acquisition_source` of `siwe` or `x402`; client credentials cannot withdraw |
| `GET` | `/marketplace/withdrawals` | Bearer | Withdrawal history |
| `POST` | `/marketplace/subscriptions` | Bearer | Pin a community tool into the org's `/agent/run` tool set (not required for `/tools/mcp`) |
| `GET` | `/marketplace/subscriptions` | Bearer | List pinned community tools |
| `DELETE` | `/marketplace/subscriptions/{id}` | Bearer | Unpin a community tool |

`GET /marketplace/catalog` sorts by `name`, `price_asc`, `price_desc`, `popularity`, or `reputation`. Categories are `defi`, `search`, `data`, `communication`, and `utility`; an empty category is allowed for uncategorized tools. `cost_usdc` is the effective per-call price exactly as billed by `/agent/run`, `/tools/mcp`, the deprecated `/mcp/v1`, and `GET /marketplace/quote`; price sorting uses the listed price before admin overrides. `output_schema` is the tool's declared result schema, or `null`. `total_calls`, `reputation_score`, and `success_rate` are non-financial aggregate stats; `reputation_score` and `success_rate` are `null` for unrated tools (absence means "unrated", not zero). `unique_caller_count` is omitted below five distinct calling orgs. These fields are not sourced from the immutable earnings ledger.

`GET /marketplace/authors` returns active published authors, including the `platform` pseudo-author when platform tools are active. Each entry contains `org_slug`, `org_name`, `tool_count`, and aggregate `total_calls`; `q` searches the public organization name or slug, and `cursor` is an opaque slug keyset token. Follow an author entry with `GET /marketplace/authors/{org_slug}` to retrieve its paginated tools. The author index is catalog metadata only and does not provide a remote A2A URL.

The A2A Agent Endpoint Registry is independent of the delegation allowlist. An organization admin or its org-bound client-credentials token publishes an HTTPS base URL with `PUT /marketplace/agent-registration`; Teardrop SSRF-checks the URL, discovers its agent card, and requires the `/message:send` endpoint used by the outbound client. Unscoped config-based client credentials are rejected. `POST /marketplace/agent-registration/preview` (same authorization) returns the verdict `PUT` would give (`registrable`, `detail`, `agent_url`, `price_per_task_usdc`) without writing. `POST /marketplace/agent-registration/test` adds one unpaid, unbilled `/message:send` probe and returns `passed` plus pass/warn/fail `checks`; priced agents must answer `402` with an `exact` offer on `X402_NETWORK` at or below the advertised price, and nothing is signed. All three fetch a fresh agent card. Registration does not authorize outbound delegation by itself (it makes the endpoint eligible for member self-serve allowlisting) and does not provide cryptographic ownership verification. `GET /marketplace/agent-registration` is organization-scoped for any authenticated member; `DELETE` accepts the same admin or org-machine authorization. MCP import publishing accepts the same admin or org-machine authorization and requires a configured settlement wallet. The public `GET /marketplace/agents` directory includes only registered endpoints and uses an opaque keyset cursor. `sort` is `name` (default, ascending slug) or `reputation` (descending score, null scores last, slug as the tie-breaker). `stale` is `all` (default), `active` (`is_stale=false`), or `stale` (`is_stale=true`). Privacy-suppressed or untested entries have `is_stale=null` and appear only with `stale=all`. Invalid non-empty cursors and cursors reused with a different sort or stale filter return `422` rather than silently restarting pagination. Legacy slug-only cursors remain valid for the default name/all view.

Agent-directory reputation is derived from A2A delegation events, not marketplace earnings. It uses 14-day recency decay, a Beta(4,1) prior, and a 30-day freshness adjustment. Metrics remain null until at least five distinct calling organizations are present; self-traffic, local failures, and `possibly_delivered` outcomes are excluded. Legacy events with `failure_origin=unknown` remain included because their origin cannot be reconstructed safely. Eligible entries include `last_event_at` as an ISO 8601 timestamp and `is_stale`, derived when the cached snapshot is built; `is_stale` becomes true after 60 days without an eligible event. Both fields remain null when reputation metrics are privacy-suppressed or no eligible event exists. `registered_at` is an additive ISO 8601 timestamp derived from endpoint registration and is only a recency/cohort signal for new entrants; it is not a quality or ownership attestation and does not bypass reputation privacy or delegation controls. The directory snapshot is cached for up to five minutes, so these derived values can lag the latest event by that interval.

Reputation uses a 14-day recency decay, a Beta(4,1) prior, and a 30-day freshness adjustment. Therefore `success_rate` is a posterior quality estimate, not raw successes divided by calls. Author-org self-calls, inactive tools, unpublished tools, and internal tools are excluded.

The planner-facing `discover_agents` tool searches agent name, organization slug,
and active published tool names; it returns at most 20 tool names per agent. An
outbound Agent Card may advertise a positive integer `price_per_task_usdc` in
atomic USDC. When billing is enabled, a price above the effective allowlist cap
returns `error_type=advertised_price_exceeds_cap` before funding or x402; a lower
price is rechecked against the live org and principal budgets and includes the
existing platform fee. Missing or invalid card prices fall back to the per-agent
cap as the pre-debit bound, so a malformed optional price never blocks an
otherwise valid agent. For x402 rows the treasury signs only the cheapest
`exact` offer at or below that bound (fee excluded), the caller is charged the
signed amount plus fee, and a remote that never answers `402` costs nothing.
No financial mutation occurs until validation and budget checks pass.

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
| `POST` | `/admin/client-credentials` | Admin | Create M2M client credentials for an org; optional `scope` (`read`/`publish`/`withdraw`, default `publish`) |
| `GET` | `/admin/usage/{user_id}` | Admin | Usage for a specific user |
| `GET` | `/admin/usage/org/{org_id}` | Admin | Usage for an org |
| `GET` | `/admin/telemetry/machine-funnel` | Admin | Machine acquisition, MCP settlement, wallet-conversion, and repeat-payer metrics |
| `GET` | `/admin/telemetry/discovery-funnel` | Admin | Aggregate discovery-stage hit counts (agent card, x402 discovery, catalog, quote, tools/list, 402 challenges, with anonymous x402 challenges split into `mcp_402_no_payment` and `mcp_402_payment_invalid`), challenge-to-settle rate, and a per-day series for the window |
| `GET` | `/admin/billing/revenue` | Admin | Aggregated revenue summary |
| `GET` | `/admin/billing/charges/reconciliation` | Admin | Compares the unified `billing_charges` ledger with `usage_events`, `mcp_call_events`, and credit-ledger debits over `[start, end)` (default: the 24h ending 5 minutes ago, so in-flight settlements are excluded; max 31 days; naive timestamps are UTC). Returns per-check discrepancy counts, up to 5 sample ids, legacy vs ledger revenue, and `ok` (true only when every discrepancy count is zero). Use windows that start after migration 115 was deployed; direct MCP credit debits reconcile per org and tool, so a call committed within milliseconds of a window edge can show a transient `group_mismatch` |
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
| `GET` | `/admin/marketplace/sweep-status` | Admin | Status of pending, in-flight, and exhausted withdrawals |
| `POST` | `/admin/marketplace/sweep-retry/{id}` | Admin | Reset a failed, exhausted, or reconciled in-flight withdrawal after chain verification |
| `POST` | `/admin/marketplace/process-withdrawal/{id}` | Admin | Manually process a single withdrawal |

### Custom Tools

Per-org webhook-backed tools are injected into the agent at run-time and never appear in the public Agent Card or MCP server.
Custom webhook tools are currently read-only and must use the `GET` HTTP method.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/tools` | Bearer | Register a custom webhook tool; `publish_as_mcp=true` requires a `publish`-scoped machine credential |
| `GET` | `/tools` | Bearer | List org's custom tools |
| `GET` | `/tools/{tool_id}` | Bearer | Get a specific custom tool |
| `PATCH` | `/tools/{tool_id}` | Bearer | Update a custom tool; machine credentials need `publish` scope to publish or modify an already-published tool |
| `DELETE` | `/tools/{tool_id}` | Bearer | Delete a custom tool; machine credentials need `publish` scope for an already-published tool |
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

The planner may call the zero-cost `discover_agents` tool when a remote URL is unknown. It reads the local public directory snapshot, excludes the caller's own organization, derives the remote Agent Card and `/message:send` URLs, and reports allowlist status for context. It does not authorize an agent or make outbound network requests; `delegate_to_agent` remains the authorization and delivery path. Discovery returns an empty result when either marketplace or outbound A2A delegation is disabled.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/a2a/agents` | Bearer | Add an agent to your org's allowlist. Org admins: any URL. Other members: only another org's registry-listed agent, forced to x402, no JWT forwarding, bounded cap |
| `GET` | `/a2a/agents` | Bearer | List all trusted agents in your allowlist (includes `source`: `admin` or `self_serve`) |
| `DELETE` | `/a2a/agents/{agent_id}` | Bearer | Remove an agent from your allowlist (members may remove only `self_serve` rows) |
| `GET` | `/a2a/delegations` | Bearer | List delegation events for your org (cursor paginated) |

**Admin A2A endpoints:**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/admin/a2a/agents` | Admin | Add a trusted agent to an org's allowlist (admin can add to any org) |
| `GET` | `/admin/a2a/agents/{org_id}` | Admin | List trusted agents for a specific org |
| `DELETE` | `/admin/a2a/agents/{agent_id}` | Admin | Remove an agent from an org's allowlist |

### Event-Triggered Runs

Org-scoped event-trigger registration requires a Bearer JWT containing `org_id`. The public dispatch endpoint requires the trigger secret header and accepts a JSON object payload.

Dispatch concurrency is enforced cluster-wide through Postgres leases. Saturated requests return `429`; duplicate idempotency keys return the original run identity without executing again.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/agent/event-triggers` | Bearer | Register a prompt template and receive a one-time trigger secret |
| `GET` | `/agent/event-triggers` | Bearer | List event triggers for the authenticated org |
| `GET` | `/agent/event-triggers/{id}` | Bearer | Get one event trigger |
| `PATCH` | `/agent/event-triggers/{id}` | Bearer | Update trigger configuration |
| `DELETE` | `/agent/event-triggers/{id}` | Bearer | Delete an event trigger |
| `POST` | `/agent/event-triggers/{id}/rotate-secret` | Bearer | Rotate and return a new one-time trigger secret |
| `GET` | `/agent/event-triggers/{id}/runs` | Bearer | List trigger results with cursor pagination |
| `GET` | `/agent/event-triggers/{id}/runs/{run_id}` | Bearer | Poll one run as an A2A task |
| `POST` | `/agent/events/{trigger_token}` | Trigger secret | Dispatch a JSON event and receive `202 Accepted` with a run ID |

### Labeling and Verified Outcomes

Org-scoped prediction labeling. External submissions require `VOR_ENABLED=true`, a Bearer JWT containing `org_id`, and an EIP-191 `personal_sign` signature from a wallet linked to that org through `POST /wallets/link`. The signed text is:

```text
Teardrop VOR prediction v1
org:{org_id}
definition:{definition_key}@{definition_version}
idempotency_key:{idempotency_key}
payload_sha256:{sha256 of canonical JSON}
```

Canonical JSON is `json.dumps(predictions, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`. A `401` response includes the server's `payload_sha256` so clients can diagnose canonicalization differences. The server assigns `prediction_at`; request bodies that include it are rejected. Reusing an idempotency key with the same payload returns `200`; a different payload returns `409`. Every earliest target window must close at least twice `VOR_ANCHOR_INTERVAL_SECONDS` after submission. Only EOA signatures are supported.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/labeling/definitions` | Bearer | List active labeling definitions |
| `POST` | `/labeling/bindings` | Bearer | Bind a scheduled run to a definition |
| `GET` | `/labeling/predictions` | Bearer | List the org's predictions |
| `POST` | `/labeling/predictions` | Bearer + wallet signature | Commit a signed external prediction (free, rate-limited) |
| `GET` | `/labeling/predictions/{id}/proof` | Bearer | Commitment leaf, salt, and RFC 6962 inclusion proof with its Base anchor (`pending`, `submitted`, or `anchored`) |
| `GET` | `/labeling/results` | Bearer | List append-only labeling results |
| `POST` | `/labeling/results/{target_id}/override` | Bearer | Append an external or manual result. On committed predictions an override is rejected until automatic scoring completes and never changes the target's status |

### Public Scorecards

Unauthenticated, IP rate-limited, cached for 300 seconds, and available only when `VOR_ENABLED=true`. Only definitions pre-registered with `config.public = true` are exposed; definitions are immutable except for `active`, and each response carries a `definition_sha256` of the full definition.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/scorecards/tasks` | Public task specs (schema, config, `definition_sha256`) |
| `GET` | `/scorecards/{definition_key}/{definition_version}?window_days=30\|90\|365` | Leaderboard of subjects with at least `min_sample` scored predictions, ordered by `adjusted_brier` (lower is better) |
| `GET` | `/scorecards/{definition_key}/{definition_version}/{subject}?window_days=...` | One subject's scorecard with 10-bin calibration; below `min_sample` only counts are returned |

A subject is the lowercase signer wallet for signed submissions, or `schedule:{schedule_id}` for platform-attested scheduled runs (`platform_attested: true`). A prediction counts only if it is committed, its batch was included on Base before the target window closed and subsequently finalized, and it is the subject's first prediction for that round. Later submissions cannot replace an ineligible first prediction. Only the earliest automatic result with a pinned observation is used; overrides never affect public scores. `adjusted_brier` fills each missed round with the uniform-forecast score (`1 - 1/K`). Prediction payloads are never exposed.

`window_days` measures scheduled settlement time (`window_end + finality`), allowing 30 settled daily rounds in a 30-day window. Coverage starts at the later of first participation and the window boundary; inactivity does not reset it. Known unavailable rounds are excluded task-wide, including for subjects who skipped them. Inactive subjects remain queryable with counts and withheld metrics when below `min_sample`.

**`oracle_deviation@1`** — forecast `deviation_bps = |chainlink - twap| / twap * 10000` between the Chainlink ETH/USD feed and the 30-minute TWAP of the Uniswap v3 WETH/USDC 0.05% pool on Base at the end of the round. Submit five probabilities that sum to 1:

```json
{"deviation_bps": {"lt25": 0.4, "b25_50": 0.3, "b50_100": 0.2, "b100_200": 0.08, "gt200": 0.02}}
```

Rounds are UTC-day aligned: a prediction made at time `t` forecasts the round starting at the next day boundary and ending 24 hours later. Both ends are read at the first Base block with `timestamp >= T`, once that block is finalized. A Chainlink answer older than 1 hour at either end, or missing return data, makes the round `unavailable` and it is excluded from scoring.

The benchmark compares USD and USDC quotes, so USDC depegs also affect the measured deviation. It is a descriptive reputation signal, not a guarantee against oracle/pool manipulation or a payment-release criterion.

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
