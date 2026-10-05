# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Brand metadata for x402 Bazaar ``resource`` objects (specs/extensions/bazaar.md "Service Metadata").

Shared by every paid surface so catalogs that group endpoints by domain (e.g. Agentic.Market)
see one consistent service name, tag set, and icon. Facilitators soft-drop invalid fields, so
a bad value loses the label, never the payment.
"""

from __future__ import annotations

from typing import Any

# Printable ASCII; name <= 32 chars; <= 5 tags of <= 32 chars each.
SERVICE_NAME = "Teardrop"
SERVICE_TAGS = ("crypto", "defi", "onchain data", "wallet analytics", "ai agents")


def service_metadata(settings: Any) -> dict[str, Any]:
    """``serviceName``/``tags``/``iconUrl`` for a 402 ``resource``; the icon only when it is an absolute http(s) URL."""
    metadata: dict[str, Any] = {"serviceName": SERVICE_NAME, "tags": list(SERVICE_TAGS)}
    icon_url = (getattr(settings, "agent_card_icon_url", "") or "").strip()
    if icon_url.startswith(("https://", "http://")):
        metadata["iconUrl"] = icon_url
    return metadata
