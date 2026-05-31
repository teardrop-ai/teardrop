# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""AG-UI streaming helpers for the ``/agent/run`` SSE endpoint.

This module owns the presentation-layer plumbing for the agent stream:

* ``_sse_event`` and the ``_EV_*`` AG-UI event-type constants used to format
  Server-Sent Events for ``sse_starlette``.
* ``_A2UIStreamFilter`` — a stateful, streaming-safe scrubber that strips the
  server-internal ```a2ui ... ``` fenced block out of human-readable
  ``TEXT_MESSAGE_CONTENT`` tokens (the block is re-emitted as a typed
  ``SURFACE_UPDATE`` event by ``ui_generator_node``).
* ``_should_flush_planner_buffer`` and ``_recover_planner_suffix`` — planner
  buffering helpers that reconcile streamed deltas with the final planner
  message when a provider drops trailing token chunks.

These helpers contain no billing, auth, or persistence logic; they only shape
the outbound event stream. Callers should import them directly from
``teardrop.agent_stream``.
"""

from __future__ import annotations

import json
from typing import Any

# ─── AG-UI event helpers ──────────────────────────────────────────────────────


def _sse_event(event_type: str, data: dict[str, Any]) -> dict[str, str]:
    """Format a Server-Sent Event dict for sse_starlette."""
    return {"event": event_type, "data": json.dumps(data)}


# AG-UI event type constants (aligned with ag-ui-protocol spec)
_EV_RUN_STARTED = "RUN_STARTED"
_EV_RUN_FINISHED = "RUN_FINISHED"
_EV_TEXT_MSG_START = "TEXT_MESSAGE_START"
_EV_TEXT_MSG_CONTENT = "TEXT_MESSAGE_CONTENT"
_EV_TEXT_MSG_END = "TEXT_MESSAGE_END"
_EV_TOOL_CALL_START = "TOOL_CALL_START"
_EV_TOOL_CALL_END = "TOOL_CALL_END"
_EV_STATE_SNAPSHOT = "STATE_SNAPSHOT"
_EV_SURFACE_UPDATE = "SURFACE_UPDATE"
_EV_USAGE_SUMMARY = "USAGE_SUMMARY"
_EV_BILLING_SETTLEMENT = "BILLING_SETTLEMENT"
_EV_ERROR = "ERROR"
_EV_DONE = "DONE"
_EV_CUSTOM = "Custom"  # ag-ui Custom event for application-defined structured payloads


# ─── A2UI stream filter ──────────────────────────────────────────────────────
# The planner LLM is instructed (see _PLANNER_SYSTEM in agent/nodes.py) to embed
# a fenced ```a2ui ... ``` block in its final assistant message. That block is
# a server-internal signal consumed by ui_generator_node and re-emitted as a
# typed SURFACE_UPDATE event. It must NEVER appear in TEXT_MESSAGE_CONTENT
# tokens, which are presented as human-readable prose to the client.
#
# This filter is a stateful, streaming-safe sentinel scrubber. It buffers a
# small lookahead so that fence sentinels split across token boundaries are
# detected correctly, without holding back the rest of the stream.

_A2UI_OPEN = "```a2ui"
_A2UI_CLOSE = "```"


class _A2UIStreamFilter:
    """Strip ```a2ui ... ``` blocks from a streaming text source.

    Use ``feed(delta)`` for each incoming token chunk; the return value is the
    text safe to forward to the client. Call ``flush()`` once the source is
    exhausted to drain any held-back buffer (any unclosed fence is discarded).
    """

    __slots__ = ("_buf", "_suppressing")

    def __init__(self) -> None:
        self._buf: str = ""
        self._suppressing: bool = False

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        self._buf += delta
        out: list[str] = []
        # Lookahead must be large enough to detect a sentinel that arrives split
        # across multiple chunks. We hold back (len(sentinel) - 1) characters.
        open_hold = len(_A2UI_OPEN) - 1  # 6
        close_hold = len(_A2UI_CLOSE) - 1  # 2
        while True:
            if self._suppressing:
                idx = self._buf.find(_A2UI_CLOSE)
                if idx == -1:
                    # Keep enough tail to catch a split close sentinel.
                    if len(self._buf) > close_hold:
                        self._buf = self._buf[-close_hold:]
                    return "".join(out)
                # Drop everything up to and including the close fence; also
                # consume one trailing newline so the client doesn't see a
                # blank gap where the block used to be.
                tail_start = idx + len(_A2UI_CLOSE)
                if tail_start < len(self._buf) and self._buf[tail_start] == "\n":
                    tail_start += 1
                self._buf = self._buf[tail_start:]
                self._suppressing = False
                continue
            # Not suppressing: scan for the open sentinel.
            idx = self._buf.find(_A2UI_OPEN)
            if idx == -1:
                # Emit everything except the last open_hold chars (which could
                # still be the start of a split open sentinel).
                if len(self._buf) > open_hold:
                    out.append(self._buf[:-open_hold])
                    self._buf = self._buf[-open_hold:]
                return "".join(out)
            # Emit text before the fence (rstrip a trailing newline that
            # immediately precedes the fence, to avoid a dangling blank line).
            prefix = self._buf[:idx]
            if prefix.endswith("\n"):
                prefix = prefix[:-1]
            if prefix:
                out.append(prefix)
            self._buf = self._buf[idx + len(_A2UI_OPEN) :]
            self._suppressing = True
            # Loop again to handle text that follows the fence in the same buffer.

    def flush(self) -> str:
        """Drain the held-back buffer at end-of-stream.

        If we were inside an unclosed a2ui fence (LLM was truncated or stopped
        early), discard the buffer silently — better to lose a partial signal
        than to leak fence characters to the client.
        """
        if self._suppressing:
            self._buf = ""
            return ""
        out = self._buf
        self._buf = ""
        return out


def _should_flush_planner_buffer(task_status: str) -> bool:
    """Return True only for planner statuses that should emit buffered prose."""
    status = task_status.strip().lower()
    return status in {"", "planning", "generating_ui", "completed"}


def _recover_planner_suffix(emitted_chunks: list[tuple[str, str]], planner_text: str) -> str:
    """Return missing planner text suffix not already emitted.

    This reconciles streamed deltas with the final planner message in case the
    provider dropped trailing token chunks.
    """
    if not emitted_chunks or not planner_text:
        return ""

    emitted_text = "".join(delta for _, delta in emitted_chunks)
    if not emitted_text:
        return ""

    fallback_filter = _A2UIStreamFilter()
    expected_text = fallback_filter.feed(planner_text) + fallback_filter.flush()
    if not expected_text or len(expected_text) <= len(emitted_text):
        return ""
    if expected_text.startswith(emitted_text):
        return expected_text[len(emitted_text) :]

    # If token chunk boundaries diverged, recover only the non-overlapping tail
    # to avoid double-emitting already delivered content.
    max_overlap = min(len(emitted_text), len(expected_text))
    for overlap in range(max_overlap, 0, -1):
        if emitted_text.endswith(expected_text[:overlap]):
            return expected_text[overlap:]
    return ""
