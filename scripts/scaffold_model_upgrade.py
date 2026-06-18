# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scaffold model-upgrade artifacts without bypassing audited migrations.

Usage example:
    python scripts/scaffold_model_upgrade.py \
        --provider google \
        --model gemini-3.5-flash \
        --display-name "Gemini 3.5 Flash" \
        --model-id google-gemini-3-5-flash-v1 \
        --provider-input-price-per-1m 0.30 \
        --provider-output-price-per-1m 2.50 \
        --replace-model gemini-3-flash-preview \
        --default-latency-ms 320 \
        --quality-tier 2 \
        --context-window 1000000 \
        --knowledge-cutoff 2026-03 \
        --training-cutoff-note "Training data through March 2026"

By default the script runs in dry-run mode and prints the generated migration SQL.
Pass ``--write`` to create the migration file and patch benchmarks.py.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ATOMIC_USDC_PER_DOLLAR = 1_000_000
DEFAULT_MARGIN_MULTIPLIER = Decimal("1.25")
DEFAULT_RUN_PRICE_USDC = 10_000
DEFAULT_TOOL_CALL_COST_USDC = 1_000


class ScaffoldError(ValueError):
    """Raised when the requested scaffold parameters are invalid."""


@dataclass(frozen=True)
class ModelUpgradePlan:
    migration_version: int
    migration_slug: str
    migration_filename: str
    migration_sql: str
    benchmarks_content: str
    config_content: str | None


def fetch_openrouter_pricing(model: str) -> tuple[Decimal, Decimal]:
    """Fetch prompt and completion pricing per 1M tokens from OpenRouter."""
    url = "https://openrouter.ai/api/v1/models"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "teardrop/scaffold"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ScaffoldError(f"Failed to fetch OpenRouter pricing: {exc}")

    for m in data.get("data", []):
        # Match e.g. "google/gemini-3.5-flash" suffix or exact match
        if m.get("id") == model or m.get("id", "").endswith(f"/{model}"):
            pricing = m.get("pricing", {})
            prompt = Decimal(pricing.get("prompt", "0")) * Decimal(1_000_000)
            completion = Decimal(pricing.get("completion", "0")) * Decimal(1_000_000)
            return prompt, completion

    raise ScaffoldError(
        f"Could not find model '{model}' in OpenRouter API. Please supply --provider-input-price-per-1m manually."
    )


def atomic_usdc_per_1k(price_per_1m_usd: Decimal, margin_multiplier: Decimal) -> int:
    """Convert provider list price per 1M tokens into atomic USDC per 1k tokens."""
    if price_per_1m_usd < 0:
        raise ScaffoldError("Provider list prices must be non-negative.")
    if margin_multiplier <= 0:
        raise ScaffoldError("Margin multiplier must be greater than zero.")

    atomic = (price_per_1m_usd * margin_multiplier * ATOMIC_USDC_PER_DOLLAR) / Decimal(1000)
    rounded = atomic.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(rounded)


def detect_next_migration_version(migrations_dir: Path) -> int:
    versions: list[int] = []
    for path in migrations_dir.glob("*.sql"):
        match = re.match(r"(\d+)_", path.name)
        if match:
            versions.append(int(match.group(1)))
    if not versions:
        raise ScaffoldError(f"No SQL migrations found in {migrations_dir}")
    return max(versions) + 1


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not slug:
        raise ScaffoldError("Unable to derive a migration slug from the provided model/display name.")
    return slug


def sql_quote(value: str) -> str:
    return value.replace("'", "''")


