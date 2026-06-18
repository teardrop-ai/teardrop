from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.scaffold_model_upgrade import (
    ScaffoldError,
    atomic_usdc_per_1k,
    build_upgrade_plan,
    patch_config_py,
)


def test_atomic_usdc_per_1k_rounds_half_up():
    assert atomic_usdc_per_1k(Decimal("0.75"), Decimal("1.25")) == 938


def test_atomic_usdc_per_1k_rejects_negative_price():
    with pytest.raises(ScaffoldError, match="non-negative"):
        atomic_usdc_per_1k(Decimal("-0.1"), Decimal("1.25"))


def test_patch_config_py():
    original = """
    agent_planner_provider: str = Field(
        default="openrouter",
        description="..."
    )
    agent_planner_model: str = Field(
        default="deepseek-v4-flash",
    )
    agent_model: str = "old"
    """
    patched = patch_config_py(original, "planner", "google", "gemini-3.5-flash")
    assert 'default="google"' in patched
    assert 'default="gemini-3.5-flash"' in patched
    assert 'agent_model: str = "old"' in patched  # Untouched


def test_build_upgrade_plan_scaffolds_sql_and_catalogue(tmp_path):
    repo_root = tmp_path / "repo"
    migrations_dir = repo_root / "migrations" / "versions"
    benchmarks_path = repo_root / "teardrop" / "benchmarks.py"
    migrations_dir.mkdir(parents=True)
    benchmarks_path.parent.mkdir(parents=True)

    (migrations_dir / "061_existing.sql").write_text("-- noop\n", encoding="utf-8")
    benchmarks_path.write_text(
        """MODEL_CATALOGUE: dict[str, dict[str, Any]] = {
    \"google:gemini-3-flash-preview\": {
        \"provider\": \"google\",
        \"model\": \"gemini-3-flash-preview\",
        \"display_name\": \"Gemini 3 Flash (Preview)\",
        \"context_window\": 1_000_000,
        \"supports_tools\": True,
        \"supports_streaming\": True,
        \"quality_tier\": 2,
        \"default_latency_ms\": 350,
        \"knowledge_cutoff\": \"2025-10\",
        \"training_cutoff_note\": \"Training data through October 2025\",
    },
}
""",
        encoding="utf-8",
    )

    plan = build_upgrade_plan(
        repo_root=repo_root,
        provider="google",
        model="gemini-3.5-flash",
        display_name="Gemini 3.5 Flash",
        model_id="google-gemini-3-5-flash-v1",
        provider_input_price_per_1m=Decimal("0.30"),
        provider_output_price_per_1m=Decimal("2.50"),
        margin_multiplier=Decimal("1.25"),
        default_latency_ms=320,
        quality_tier=2,
        context_window=1_000_000,
        knowledge_cutoff="2026-03",
        training_cutoff_note="Training data through March 2026",
        replace_provider="google",
        replace_model="gemini-3-flash-preview",
        replace_model_id="google-gemini-3-flash-preview-v1",
        update_config_role=None,
        supports_tools=True,
        supports_streaming=True,
        run_price_usdc=10_000,
        tool_call_cost=1_000,
    )

    assert plan.migration_filename == "062_gemini_3_5_flash_pricing.sql"
    assert "DELETE FROM pricing_rules WHERE id = 'google-gemini-3-flash-preview-v1';" in plan.migration_sql
    assert "'google-gemini-3-5-flash-v1'" in plan.migration_sql
    assert "375, 3125" in plan.migration_sql
    assert '"google:gemini-3.5-flash"' in plan.benchmarks_content
    assert '"deprecated": True' in plan.benchmarks_content


def test_build_upgrade_plan_rejects_duplicate_catalogue_entry(tmp_path):
    repo_root = tmp_path / "repo"
    migrations_dir = repo_root / "migrations" / "versions"
    benchmarks_path = repo_root / "teardrop" / "benchmarks.py"
    migrations_dir.mkdir(parents=True)
    benchmarks_path.parent.mkdir(parents=True)

    (migrations_dir / "001_baseline.sql").write_text("-- noop\n", encoding="utf-8")
    benchmarks_path.write_text(
        """MODEL_CATALOGUE: dict[str, dict[str, Any]] = {
    \"google:gemini-3.5-flash\": {
        \"provider\": \"google\",
        \"model\": \"gemini-3.5-flash\",
        \"display_name\": \"Gemini 3.5 Flash\",
        \"context_window\": 1_000_000,
        \"supports_tools\": True,
        \"supports_streaming\": True,
        \"quality_tier\": 2,
        \"default_latency_ms\": 320,
        \"knowledge_cutoff\": \"2026-03\",
        \"training_cutoff_note\": \"Training data through March 2026\",
    },
}
""",
        encoding="utf-8",
    )

    with pytest.raises(ScaffoldError, match="already contains"):
        build_upgrade_plan(
            repo_root=repo_root,
            provider="google",
            model="gemini-3.5-flash",
            display_name="Gemini 3.5 Flash",
            model_id="google-gemini-3-5-flash-v1",
            provider_input_price_per_1m=Decimal("0.30"),
            provider_output_price_per_1m=Decimal("2.50"),
            margin_multiplier=Decimal("1.25"),
            default_latency_ms=320,
            quality_tier=2,
            context_window=1_000_000,
            knowledge_cutoff="2026-03",
            training_cutoff_note="Training data through March 2026",
            replace_provider=None,
            replace_model=None,
            replace_model_id=None,
            update_config_role=None,
            supports_tools=True,
            supports_streaming=True,
            run_price_usdc=10_000,
            tool_call_cost=1_000,
        )
