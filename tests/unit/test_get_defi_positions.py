"""Unit tests for tools/definitions/get_defi_positions.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode as abi_encode

from tools.definitions.get_defi_positions import get_defi_positions

_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

# ─── Default multicall3_batch stub ─────────────────────────────────────────
# All tests in this module patch `get_web3` to return a mock w3 whose contract
# functions are pre-configured. After the Multicall3 refactor, _fetch_aave_v3
# calls `multicall3_batch` instead of per-call contract functions for reserve
# data. We auto-patch it here to return the same `reserve_data` tuple that
# `_build_mock_w3` was given, ABI-encoded as if returned by the on-chain call.
# Individual tests that need different behaviour can override it further.

_DEFAULT_RESERVE_DATA = (0, 0, 0, 0, 0, 0, 0, 0, False)


def _encode_reserve_data(rd: tuple) -> bytes:
    return abi_encode(
        ["uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint40", "bool"],
        list(rd),
    )


@pytest.fixture(autouse=True)
def patch_multicall3(monkeypatch):
    """Patch multicall3_batch in get_defi_positions to avoid real RPC calls.

    The stub returns the reserve_data tuple from ``_current_reserve_data`` (a
    module-level variable that individual tests can override via
    ``set_reserve_data()``).
    """
    _current = {"reserve_data": _DEFAULT_RESERVE_DATA}

    async def _stub(w3, calls, *, allow_failure=True):
        encoded = _encode_reserve_data(_current["reserve_data"])
        return [(True, encoded)] * len(calls)

    monkeypatch.setattr("tools.definitions.get_defi_positions.multicall3_batch", _stub)
    # Expose a setter so tests can customise reserve_data.
    monkeypatch._multicall3_current = _current


def set_reserve_data(monkeypatch, rd: tuple):
    """Helper: override the reserve_data returned by the patched multicall3_batch."""
    monkeypatch._multicall3_current["reserve_data"] = rd


def _make_block_awaitable(value: int = 12345):
    """Return a fresh awaitable wrapper for ``w3.eth.block_number`` property access."""

    async def _coro():
        return value

    # Use a property descriptor so each access returns a fresh coroutine.
    class _EthProxy:
        pass

    return _coro()


def _build_mock_w3(
    *,
    account_data: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 2**256 - 1),
    reserve_data: tuple | None = None,
    comet_balance: int = 0,
    comet_borrow: int = 0,
    comet_num_assets: int = 0,
    comet_asset_infos: list[tuple] | None = None,
    comet_user_collateral: tuple[int, int] = (0, 0),
    comet_is_liquidatable: bool = False,
    uniswap_balance: int = 0,
    uniswap_token_ids: list[int] | None = None,
    uniswap_position: tuple | None = None,
    block_number: int = 12345,
    raise_on: set[str] | None = None,
) -> MagicMock:
    """Build a mock AsyncWeb3 instance with configurable per-function behaviour.

    ``raise_on`` is a set of function names that should raise ``Exception`` when called.
    """
    raise_on = raise_on or set()
    mock_w3 = MagicMock()
    mock_contract = MagicMock()

    def _maybe_raise(name: str, result):
        if name in raise_on:
            return AsyncMock(side_effect=Exception(f"simulated {name} failure"))
        return AsyncMock(return_value=result)

    # Aave v3 Pool.getUserAccountData
    mock_contract.functions.getUserAccountData.return_value.call = _maybe_raise("getUserAccountData", account_data)

    # Aave v3 DataProvider.getUserReserveData
    default_reserve = reserve_data or (0, 0, 0, 0, 0, 0, 0, 0, False)
    mock_contract.functions.getUserReserveData.return_value.call = _maybe_raise("getUserReserveData", default_reserve)

    # Compound v3 Comet.*
    mock_contract.functions.balanceOf.return_value.call = _maybe_raise(
        "balanceOf", comet_balance if "uniswap_balanceOf" not in raise_on else uniswap_balance
    )
    # balanceOf is overloaded between Comet (supply) and Uniswap NFPM (NFT count).
    # Our mock returns whichever value is set; tests should isolate protocol paths.
    mock_contract.functions.borrowBalanceOf.return_value.call = _maybe_raise("borrowBalanceOf", comet_borrow)
    mock_contract.functions.numAssets.return_value.call = _maybe_raise("numAssets", comet_num_assets)
    if comet_asset_infos is None:
        comet_asset_infos = []

    def _asset_info_call(idx):
        # functions.getAssetInfo(idx) returns a Mock whose .call is awaitable
        m = MagicMock()
        if idx < len(comet_asset_infos):
            m.call = AsyncMock(return_value=comet_asset_infos[idx])
        else:
            m.call = AsyncMock(side_effect=Exception("out of range"))
        return m

    mock_contract.functions.getAssetInfo.side_effect = _asset_info_call

    mock_contract.functions.userCollateral.return_value.call = _maybe_raise("userCollateral", comet_user_collateral)
    mock_contract.functions.isLiquidatable.return_value.call = _maybe_raise("isLiquidatable", comet_is_liquidatable)

    # Uniswap v3 NFPM.tokenOfOwnerByIndex / positions
    uniswap_token_ids = uniswap_token_ids or []

    def _token_of_owner_call(_owner, idx):
        m = MagicMock()
        if idx < len(uniswap_token_ids):
            m.call = AsyncMock(return_value=uniswap_token_ids[idx])
        else:
            m.call = AsyncMock(side_effect=Exception("out of range"))
        return m

    mock_contract.functions.tokenOfOwnerByIndex.side_effect = _token_of_owner_call

    default_position = uniswap_position or (
        0,
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    mock_contract.functions.positions.return_value.call = _maybe_raise("positions", default_position)

    mock_w3.eth.contract.return_value = mock_contract

    # block_number is an async property on AsyncWeb3 — awaitable on access
    async def _bn():
        if "block_number" in raise_on:
            raise Exception("block_number failure")
        return block_number

    mock_w3.eth.block_number = _bn()

    return mock_w3


# ─── Input validation & chain_id ─────────────────────────────────────────────


class TestChainValidation:
    async def test_unsupported_chain_raises(self, test_settings):
        from tools.definitions.get_defi_positions import get_defi_positions

        with pytest.raises(ValueError, match="chain_id"):
            await get_defi_positions(wallet_address=_WALLET, chain_id=137)


# ─── Aave v3 ─────────────────────────────────────────────────────────────────


class TestAaveV3:
    async def test_no_debt_health_factor_normalized(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        mock_w3 = _build_mock_w3(
            # 10 ETH collateral @ $2000 = $20000 → 20000 * 1e8 base units
            account_data=(20000 * 10**8, 0, 15000 * 10**8, 8500, 8000, 2**256 - 1),
        )
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        aave = result["aave_v3"]
        assert aave is not None
        assert aave["total_collateral_usd"] == 20000.0
        assert aave["total_debt_usd"] == 0.0
        assert aave["health_factor"] is None
        assert aave["health_factor_status"] == "verified_no_debt"
        assert aave["ltv_bps"] == 8000
        assert aave["liquidation_threshold_bps"] == 8500

    async def test_healthy_position(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        # collateral=$10000, debt=$2000, HF=2.5 * 1e18
        mock_w3 = _build_mock_w3(
            account_data=(10000 * 10**8, 2000 * 10**8, 5000 * 10**8, 8500, 8000, int(2.5 * 10**18)),
        )
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        aave = result["aave_v3"]
        assert aave["health_factor"] == pytest.approx(2.5, rel=1e-3)
        assert aave["health_factor_status"] == "healthy"

    async def test_at_risk_position(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        # HF = 0.95 * 1e18 → at_risk
        mock_w3 = _build_mock_w3(
            account_data=(10000 * 10**8, 9500 * 10**8, 0, 8500, 8000, int(0.95 * 10**18)),
        )
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        assert result["aave_v3"]["health_factor_status"] == "at_risk"

    async def test_reserve_breakdown_included_when_nonzero(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        # Every tracked reserve returns 1.0 unit supplied (decimals-scaled), no debt
        reserve = (
            10**18,  # currentATokenBalance = 1 token (18dec)
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            True,
        )
        set_reserve_data(monkeypatch, reserve)
        mock_w3 = _build_mock_w3(
            account_data=(1 * 10**8, 0, 0, 8500, 8000, 2**256 - 1),
        )
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        reserves = result["aave_v3"]["reserves"]
        # Should have entries for every tracked reserve since all returned nonzero
        assert len(reserves) > 0
        for r in reserves:
            assert r["usage_as_collateral"] is True


# ─── Compound v3 ─────────────────────────────────────────────────────────────


class TestCompoundV3:
    async def test_empty_wallet_skips_market(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        mock_w3 = _build_mock_w3(
            comet_balance=0,
            comet_borrow=0,
            comet_num_assets=0,
        )
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        assert result["compound_v3"] == []

    async def test_market_with_supply_returned(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        # 5000 USDC supplied (6 decimals), no borrow, no collaterals
        mock_w3 = _build_mock_w3(
            comet_balance=5000 * 10**6,
            comet_borrow=0,
            comet_num_assets=0,
            comet_is_liquidatable=False,
        )
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        # All 3 Ethereum markets return the same raw balance (shared mock), but
        # base_decimals differ (USDC/USDT=6, WETH=18) so formatted values differ.
        # At least the 6-decimal markets should show the full 5000 supply.
        markets = result["compound_v3"]
        assert len(markets) == 3
        usdc_markets = [m for m in markets if m["base_asset_symbol"] in ("USDC", "USDT")]
        assert len(usdc_markets) == 2
        for m in usdc_markets:
            assert float(m["supplied_amount"]) == 5000.0
            assert m["is_liquidatable"] is False

    async def test_market_with_collateral(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        # numAssets=1 → getAssetInfo(0) returns struct; userCollateral returns (1e18, 0)
        weth = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        asset_info = (0, weth, "0x0000000000000000000000000000000000000000", 0, 0, 0, 0, 0)
        mock_w3 = _build_mock_w3(
            comet_balance=0,
            comet_borrow=1000 * 10**6,  # borrowed 1000 USDC
            comet_num_assets=1,
            comet_asset_infos=[asset_info],
            comet_user_collateral=(10**18, 0),  # 1 WETH
            comet_is_liquidatable=False,
        )
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        markets = result["compound_v3"]
        assert len(markets) == 3
        # Only USDC/USDT (6-dec) markets will show the full 1000 borrow;
        # WETH market sees same raw int / 1e18 → effectively 0. Verify collateral
        # and liquidation status are tracked on every market regardless.
        for m in markets:
            assert len(m["collateral"]) == 1
            assert m["collateral"][0]["amount"] == str(10**18)
        usdc_like = [m for m in markets if m["base_asset_symbol"] in ("USDC", "USDT")]
        assert len(usdc_like) == 2
        for m in usdc_like:
            assert float(m["borrowed_amount"]) == 1000.0


# ─── Uniswap v3 ──────────────────────────────────────────────────────────────


class TestUniswapV3:
    async def test_no_positions(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        mock_w3 = _build_mock_w3(uniswap_balance=0)
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)
        assert result["uniswap_v3"] == []

    async def test_closed_position_filtered(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        # liquidity=0 and tokensOwed=0 → closed
        closed_position = (
            0,
            "0x0000000000000000000000000000000000000000",
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            3000,
            -887220,
            887220,
            0,  # liquidity
            0,
            0,
            0,
            0,  # tokensOwed0, tokensOwed1
        )
        mock_w3 = _build_mock_w3(
            comet_balance=0,  # shared balanceOf mock — Uniswap balanceOf will also read this path
            uniswap_balance=1,
            uniswap_token_ids=[12345],
            uniswap_position=closed_position,
        )
        # Override: balanceOf is shared across Comet + Uniswap in our mock.
        # Force Uniswap path by setting balanceOf = 1.
        mock_w3.eth.contract.return_value.functions.balanceOf.return_value.call = AsyncMock(return_value=1)
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)
        assert result["uniswap_v3"] == []

    async def test_active_position_returned(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        active_position = (
            0,
            "0x0000000000000000000000000000000000000000",
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            3000,
            -887220,
            887220,
            10**15,  # liquidity
            0,
            0,
            100,
            200,  # tokensOwed0, tokensOwed1
        )
        mock_w3 = _build_mock_w3(
            uniswap_balance=1,
            uniswap_token_ids=[42],
            uniswap_position=active_position,
        )
        mock_w3.eth.contract.return_value.functions.balanceOf.return_value.call = AsyncMock(return_value=1)
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)
        positions = result["uniswap_v3"]
        assert len(positions) == 1
        p = positions[0]
        assert p["token_id"] == "42"
        assert p["fee_tier_raw"] == 3000
        assert p["tick_lower"] == -887220
        assert p["tick_upper"] == 887220
        assert p["liquidity"] == str(10**15)
        assert p["tokens_owed_0"] == "100"
        assert p["tokens_owed_1"] == "200"
        assert p["status"] == "active"

    async def test_known_token_symbol_resolved(self, test_settings, monkeypatch):
        """token0/token1 addresses that are in _KNOWN_SYMBOLS should have symbols set."""
        from tools.definitions.get_defi_positions import get_defi_positions

        # WETH (chain 1) and USDC (chain 1) are both in _AAVE_V3_TRACKED_RESERVES
        weth = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        usdc = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        active_position = (
            0,
            "0x0000000000000000000000000000000000000000",
            weth,
            usdc,
            3000,
            -887220,
            887220,
            10**15,  # liquidity
            0,
            0,
            0,
            0,
        )
        mock_w3 = _build_mock_w3(
            uniswap_balance=1,
            uniswap_token_ids=[99],
            uniswap_position=active_position,
        )
        mock_w3.eth.contract.return_value.functions.balanceOf.return_value.call = AsyncMock(return_value=1)
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)
        p = result["uniswap_v3"][0]
        assert p["token0_symbol"] == "WETH"
        assert p["token1_symbol"] == "USDC"

    async def test_unknown_token_symbol_falls_back_to_address(self, test_settings, monkeypatch):
        """Addresses not in _KNOWN_SYMBOLS should fall back to raw address strings."""
        from tools.definitions.get_defi_positions import get_defi_positions

        unlisted = "0x1111111111111111111111111111111111111111"
        active_position = (
            0,
            "0x0000000000000000000000000000000000000000",
            unlisted,
            unlisted,
            500,
            -10,
            10,
            10**12,
            0,
            0,
            0,
            0,
        )
        mock_w3 = _build_mock_w3(
            uniswap_balance=1,
            uniswap_token_ids=[77],
            uniswap_position=active_position,
        )
        mock_w3.eth.contract.return_value.functions.balanceOf.return_value.call = AsyncMock(return_value=1)
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)
        p = result["uniswap_v3"][0]
        assert p["token0_symbol"] == unlisted
        assert p["token1_symbol"] == unlisted


# ─── Partial-success / error isolation ───────────────────────────────────────


class TestPartialSuccess:
    async def test_aave_failure_isolated(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        mock_w3 = _build_mock_w3(raise_on={"getUserAccountData"})
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        assert result["aave_v3"] is None
        # Compound and Uniswap still returned empty lists (not failures)
        assert result["compound_v3"] == []
        assert result["uniswap_v3"] == []
        # Error captured
        error_protocols = {e["protocol"] for e in result["errors"]}
        assert "aave_v3" in error_protocols

    async def test_tool_never_raises_on_rpc_error(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        # All three protocols fail
        mock_w3 = _build_mock_w3(raise_on={"getUserAccountData", "balanceOf", "numAssets", "positions"})
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        # Must not raise
        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        assert result["aave_v3"] is None
        assert result["compound_v3"] == []
        assert result["uniswap_v3"] == []
        assert len(result["errors"]) >= 1


# ─── Output schema ───────────────────────────────────────────────────────────


class TestOutputSchema:
    async def test_includes_block_number_and_note(self, test_settings, monkeypatch):
        from tools.definitions.get_defi_positions import get_defi_positions

        mock_w3 = _build_mock_w3(block_number=99999)
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        assert result["data_block_number"] == 99999
        assert "snapshot" in result["note"].lower()
        assert result["wallet_address"] == _WALLET
        assert result["chain_id"] == 1

    async def test_tool_registered(self):
        from tools.definitions.get_defi_positions import TOOL

        assert TOOL.name == "get_defi_positions"
        assert TOOL.version == "1.0.0"
        assert "aave" in TOOL.tags
        assert "compound" in TOOL.tags
        assert "uniswap" in TOOL.tags

    async def test_get_defi_positions_timeout_handling(self, monkeypatch):
        """Test that get_defi_positions handles slow protocol branches with 45s timeouts."""
        # protocol_task timeouts are 45s in code.
        # We'll mock one task to be slow and verify it returns a ProtocolErrorInfo instead of crashing.

        async def slow_fetch(*args, **kwargs):
            await asyncio.sleep(0.5)  # Simulated delay
            return MagicMock()

        mock_w3 = _build_mock_w3()
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        # Patch asyncio.wait_for to trigger TimeoutError quickly for Aave
        original_wait_for = asyncio.wait_for

        async def mock_wait_for(fut, timeout):
            if timeout == 45:  # This identifies our protocol-level timeouts
                # For this test, Force Aave to time out
                # We can't easily distinguish which task is which here without more complex mocking,
                # but we can mock the specific fetcher function instead.
                raise asyncio.TimeoutError()
            return await original_wait_for(fut, timeout)

        with patch("asyncio.wait_for", side_effect=mock_wait_for):
            result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        # Should have errors for all three protocols because our mock_wait_for timed out all 45s tasks
        assert len(result["errors"]) >= 1
        protocols_with_errors = [e["protocol"] for e in result["errors"]]
        assert "aave_v3" in protocols_with_errors
        assert "compound_v3" in protocols_with_errors
        # The protocol key in get_defi_positions.py for Uniswap task timeout is "uniswap_v3"
        assert "uniswap_v3" in protocols_with_errors
        assert "timeout" in result["errors"][0]["error"].lower()

    async def test_get_defi_positions_rpc_timeout_handling(self, monkeypatch):
        """Test that get_defi_positions handles individual RPC call timeouts."""
        mock_w3 = _build_mock_w3()
        monkeypatch.setattr("tools.definitions.get_defi_positions.get_web3", lambda chain_id=1: mock_w3)

        # Mock rpc_call to raise TimeoutError
        with patch("tools.definitions.get_defi_positions.rpc_call", side_effect=asyncio.TimeoutError()):
            result = await get_defi_positions(wallet_address=_WALLET, chain_id=1)

        # Individual RPC timeouts should bubble up to protocol error handling
        assert len(result["errors"]) >= 1
        assert any("timeout" in e["error"].lower() for e in result["errors"])
