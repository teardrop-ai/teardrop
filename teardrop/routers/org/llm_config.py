# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org LLM configuration (BYOK) and model benchmark routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from teardrop.benchmarks import build_benchmarks_response
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.llm_config import (
    ALLOWED_ROUTING_PREFERENCES,
    OrgLlmConfig,
    delete_org_llm_config,
    get_org_llm_config,
    upsert_org_llm_config,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


# ─── LLM Config endpoints ────────────────────────────────────────────────────


class UpsertLlmConfigRequest(BaseModel):
    provider: str = Field(..., description="LLM provider: anthropic, openai, or google")
    model: str = Field(..., min_length=1, max_length=200, description="Model identifier")
    api_key: str | None = Field(
        default=None,
        description="Provider API key (BYOK). Omit to use Teardrop shared key.",
    )
    api_base: str | None = Field(
        default=None,
        description="Custom base URL for OpenAI-compatible endpoints (vLLM, Ollama, etc.)",
    )
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    routing_preference: str = Field(default="default", description="default, cost, speed, or quality")


def _llm_config_to_response(cfg: OrgLlmConfig) -> dict:
    return {
        "org_id": cfg.org_id,
        "provider": cfg.provider,
        "model": cfg.model,
        "has_api_key": cfg.has_api_key,
        "api_base": cfg.api_base,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "timeout_seconds": cfg.timeout_seconds,
        "routing_preference": cfg.routing_preference,
        "is_byok": cfg.is_byok,
        "created_at": cfg.created_at.isoformat(),
        "updated_at": cfg.updated_at.isoformat(),
    }


@router.get("/llm-config", tags=["LLM Config"])
async def get_llm_config_endpoint(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get the authenticated org's LLM configuration."""
    org_id = _require_org_id(payload)
    cfg = await get_org_llm_config(org_id)
    if cfg is None:
        return JSONResponse(
            content={
                "configured": False,
                "provider": settings.agent_provider,
                "model": settings.agent_model,
            }
        )
    return JSONResponse(content={"configured": True, **_llm_config_to_response(cfg)})


@router.put("/llm-config", tags=["LLM Config"])
async def upsert_llm_config_endpoint(
    body: UpsertLlmConfigRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Create or update the authenticated org's LLM configuration."""
    from agent.llm import ALLOWED_PROVIDERS
    from tools.definitions.http_fetch import validate_url

    org_id = _require_org_id(payload)

    if body.provider.lower() not in ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid provider '{body.provider}'. Allowed: {', '.join(sorted(ALLOWED_PROVIDERS))}",
        )

    if body.routing_preference not in ALLOWED_ROUTING_PREFERENCES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid routing_preference. Allowed: {', '.join(sorted(ALLOWED_ROUTING_PREFERENCES))}",
        )

    # Provider-specific temperature limits
    _provider_temp_limits: dict[str, float] = {"anthropic": 1.0, "openai": 2.0, "google": 2.0, "openrouter": 2.0}
    temp_limit = _provider_temp_limits.get(body.provider.lower(), 2.0)
    if body.temperature > temp_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Provider '{body.provider}' requires temperature ≤ {temp_limit}",
        )

    # Non-BYOK model name validation against known catalogue
    if body.api_key is None:
        from teardrop.benchmarks import MODEL_CATALOGUE

        catalogue_key = f"{body.provider.lower()}:{body.model}"
        if catalogue_key not in MODEL_CATALOGUE:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unknown model '{body.model}' for provider '{body.provider}'. "
                    "Supply an api_key (BYOK) to use custom/fine-tuned models."
                ),
            )

    # SSRF validation for api_base
    if body.api_base:
        # A custom api_base with no BYOK key would forward the platform's shared
        # provider key to an arbitrary org-controlled endpoint (key exfiltration).
        # Require BYOK: a key set in this request, or an existing stored key that
        # this request preserves (api_key omitted, not explicitly cleared).
        api_key_omitted = "api_key" not in body.model_fields_set
        will_have_byok = body.api_key is not None
        if not will_have_byok and api_key_omitted:
            existing = await get_org_llm_config(org_id)
            will_have_byok = existing is not None and existing.has_api_key
        if not will_have_byok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="api_base requires api_key (BYOK). Custom base URLs are not supported with the shared platform key.",
            )

        if body.provider.lower() not in {"openai", "openrouter"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="api_base is only supported for OpenAI-compatible providers ('openai', 'openrouter').",
            )
        # Always validate URL structure and scheme
        ssrf_err = validate_url(body.api_base)
        if ssrf_err:
            # When private endpoints are allowed, only reject non-http(s) schemes
            # and DNS failures — allow private IPs
            if not settings.allow_private_llm_endpoints:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unsafe api_base URL: {ssrf_err}",
                )
            elif "Blocked scheme" in ssrf_err or "DNS resolution failed" in ssrf_err or "No hostname" in ssrf_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid api_base URL: {ssrf_err}",
                )

    cfg = await upsert_org_llm_config(
        org_id,
        provider=body.provider.lower(),
        model=body.model,
        api_key=body.api_key,
        clear_api_key="api_key" in body.model_fields_set and body.api_key is None,
        api_base=body.api_base,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        timeout_seconds=body.timeout_seconds,
        routing_preference=body.routing_preference,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_llm_config_to_response(cfg),
    )


@router.delete("/llm-config", tags=["LLM Config"])
async def delete_llm_config_endpoint(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Delete the authenticated org's LLM config (revert to global default)."""
    org_id = _require_org_id(payload)
    deleted = await delete_org_llm_config(org_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No LLM config found.")
    return JSONResponse(content={"status": "deleted"})


# ─── Model benchmarks endpoints ──────────────────────────────────────────────


@router.get("/models/benchmarks", tags=["Models"])
async def get_models_benchmarks() -> JSONResponse:
    """Public: model catalogue with live operational benchmarks."""
    try:
        data = await build_benchmarks_response()
    except Exception:
        logger.warning("Benchmark query failed", exc_info=True)
        data = {"models": [], "updated_at": None}
    return JSONResponse(content=data)


@router.get("/models/benchmarks/org", tags=["Models"])
async def get_org_models_benchmarks(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Authenticated: model benchmarks scoped to the caller's org."""
    org_id = _require_org_id(payload)
    try:
        data = await build_benchmarks_response(org_id=org_id)
    except Exception:
        logger.warning("Org benchmark query failed for org_id=%s", org_id, exc_info=True)
        data = {"models": [], "updated_at": None}
    return JSONResponse(content=data)
