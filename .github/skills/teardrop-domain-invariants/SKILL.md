---
name: teardrop-domain-invariants
argument-hint: "Describe the feature, bug, or change involving payments, billing, marketplace, MCP, A2A, or auth."
description: "Use for any work touching Teardrop billing (x402/credits/Stripe), pricing, marketplace (platform tools vs org tools), MCP gateway, A2A delegation, SSRF, or financial database migrations. Provides authoritative invariants and file map."
disable-model-invocation: false
metadata: teardrop, billing, x402, credits, stripe, marketplace, platform tools, org tools, mcp, a2a, delegation, ssrf, pricing, atomic usdc, auth_method, billing_method, settlement, financial, payments, wallet, siwe, migration
user-invocable: true
---

You are a domain expert for Teardrop's payments, marketplace, and protocol surfaces.

## Critical: Verify Before Trusting Memory
- Treat live source code as the source of truth.
- Treat /memories/repo notes as secondary references.
- If a memory note says "missing", "pending", "TODO", or "warning", verify against live code before acting.
- Prefer app.py, billing/__init__.py, marketplace/__init__.py, agent/nodes.py, and migrations/versions/ over notes.

## Non-Negotiable Invariants
1. Atomic USDC convention is fixed: 1_000_000 = $1.00 and 10_000 = $0.01. Money values are BIGINT, never float.
2. auth_method drives billing gate dispatch: siwe with credit uses verify_credit, siwe without credit uses x402, email/client_credentials use verify_credit.
3. billing_method drives settlement dispatch: credit path debits credit ledger, x402 path performs on-chain settlement.
4. Tool cost resolution precedence must remain intact: overrides -> marketplace price -> global fallback.
5. Platform tools (platform/tool_name) and org marketplace tools (org_slug/tool_name) are distinct access and billing modes.
6. billable_tool_calls and billable_tool_names are for cost accounting; do not substitute total attempt counters blindly.
7. All outbound URLs must pass SSRF validation (validate_url or async_validate_url) before network calls.
8. Stripe webhook processing must remain idempotent and retriable on transactional failure.
9. Credit debit paths must preserve row locking semantics (SELECT FOR UPDATE) to prevent double-spend.
10. Financial side effects must preserve immutable audit trails.
11. Migration changes are additive-first and backward-compatible by default.
12. Cache invalidation is required after any mutation affecting cached pricing/catalog/tool surfaces.
13. Secret material (private keys, API credentials, tokens) must never be logged or echoed in user-facing errors.

## Security Checklist
- [ ] SSRF checks exist for every outbound URL path.
- [ ] auth_method and billable_auth_methods gates are preserved.
- [ ] billing_method settlement routing is preserved.
- [ ] No secrets in logs, exceptions, or HTTP error details.
- [ ] Replay/double-settlement protections remain intact.
- [ ] org_id scoping is enforced for org-owned resources.
- [ ] Input schemas validate bounds and format at boundaries.
- [ ] Financial mutations emit immutable audit records.
- [ ] Migration path is additive and rollback-aware.
- [ ] Cache invalidation happens on mutable pricing/catalog writes.

## Critical File Map
- Billing gate and run settlement orchestration: app.py
- Pricing and billing calculations: billing/__init__.py
- Marketplace catalog and pricing lookups: marketplace/__init__.py
- MCP middleware auth/billing gate: mcp_gateway.py
- Delegation tool implementation: tools/definitions/delegate_to_agent.py
- A2A SSRF and outbound client flow: a2a_client.py
- Planner/tool executor behavior: agent/nodes.py
- Org tool CRUD and publish paths: org_tools/__init__.py
- Migration chain and billing schema evolution: migrations/versions/

## SDK Surface Awareness
- If behavior changes affect billing gate semantics, SSE event contracts, auth_method values, billing_method values, or tool-name/qualified-name formats, update prompts/SDK-HANDOFF/03_SDK_HANDOFF.md.

## Output Contract
When this skill is used for implementation or review, report:
- Files changed
- Invariants preserved
- Security checklist status
- Whether SDK handoff updates are required
