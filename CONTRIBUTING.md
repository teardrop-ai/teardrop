# Contributing to Teardrop

Thank you for your interest in contributing. Teardrop is licensed under the
[Business Source License 1.1](LICENSE). By submitting a contribution you agree
that it will be governed by the same license.

---

## Development Setup

**Requirements:** Python 3.11+, Docker (optional), a Postgres database.

```powershell
# 1. Clone and enter the project
cd "C:\Users\<you>\Documents\Local Repositiories\teardrop"

# 2. Create and activate a virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3. Install runtime + dev dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 4. Configure environment
Copy-Item .env.example .env
# Fill in DATABASE_URL and ANTHROPIC_API_KEY at minimum

# 5. Generate RSA keys and run migrations
python scripts/generate_keys.py
python -m migrations.runner

# 6. Seed a local admin user
python scripts/seed_users.py

# 7. Start the API
uvicorn app:app --reload
```

The API will be available at `http://localhost:8000`. Interactive docs at
`http://localhost:8000/docs`.

---

## Code Style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```powershell
# Check for lint errors
ruff check .

# Auto-fix safe issues
ruff check --fix .

# Format
ruff format .
```

CI will reject PRs that fail `ruff check .`. Run it before pushing.

---

## Testing

```powershell
# Run all tests
pytest

# Run with coverage
pytest --cov --cov-report=term-missing

# Run a specific test file
pytest tests/unit/test_auth.py -v
```

Coverage must stay above 60% (`fail_under = 60` in `pyproject.toml`).

---

## Pull Request Process

1. **Open an issue first** for non-trivial changes. Describe what you want to
   fix or add before writing code.
2. Fork the repo and create a feature branch from `main`.
3. Keep commits small and focused. Write clear commit messages.
4. Add or update tests for any changed behaviour.
5. Run `ruff check .` and `pytest` locally before pushing.
6. Submit a pull request referencing the issue.
7. A maintainer will review within a few business days.

---

## Adding a Tool

Tools live in `tools/definitions/`. Each file exports a single `TOOL`
variable of type `ToolDefinition`. See `tools/definitions/get_datetime.py`
for the simplest example.

1. Create `tools/definitions/my_tool.py` with your `TOOL` definition.
2. The `register_all()` function in `tools/definitions/__init__.py` picks it
   up automatically via `tools/definitions/*.py` glob — no manual wiring.
3. Add unit tests in `tests/unit/test_my_tool.py`.

---

## License

By contributing to Teardrop, you agree that your contributions are licensed
under the [Business Source License 1.1](LICENSE). The Change Date is
**April 3, 2030**, after which all code becomes AGPL-3.0-only.

For commercial licensing enquiries, see the contact in the LICENSE file.
