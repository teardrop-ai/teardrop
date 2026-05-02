# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Multicall3 helper — batch multiple eth_calls into a single RPC request.

Multicall3 is deployed at 0xcA11bde05977b3631167028862bE2a173976CA11 on 250+
chains including Ethereum mainnet (chain 1) and Base (chain 8453).

Usage::

    results = await multicall3_batch(w3, [
        (token_addr, allowance_calldata),
        (token_addr2, allowance_calldata2),
    ])
    # results: list[(success: bool, return_data: bytes)] — same order as input

The entire batch consumes a **single** RPC semaphore permit per chunk,
regardless of how many calls are bundled. This is the primary mechanism for
eliminating per-call RPC 429s caused by N×M fan-outs in wallet analysis tools.

For very large call lists, calls are automatically chunked at BATCH_CHUNK_SIZE
(default 300) to stay within provider calldata limits (~20 KB per batch for
typical allowance() payloads).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import AsyncWeb3

from tools.definitions._rpc_semaphore import acquire_rpc_semaphore
from tools.definitions._web3_helpers import rpc_call

logger = logging.getLogger(__name__)

# Multicall3 canonical address — same on Ethereum, Base, and 250+ other chains.
# Source: https://github.com/mds1/multicall3
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Function selector for aggregate3((address,bool,bytes)[]) = keccak256[0:4].
# Verified against https://github.com/mds1/multicall3/blob/main/src/Multicall3.sol
_AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")

# Maximum calls per Multicall3 invocation.
# Allowance payloads are ~68 bytes each → 300 × 68 ≈ 20 KB of calldata, well
# within the ~128 KB limit of most RPC providers' eth_call input size.
BATCH_CHUNK_SIZE = 300


def _encode_aggregate3(calls: list[tuple[str, bytes]], allow_failure: bool) -> bytes:
    """Return the ABI-encoded calldata for aggregate3((address,bool,bytes)[])."""
    structs = [(addr, allow_failure, data) for addr, data in calls]
    return _AGGREGATE3_SELECTOR + abi_encode(["(address,bool,bytes)[]"], [structs])


def _decode_aggregate3(raw: bytes) -> list[tuple[bool, bytes]]:
    """Decode the bytes returned by aggregate3 into (success, returnData) pairs."""
    (results,) = abi_decode(["(bool,bytes)[]"], raw)
    return list(results)


async def multicall3_batch(
    w3: AsyncWeb3,
    calls: Sequence[tuple[str, bytes]],
    *,
    allow_failure: bool = True,
) -> list[tuple[bool, bytes]]:
    """Execute *calls* via Multicall3, consuming one RPC semaphore permit per chunk.

    Args:
        w3: AsyncWeb3 instance connected to the target chain.
        calls: Sequence of ``(target_address, abi_encoded_calldata)`` pairs.
        allow_failure: When True (default), a reverting sub-call returns
            ``(False, b"")`` instead of reverting the whole batch. Always
            leave this True for read-only fan-outs where partial results
            are acceptable (e.g., some tokens not implementing ERC-20).

    Returns:
        List of ``(success: bool, return_data: bytes)`` tuples in the same
        order as *calls*. Length always equals ``len(calls)``.
    """
    if not calls:
        return []

    output: list[tuple[bool, bytes]] = []
    call_list = list(calls)

    for chunk_start in range(0, len(call_list), BATCH_CHUNK_SIZE):
        chunk = call_list[chunk_start : chunk_start + BATCH_CHUNK_SIZE]
        calldata = _encode_aggregate3(chunk, allow_failure)
        async with acquire_rpc_semaphore():
            try:
                raw: bytes = await rpc_call(
                    lambda: w3.eth.call(
                        {
                            "to": MULTICALL3_ADDRESS,
                            "data": calldata,
                        }
                    )
                )
                output.extend(_decode_aggregate3(raw))
            except Exception as exc:
                logger.warning(
                    "Multicall3 batch (calls %d–%d of %d) failed: %s; returning all-failed results for this chunk",
                    chunk_start,
                    chunk_start + len(chunk) - 1,
                    len(call_list),
                    exc,
                )
                # Graceful degradation — treat entire failed chunk as reverted.
                output.extend([(False, b"")] * len(chunk))

    return output
