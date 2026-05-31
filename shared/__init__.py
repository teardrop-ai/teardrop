# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Cross-cutting shared utilities.

Infrastructure helpers reused across the Teardrop service that are not specific
to any one domain: the database connection pool registry (``db_pool``), audit
logging (``audit``), email delivery (``email``), Sentry observability and secret
scrubbing (``observability``), pagination helpers (``pagination``), and webhook
signing/verification (``webhook``).

Import submodules directly (e.g. ``from shared.db_pool import get_pool``); this
package is a namespace and intentionally does not re-export symbols.
"""
