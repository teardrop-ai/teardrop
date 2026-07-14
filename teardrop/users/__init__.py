# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""User and organisation data layer (async Postgres via asyncpg).

Provides:
- Org / User Pydantic models
- init_user_db()     — create tables on startup
- create_org()       — register a new organisation
- create_user()      — register a new user within an org
- get_user_by_email()— look up user for authentication
- verify_secret()    — constant-time password verification

This module is a thin backward-compatibility facade. The implementation lives in
focused submodules:

* :mod:`teardrop.users.base`         — pool, schema init, password hashing
* :mod:`teardrop.users.models`       — Pydantic models
* :mod:`teardrop.users.accounts`     — org/user CRUD + registration
* :mod:`teardrop.users.credentials`  — M2M client credentials
* :mod:`teardrop.users.verification` — email verification + org invites
* :mod:`teardrop.users.tokens`       — refresh tokens
"""

from __future__ import annotations

from teardrop.users.accounts import (  # noqa: F401  (re-exported for backward compatibility)
    create_org,
    create_user,
    get_org_by_id,
    get_org_by_name,
    get_org_id_for_user,
    get_user_by_email,
    get_user_by_org_id,
    register_org_and_user,
)
from teardrop.users.base import (  # noqa: F401  (re-exported for backward compatibility)
    _HASH_ITERATIONS,
    _generate_org_slug,
    _get_pool,
    _hash_secret,
    _pool,
    close_user_db,
    init_user_db,
    logger,
    verify_secret,
)
from teardrop.users.credentials import (  # noqa: F401  (re-exported for backward compatibility)
    create_client_credential,
    delete_org_client_credentials,
    get_client_credential_by_id,
    list_org_client_credentials,
)
from teardrop.users.models import (  # noqa: F401  (re-exported for backward compatibility)
    Org,
    OrgClientCredential,
    OrgInvite,
    RefreshTokenRecord,
    User,
)
from teardrop.users.tokens import (  # noqa: F401  (re-exported for backward compatibility)
    cleanup_expired_refresh_tokens,
    create_refresh_token,
    get_refresh_token_successor,
    revoke_refresh_token,
    rotate_refresh_token,
)
from teardrop.users.verification import (  # noqa: F401  (re-exported for backward compatibility)
    _INVITE_TOKEN_TTL_HOURS,
    _VERIFICATION_TOKEN_TTL_SECONDS,
    consume_org_invite,
    consume_verification_token,
    create_org_invite,
    create_verification_token,
    get_org_invite,
    mark_user_verified,
    verify_user_and_enqueue_onboarding_credit,
)
