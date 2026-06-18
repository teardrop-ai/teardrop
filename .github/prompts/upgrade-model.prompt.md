---
name: upgrade-model
description: "Scaffold an LLM upgrade workflow using llm-lifecycle-manager. Usage: /upgrade-model <provider> <model> [--role <role>]"
---
You are responding to an `/upgrade-model` command.
The user expects a seamless, one-shot operation to scaffold a new model upgrade using the `llm-lifecycle-manager` skill.
The user has provided variables inline (e.g. `@workspace /upgrade-model google gemini-3.5-flash --role planner`).

Please extract the `provider`, `model`, and `--role` from the user's input alongside this command, and execute the scaffolding.

**Instructions**:
1. Identify the target `provider` (e.g., google) and `model` (e.g., gemini-3.5-flash) from the prompt arguments.
2. Determine if a `--role` (primary, planner, synthesis) was specified.
3. Review the current codebase (`teardrop/config.py`, `teardrop/benchmarks.py`) to determine what display name, model id, context window, etc., should be used, referencing the model it is replacing.
4. Run `python scripts/scaffold_model_upgrade.py --provider <provider> --model <model> ...`
   Supply all necessary arguments. 
   **Do NOT** supply `--provider-input-price-per-1m` or `--provider-output-price-per-1m`, as the script handles OpenRouter API pricing natively.
   If a role was specified, add `--update-config-role <role>`.
5. Run the script in dry-run first, and if confident in the SQL output and benchmark/config edits, run it immediately afterward with `--write`.