def build_migration_sql(
    *,
    model_id: str,
    display_name: str,
    provider: str,
    model: str,
    tokens_in_cost_per_1k: int,
    tokens_out_cost_per_1k: int,
    run_price_usdc: int,
    tool_call_cost: int,
    replace_model_id: str | None,
    provider_input_price_per_1m: Decimal,
    provider_output_price_per_1m: Decimal,
    margin_multiplier: Decimal,
) -> str:
    comments = [
        f"-- Seed pricing for {display_name}.",
        "-- Domain: billing",
        "-- Invariant: Per-1k-token rates in BIGINT atomic USDC",
        f"-- Provider list price: ${provider_input_price_per_1m}/M input, ${provider_output_price_per_1m}/M output.",
        (
            "-- Teardrop rate "
            f"(+{((margin_multiplier - Decimal('1')) * Decimal('100')).normalize()}% margin): "
            f"{tokens_in_cost_per_1k} input, {tokens_out_cost_per_1k} output atomic USDC per 1k tokens."
        ),
    ]
    body: list[str] = []
    if replace_model_id:
        body.extend(
            [
                f"DELETE FROM pricing_rules WHERE id = '{sql_quote(replace_model_id)}';",
                "",
            ]
        )
    body.extend(
        [
            "INSERT INTO pricing_rules",
            "    (id, name, provider, model, run_price_usdc,",
            "     tokens_in_cost_per_1k, tokens_out_cost_per_1k, tool_call_cost, effective_from)",
            "VALUES",
            f"    ('{sql_quote(model_id)}',",
            f"     '{sql_quote(display_name)}',",
            f"     '{sql_quote(provider)}', '{sql_quote(model)}',",
            f"     {run_price_usdc}, {tokens_in_cost_per_1k}, {tokens_out_cost_per_1k}, {tool_call_cost}, NOW())",
            "",
            "ON CONFLICT (id) DO NOTHING;",
        ]
    )
    return "\n".join(comments + [""] + body) + "\n"


