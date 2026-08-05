---
topic: improvements
status: draft
generated_at_utc: "2026-08-05T03:54:49.284041+00:00"
evidence_commit: "d0d19feb1ae52b6e73caf91e6b80813189ea479c"
evidence_dirty: true
source_manifest_sha256: "d5f748fd204f4a9b4164304db3f50d151d9fd85b3e690c0b9ca40d5111d9ccd6"
source_redaction_count: 0
report_source: "local"
prompt_version: 2
query: "For marketplace tool billing, does resolve_tool_cost preserve override -> marketplace price -> global fallback precedence and cache freshness for platform and org tools, and what single incremental fix is justified?"
---

# Product improvement research

> This report is unverified research. Do not treat a suspected security issue as confirmed until a human verifies it against the cited live code.

## Research scope

- `billing/pricing.py`
- `marketplace/_catalog_pricing.py`
- `marketplace/catalog.py`
- `marketplace/subscriptions.py`
- `teardrop/mcp_gateway.py`
- `tests/unit/test_platform_tools.py`
- `tests/api/test_mcp_gateway.py`

## Report

## Executive conclusion

`resolve_tool_cost` in `billing/pricing.py` correctly implements the intended precedence: admin overrides (full or bare name) win, then marketplace price (platform or org author price), then the global fallback (`default_cost`). The MCP `__` shortcut correctly returns 0 unless overridden. However, cache freshness for platform and org tool prices is not guaranteed when a tool’s price or active status changes, because the invalidation functions (`_invalidate_platform_tool_cache`, `_invalidate_all_org_tool_price_cache`) are not invoked by the update/deactivate code paths. This can lead to stale billing for up to 60 seconds (the hardcoded TTL). The single incremental fix is to add explicit cache invalidation calls to the marketplace tool update/deactivate endpoints.

## Improvement candidates

1. **Cache invalidation on marketplace tool price/status changes** (priority: high, confidence: high)  
   - User/business value: Prevents incorrect billing when an admin changes a tool’s price or deactivates it; ensures the marketplace price is immediately reflected in `resolve_tool_cost`.  
   - Current behavior: `_load_platform_tool_price` and `_load_org_tool_price` query the DB with `is_active = TRUE`, but the TTL caches (60s) are not invalidated when the underlying row changes.  
   - Proposed change: Call `_invalidate_platform_tool_cache()` and `_invalidate_all_org_tool_price_cache()` after any update to `marketplace_platform_tools` or `marketplace_org_tools` (price change, deactivation, reactivation).  
   - Affected API/schema: No schema change; internal cache invalidation only.  
   - Effort: Low (a few lines in the update endpoints).  
   - Risk: Low; invalidation is idempotent and already used in tests.  
   - Dependencies: None.  
   - Security/OWASP: No direct impact.  
   - Financial/ledger: Prevents over/under-billing due to stale prices.  
   - Tests: Add unit tests that update a price and assert the next `get_platform_tool_price`/`get_org_tool_price_by_qualified_name` returns the new value without waiting for TTL.  
   - Observability: Log invalidation events.  
   - Rollout: Deploy with the update endpoints; no migration needed.  
   - Rollback: Revert the invalidation calls; TTL will restore eventual consistency.

2. **Unify TTL configuration** (priority: low, confidence: medium)  
   - The platform/org price TTLs are hardcoded to 60s while the overrides cache uses `pricing_cache_ttl_seconds`. Making them configurable would improve operational flexibility, but this is not a correctness issue.

3. **Add explicit tests for precedence edge cases** (priority: medium, confidence: high)  
   - Existing tests cover many cases, but there is no test for a qualified tool with an override on the full name (e.g., `acme/weather` in overrides) vs bare name. Adding such tests would lock in the intended precedence.

## Current behavior and evidence

- `resolve_tool_cost` in `billing/pricing.py` implements the precedence:
  - For qualified names (`/` present): checks `tool_name in overrides` first, then `bare_tool_name in overrides`, then if `marketplace_enabled` calls `get_org_tool_price_by_qualified_name`, else returns `default_cost`.
  - For non-qualified names: checks `tool_name in overrides`, then if `marketplace_enabled` calls `get_platform_tool_price`, else returns `default_cost`. The MCP `__` shortcut returns 0 unless overridden (evidenced by tests).
- Cache definitions in `marketplace/_catalog_pricing.py`:
  - `_PLATFORM_TOOL_PRICE_TTL_SECONDS = 60` and `_ORG_TOOL_PRICE_TTL_SECONDS = 60`.
  - `_get_platform_tool_cache` and `_get_org_tool_price_cache` create `TTLCache` instances with these TTLs.
  - `_load_platform_tool_price` queries `marketplace_platform_tools` with `is_active = TRUE`; `_load_org_tool_price` likely similar (not shown but inferred).
- Invalidation functions exist: `_invalidate_platform_tool_cache` and `_invalidate_all_org_tool_price_cache` (in `_catalog_pricing.py`). They are used in tests (e.g., `tests/unit/test_platform_tools.py`) but no production call sites are visible in the manifest.
- The overrides cache (`_tool_overrides_cache_obj` in `billing/pricing.py`) is invalidated on `set_tool_pricing_override` and `delete_tool_pricing_override` (evidenced by `await _tool_overrides_cache_obj.invalidate()` in those functions).

## Proposed incremental changes

**Primary fix**: Add cache invalidation to the marketplace tool update/deactivate endpoints.

- In the endpoint that updates a platform tool (likely `PUT /marketplace/platform-tools/{tool_name}` or similar), call `await _invalidate_platform_tool_cache()` after the DB update.
- In the endpoint that updates an org tool (likely `PUT /marketplace/org-tools/{org_slug}/{tool_name}`), call `await _invalidate_all_org_tool_price_cache()` after the DB update.
- If the endpoints are not present in the manifest, the fix should be applied to the service functions that perform the updates (e.g., `update_platform_tool_price`, `deactivate_platform_tool`).

**Secondary (optional)**: Add a test that verifies invalidation is called on price change.

## Dependencies and prioritization

- No new dependencies.
- The fix is independent of other changes and can be shipped immediately.
- Priority: high because it directly affects billing correctness.

## Verification and rollout plan

1. **Unit tests**:
   - Mock the DB and call the update endpoint; assert `_invalidate_platform_tool_cache` is called.
   - Simulate a price change and then call `get_platform_tool_price`; assert the new price is returned (no stale cache).
   - Repeat for org tools.
2. **Integration test**: Use a real DB and Redis, update a price, and verify the next billing call uses the new price.
3. **Rollout**: Deploy the change with the update endpoints. No migration required.
4. **Rollback**: Revert the invalidation calls; TTL will restore eventual consistency.

## Uncertainties and alternatives

- The exact location of the update endpoints is not visible in the manifest; the fix must be applied to whatever code path modifies `marketplace_platform_tools` or `marketplace_org_tools`.
- It is possible that invalidation is already called elsewhere (e.g., in a service layer not shown). The manifest does not include those files, so this is a hypothesis based on the absence of evidence.
- Alternative: Increase the TTL to a larger value and rely on eventual consistency, but this would worsen the stale window.
- Another alternative: Use a Redis pub/sub or versioned cache key to invalidate across processes, but that is a larger change.

## Sources

- `billing/pricing.py` - `resolve_tool_cost` precedence and override cache invalidation.
- `marketplace/_catalog_pricing.py` - TTL caches and invalidation functions.
- `tests/unit/test_platform_tools.py` - Tests for platform tool pricing and cache invalidation.
