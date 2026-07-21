# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Pydantic models for the user/org data layer."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Org(BaseModel):
    id: str
    name: str
    slug: str = ""
    acquisition_source: str = ""
    created_at: datetime


class User(BaseModel):
    id: str
    email: str
    org_id: str
    hashed_secret: str
    salt: str
    role: str  # "admin" | "user"
    is_active: bool
    is_verified: bool = True
    created_at: datetime


class OrgClientCredential(BaseModel):
    client_id: str
    org_id: str
    hashed_secret: str
    salt: str
    created_at: datetime


class OrgInvite(BaseModel):
    token: str
    org_id: str
    email: str | None
    role: str
    invited_by: str
    created_at: datetime
    expires_at: datetime
    used: bool


class RefreshTokenRecord(BaseModel):
    token: str
    user_id: str
    org_id: str
    auth_method: str
    extra_claims: dict
    created_at: datetime
    expires_at: datetime
