# Marketplace Onboarding Guide

This guide walks you through publishing an MCP tool to the Teardrop marketplace
and collecting earnings.  All amounts are in **USDC atomic units** (6 decimals).

| Human amount | Atomic units |
|---|---|
| $0.001 (0.1¢) | 1,000 |
| $0.01 (1¢) | 10,000 |
| $0.10 (10¢) | 100,000 |
| $1.00 | 1,000,000 |
| $5.00 | 5,000,000 |

---

## Prerequisites

- An active Teardrop account with API access.
- A valid **EIP-55 checksummed** Ethereum / Base address for settlement payouts.
  Use `Web3.to_checksum_address(addr)` if you are unsure — all lowercase or
  uppercase addresses will be rejected.
- A publicly reachable HTTPS webhook URL that implements your tool logic.

---

## Step 1 — Authenticate

All marketplace endpoints require a valid JWT.  Obtain one via your usual login
flow and set it in every request:

```bash
TOKEN="<your-JWT>"
```

---

## Step 2 — Register a Settlement Wallet

You must register a wallet before any tool can be published.

```bash
curl -X POST https://api.teardrop.ai/marketplace/author-config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"settlement_wallet": "0xYourChecksumAddress"}'
```

**Response:**
```json
{
  "org_id": "your-org-id",
  "settlement_wallet": "0xYourChecksumAddress",
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z"
}
```

The wallet can be updated at any time with the same endpoint.  Pending
earnings are always settled to the wallet recorded **at withdrawal time**.

---

## Step 3 — Create the Tool

Create your webhook-backed tool via `POST /tools`.  The `input_schema` field
must be a valid JSON Schema object describing the tool's parameters.

```bash
curl -X POST https://api.teardrop.ai/tools \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "weather_lookup",
    "description": "Returns current weather for a given city.",
    "input_schema": {
      "type": "object",
      "properties": {
        "city": {"type": "string", "description": "City name"}
      },
      "required": ["city"]
    },
    "webhook_url": "https://your-service.example.com/weather",
    "webhook_method": "POST",
    "base_price_usdc": 10000,
    "publish_as_mcp": false
  }'
```

> **`base_price_usdc`** — the amount callers pay per invocation. The platform
> applies a **70 / 30 revenue split**: 70 % goes to you, 30 % to Teardrop.
> This split is fixed; per-author overrides are not supported.

---

## Step 4 — Publish to the Marketplace

Enable marketplace visibility by patching the tool:

```bash
TOOL_ID="<tool-id-from-step-3>"

curl -X PATCH https://api.teardrop.ai/tools/$TOOL_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "publish_as_mcp": true,
    "marketplace_description": "Real-time weather data for any city worldwide."
  }'
```

Your tool now appears in the public catalog at `GET /marketplace/catalog`.

> Soft-deleting (`DELETE /tools/<id>`) a published tool automatically
> deactivates all subscriber subscriptions so callers are not left with
> a broken reference.

---

## Step 5 — View Your Earnings

```bash
# All earnings, most recent first (up to 50 per page)
curl https://api.teardrop.ai/marketplace/earnings \
  -H "Authorization: Bearer $TOKEN"

# Filter by tool name
curl "https://api.teardrop.ai/marketplace/earnings?tool_name=weather_lookup" \
  -H "Authorization: Bearer $TOKEN"

# Paginate
curl "https://api.teardrop.ai/marketplace/earnings?cursor=<next_cursor>&limit=20" \
  -H "Authorization: Bearer $TOKEN"
```

**Response:**
```json
{
  "earnings": [
    {
      "id": "earn-abc123",
      "tool_name": "weather_lookup",
      "caller_org_id": "subscriber-org",
      "total_cost_usdc": 10000,
      "author_share_usdc": 7000,
      "platform_share_usdc": 3000,
      "status": "settled",
      "created_at": "2026-01-15T12:34:56Z"
    }
  ],
  "next_cursor": "2026-01-15T12:34:56Z"
}
```

Pass `next_cursor` as the `cursor` query parameter in the next request to
fetch the following page.  A `null` `next_cursor` means you are on the last page.

---

## Step 6 — Check Your Balance

```bash
curl https://api.teardrop.ai/marketplace/balance \
  -H "Authorization: Bearer $TOKEN"
```

**Response:**
```json
{
  "available_usdc": 350000,
  "pending_usdc": 0
}
```

`available_usdc` is the amount you can withdraw right now.

---

## Step 7 — Request a Withdrawal

Withdrawals transfer your available earnings to the settlement wallet in one
atomic on-chain USDC transfer.  The minimum withdrawal amount is 1,000 units
($0.001).

```bash
curl -X POST https://api.teardrop.ai/marketplace/withdraw \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount_usdc": 350000}'
```

**Response (HTTP 201):**
```json
{
  "id": "w-xyz789",
  "org_id": "your-org-id",
  "amount_usdc": 350000,
  "wallet": "0xYourChecksumAddress",
  "status": "pending",
  "created_at": "2026-01-20T08:00:00Z"
}
```

---

## Step 8 — Track Withdrawal Status

```bash
# All withdrawals, newest first
curl https://api.teardrop.ai/marketplace/withdrawals \
  -H "Authorization: Bearer $TOKEN"
```

**Response:**
```json
{
  "withdrawals": [
    {
      "id": "w-xyz789",
      "amount_usdc": 350000,
      "wallet": "0xYourChecksumAddress",
      "tx_hash": "0xabc...def",
      "status": "settled",
      "created_at": "2026-01-20T08:00:00Z",
      "settled_at": "2026-01-20T08:02:14Z"
    }
  ],
  "next_cursor": null
}
```

| Status | Meaning |
|---|---|
| `pending` | Queued for processing |
| `processing` | On-chain transfer initiated |
| `settled` | USDC confirmed on-chain |
| `failed` | Transfer failed; contact support |

Pagination works the same way as earnings: pass `next_cursor` as `cursor`.

---

## Troubleshooting

| Error | Resolution |
|---|---|
| `422 settlement wallet` on publish | Complete Step 2 first |
| `422 invalid checksum` | Use EIP-55 format (mixed-case hex) |
| Withdrawal stays `pending` for >30 min | The auto-sweep runs every 5 minutes; if still stuck, contact support who can trigger an admin reset |
| `429 Too Many Requests` on catalog | Back off for 60 seconds (`Retry-After` header) |
