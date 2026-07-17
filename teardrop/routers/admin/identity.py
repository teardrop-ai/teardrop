# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin identity provisioning: orgs, users, and client credentials.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.routers.admin`` with no logic changes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from teardrop.dependencies import require_admin
from teardrop.users import create_client_credential, create_org, create_user

router = APIRouter()


class CreateOrgRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class CreateOrgResponse(BaseModel):
    id: str
    name: str


class CreateUserRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    secret: str = Field(..., min_length=8, max_length=128)
    org_id: str
    role: str = "user"


class CreateUserResponse(BaseModel):
    id: str
    email: str
    org_id: str
    role: str


@router.post(
    "/admin/orgs", tags=["Admin", "Admin / Identity"], response_model=CreateOrgResponse, status_code=status.HTTP_201_CREATED
)
async def admin_create_org(
    body: CreateOrgRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create a new organisation (admin only)."""
    org = await create_org(body.name)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": org.id, "name": org.name},
    )


@router.post(
    "/admin/users",
    tags=["Admin", "Admin / Identity"],
    response_model=CreateUserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_user(
    body: CreateUserRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create a new user within an org (admin only)."""
    user = await create_user(
        email=body.email,
        secret=body.secret,
        org_id=body.org_id,
        role=body.role,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": user.id, "email": user.email, "org_id": user.org_id, "role": user.role},
    )


class CreateClientCredentialsRequest(BaseModel):
    org_id: str


class CreateClientCredentialsResponse(BaseModel):
    client_id: str
    client_secret: str = Field(..., description="Plaintext secret — shown once, only its hash is persisted.")
    org_id: str
    created_at: str = Field(..., description="ISO 8601 timestamp.")


@router.post(
    "/admin/client-credentials",
    tags=["Admin", "Admin / Identity"],
    response_model=CreateClientCredentialsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_client_credentials(
    body: CreateClientCredentialsRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create org-scoped M2M client credentials (admin only).

    The client_secret is returned exactly once — store it immediately.
    """
    cred, plaintext_secret = await create_client_credential(body.org_id)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "client_id": cred.client_id,
            "client_secret": plaintext_secret,
            "org_id": cred.org_id,
            "created_at": cred.created_at.isoformat(),
        },
    )
