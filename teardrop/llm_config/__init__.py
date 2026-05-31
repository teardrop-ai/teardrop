# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-org LLM configuration — CRUD, caching, encryption, and routing resolution.

Allows organisations to configure their preferred LLM provider/model, bring
their own API keys (BYOK), set routing preferences, and optionally point at
OpenAI-compatible self-hosted endpoints (vLLM, Ollama, OpenRouter, etc.).

This package preserves the historical flat ``teardrop.llm_config`` import
surface via re-exports.  Implementation is split across:

- :mod:`teardrop.llm_config.base` — models, encryption, pool, cache, CRUD.
- :mod:`teardrop.llm_config.routing` — smart cost/speed/quality routing.
"""

from __future__ import annotations

from teardrop.llm_config.base import (  # noqa: F401  (re-exported for backward compatibility)
    ALLOWED_ROUTING_PREFERENCES,
    OrgLlmConfig,
    _config_cache,
    _config_lock,
    _decrypt_llm_key,
    _encrypt_llm_key,
    _get_cache_ttl,
    _get_llm_fernet,
    _get_pool,
    _llm_fernet,
    _pool,
    _resolve_shared_key,
    _row_to_config,
    build_llm_config_dict,
    close_llm_config_db,
    delete_org_llm_config,
    get_org_llm_config,
    get_org_llm_config_cached,
    get_redis,
    get_settings,
    init_llm_config_db,
    invalidate_llm_config_cache,
    logger,
    reset_llm_fernet,
    upsert_org_llm_config,
)
from teardrop.llm_config.routing import (  # noqa: F401  (re-exported for backward compatibility)
    _COOLDOWN_SECONDS,
    _QUALITY_TIERS,
    _provider_cooldowns,
    _route_from_pool,
    _select_cheapest,
    _select_fastest,
    _select_highest_quality,
    is_provider_cooled_down,
    record_provider_failure,
    resolve_llm_config,
)