def _find_catalogue_bounds(content: str) -> tuple[int, int]:
    anchor = "MODEL_CATALOGUE: dict[str, dict[str, Any]] = {"
    start = content.find(anchor)
    if start == -1:
        raise ScaffoldError("Unable to locate MODEL_CATALOGUE in benchmarks.py")
    brace_start = content.find("{", start)
    if brace_start == -1:
        raise ScaffoldError("Unable to locate MODEL_CATALOGUE opening brace in benchmarks.py")

    depth = 0
    for index in range(brace_start, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return brace_start, index
    raise ScaffoldError("Unable to locate MODEL_CATALOGUE closing brace in benchmarks.py")


def _render_catalogue_entry(
    *,
    provider: str,
    model: str,
    display_name: str,
    context_window: int,
    supports_tools: bool,
    supports_streaming: bool,
    quality_tier: int,
    default_latency_ms: int,
    knowledge_cutoff: str,
    training_cutoff_note: str,
) -> str:
    key = f"{provider}:{model}"
    lines = [
        f'    "{key}": {{',
        f'        "provider": "{provider}",',
        f'        "model": "{model}",',
        f'        "display_name": "{display_name}",',
        f'        "context_window": {context_window:,},'.replace(",", "_"),
        f'        "supports_tools": {str(supports_tools)},',
        f'        "supports_streaming": {str(supports_streaming)},',
        f'        "quality_tier": {quality_tier},',
        f'        "default_latency_ms": {default_latency_ms},',
        f'        "knowledge_cutoff": "{knowledge_cutoff}",',
        f'        "training_cutoff_note": "{training_cutoff_note}",',
        "    },",
    ]
    return "\n".join(lines)


def _mark_catalogue_entry_deprecated(content: str, provider: str, model: str) -> str:
    entry_key = f'    "{provider}:{model}": {{'
    start = content.find(entry_key)
    if start == -1:
        raise ScaffoldError(f"Unable to locate benchmark catalogue entry for {provider}:{model}")
    end = content.find("    },", start)
    if end == -1:
        raise ScaffoldError(f"Unable to locate benchmark catalogue boundary for {provider}:{model}")
    entry = content[start : end + len("    },")]
    if '        "deprecated": True,' in entry:
        return content
    replacement = entry.replace("    },", '        "deprecated": True,\n    },')
    return content[:start] + replacement + content[end + len("    },") :]


def patch_benchmarks_catalogue(
    content: str,
    *,
    provider: str,
    model: str,
    display_name: str,
    context_window: int,
    supports_tools: bool,
    supports_streaming: bool,
    quality_tier: int,
    default_latency_ms: int,
    knowledge_cutoff: str,
    training_cutoff_note: str,
    replace_provider: str | None,
    replace_model: str | None,
) -> str:
    brace_start, brace_end = _find_catalogue_bounds(content)
    new_entry = _render_catalogue_entry(
        provider=provider,
        model=model,
        display_name=display_name,
        context_window=context_window,
        supports_tools=supports_tools,
        supports_streaming=supports_streaming,
        quality_tier=quality_tier,
        default_latency_ms=default_latency_ms,
        knowledge_cutoff=knowledge_cutoff,
        training_cutoff_note=training_cutoff_note,
    )
    key = f'"{provider}:{model}": {{'
    if key in content[brace_start:brace_end]:
        raise ScaffoldError(f"Benchmark catalogue already contains {provider}:{model}")

    updated = content[:brace_end] + new_entry + "\n" + content[brace_end:]
    if replace_provider and replace_model:
        updated = _mark_catalogue_entry_deprecated(updated, replace_provider, replace_model)
    return updated


def patch_config_py(content: str, role: str, provider: str, model: str) -> str:
    if role == "primary":
        provider_var, model_var = "agent_provider", "agent_model"
    elif role == "planner":
        provider_var, model_var = "agent_planner_provider", "agent_planner_model"
    elif role == "synthesis":
        provider_var, model_var = "agent_synthesis_provider", "agent_synthesis_model"
    else:
        raise ScaffoldError(f"Unknown config role: {role}")

    def replacer(var_name: str, new_val: str, text: str) -> str:
        # Match `var_name: str = "val"` or `var_name: str = Field(..., default="val"`
        pattern = rf'({var_name}\s*:\s*str\s*=(?:[^=]*?\bdefault\s*=\s*)?)["\'][^"\']*["\']'
        replaced = re.sub(pattern, rf'\g<1>"{new_val}"', text, count=1)
        if replaced == text:
            raise ScaffoldError(f"Failed to patch {var_name} in teardrop/config.py")
        return replaced

    content = replacer(provider_var, provider, content)
    content = replacer(model_var, model, content)
    return content


def build_upgrade_plan(
    *,
    repo_root: Path,
    provider: str,
    model: str,
    display_name: str,
    model_id: str,
    provider_input_price_per_1m: Decimal | None,
    provider_output_price_per_1m: Decimal | None,
    margin_multiplier: Decimal,
    default_latency_ms: int,
    quality_tier: int,
    context_window: int,
    knowledge_cutoff: str,
    training_cutoff_note: str,
    replace_provider: str | None,
    replace_model: str | None,
    replace_model_id: str | None,
    update_config_role: str | None,
    supports_tools: bool,
    supports_streaming: bool,
    run_price_usdc: int,
    tool_call_cost: int,
) -> ModelUpgradePlan:
    migrations_dir = repo_root / "migrations" / "versions"
    benchmarks_path = repo_root / "teardrop" / "benchmarks.py"
    config_path = repo_root / "teardrop" / "config.py"

    migration_version = detect_next_migration_version(migrations_dir)
    migration_slug = slugify(display_name)
    migration_filename = f"{migration_version:03d}_{migration_slug}_pricing.sql"

    if provider_input_price_per_1m is None or provider_output_price_per_1m is None:
        provider_input_price_per_1m, provider_output_price_per_1m = fetch_openrouter_pricing(model)

    tokens_in_cost_per_1k = atomic_usdc_per_1k(provider_input_price_per_1m, margin_multiplier)
    tokens_out_cost_per_1k = atomic_usdc_per_1k(provider_output_price_per_1m, margin_multiplier)
    migration_sql = build_migration_sql(
        model_id=model_id,
        display_name=display_name,
        provider=provider,
        model=model,
        tokens_in_cost_per_1k=tokens_in_cost_per_1k,
        tokens_out_cost_per_1k=tokens_out_cost_per_1k,
        run_price_usdc=run_price_usdc,
        tool_call_cost=tool_call_cost,
        replace_model_id=replace_model_id,
        provider_input_price_per_1m=provider_input_price_per_1m,
        provider_output_price_per_1m=provider_output_price_per_1m,
        margin_multiplier=margin_multiplier,
    )

    benchmarks_content = patch_benchmarks_catalogue(
        benchmarks_path.read_text(encoding="utf-8"),
        provider=provider,
        model=model,
        display_name=display_name,
        context_window=context_window,
        supports_tools=supports_tools,
        supports_streaming=supports_streaming,
        quality_tier=quality_tier,
        default_latency_ms=default_latency_ms,
        knowledge_cutoff=knowledge_cutoff,
        training_cutoff_note=training_cutoff_note,
        replace_provider=replace_provider,
        replace_model=replace_model,
    )

    config_content = None
    if update_config_role:
        config_content = patch_config_py(
            config_path.read_text(encoding="utf-8"),
            role=update_config_role,
            provider=provider,
            model=model,
        )

    return ModelUpgradePlan(
        migration_version=migration_version,
        migration_slug=migration_slug,
        migration_filename=migration_filename,
        migration_sql=migration_sql,
        benchmarks_content=benchmarks_content,
        config_content=config_content,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--provider-input-price-per-1m", type=Decimal, help="List input price per 1M tokens (USD)")
    parser.add_argument("--provider-output-price-per-1m", type=Decimal, help="List output price per 1M tokens (USD)")
    parser.add_argument(
        "--update-config-role",
        choices=["primary", "planner", "synthesis"],
        help="Automatically update teardrop/config.py defaults for this role.",
    )
    parser.add_argument("--margin-multiplier", type=Decimal, default=DEFAULT_MARGIN_MULTIPLIER)
    parser.add_argument("--run-price-usdc", type=int, default=DEFAULT_RUN_PRICE_USDC)
    parser.add_argument("--tool-call-cost-usdc", type=int, default=DEFAULT_TOOL_CALL_COST_USDC)
    parser.add_argument("--default-latency-ms", type=int, required=True)
    parser.add_argument("--quality-tier", type=int, required=True)
    parser.add_argument("--context-window", type=int, required=True)
    parser.add_argument("--knowledge-cutoff", required=True)
    parser.add_argument("--training-cutoff-note", required=True)
    parser.add_argument("--replace-provider")
    parser.add_argument("--replace-model")
    parser.add_argument("--replace-model-id")
    parser.add_argument("--supports-tools", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--supports-streaming", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    try:
        plan = build_upgrade_plan(
            repo_root=repo_root,
            provider=args.provider,
            model=args.model,
            display_name=args.display_name,
            model_id=args.model_id,
            provider_input_price_per_1m=args.provider_input_price_per_1m,
            provider_output_price_per_1m=args.provider_output_price_per_1m,
            margin_multiplier=args.margin_multiplier,
            default_latency_ms=args.default_latency_ms,
            quality_tier=args.quality_tier,
            context_window=args.context_window,
            knowledge_cutoff=args.knowledge_cutoff,
            training_cutoff_note=args.training_cutoff_note,
            replace_provider=args.replace_provider,
            replace_model=args.replace_model,
            replace_model_id=args.replace_model_id,
            update_config_role=args.update_config_role,
            supports_tools=args.supports_tools,
            supports_streaming=args.supports_streaming,
            run_price_usdc=args.run_price_usdc,
            tool_call_cost=args.tool_call_cost_usdc,
        )
    except ScaffoldError as exc:
        raise SystemExit(str(exc)) from exc

    migrations_dir = repo_root / "migrations" / "versions"
    migration_path = migrations_dir / plan.migration_filename
    benchmarks_path = repo_root / "teardrop" / "benchmarks.py"
    config_path = repo_root / "teardrop" / "config.py"

    print(f"Migration file: {migration_path.name}")
    print(plan.migration_sql)

    if not args.write:
        print("Dry run only. Re-run with --write to create the migration and patch benchmarks.py/config.py.")
        return 0

    migration_path.write_text(plan.migration_sql, encoding="utf-8")
    benchmarks_path.write_text(plan.benchmarks_content, encoding="utf-8")
    print(f"Wrote {migration_path}")
    print(f"Patched {benchmarks_path}")
    if plan.config_content:
        config_path.write_text(plan.config_content, encoding="utf-8")
        print(f"Patched {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
