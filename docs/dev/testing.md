# Testing

Tests use pytest with pytest-asyncio for TUI tests and pytest-cov for coverage. Run with:

```sh
uv run pytest
```

Coverage is reported automatically (configured in `pyproject.toml`).

`tests/conftest.py` points `XDG_CONFIG_HOME` at a per-test temp dir (autouse),
so a real `~/.config/fujimoto/config.yaml` — e.g. a default `model` — can't
leak into launch tests' expected claude arguments. Write global config in a
test through `global_config_path()`.

### Matrix testing with nox

`noxfile.py` defines sessions that run the test suite across Python versions and pinned dependency versions, using uv as the venv backend (`nox.options.default_venv_backend = "uv"`).

- `uv run nox` — default session: `tests` against every supported Python (3.11–3.14). Each session runs `uv sync --active --group=dev` into the nox-managed venv, then `pytest`.
- `uv run nox -s tests-3.14` — single Python version.
- `uv run nox -s tests_textual` — parametrized over `TEXTUAL_VERSIONS` (currently 8.0.2 and 8.2.8). Add new versions to the constant in `noxfile.py` to extend the matrix.

To add a new Python version: append it to `PYTHON_VERSIONS` in `noxfile.py`, add the classifier to `pyproject.toml`, and add the version to the CI matrix in `.github/workflows/tests.yml`. To add a dependency-pin matrix for a new package, follow the `tests_textual` pattern: define a `*_VERSIONS` list and parametrize a session with `@nox.parametrize`.

CI runs the per-Python `tests` session as a GitHub Actions matrix; the coverage badge is generated only on the 3.13 job to avoid races.

**Maximize test coverage.** Write tests for all new code — unit tests for logic, async TUI tests using Textual's `app.run_test()` pilot for UI flows. Only skip coverage for lines that are genuinely impractical to test (e.g. defensive error handlers in deeply nested async TUI paths that can't be triggered through the pilot). Use `# pragma: no cover` sparingly and only with justification.

TUI tests follow this pattern:
- Patch external dependencies (`git`, `tmux`, `config`) via `_patch_git_info()` helper
- Use `async with app.run_test() as pilot:` to drive the UI
- Use `pilot.press()` to simulate keyboard input
- Navigate to items by setting `list.index` directly (more reliable than repeated `pilot.press("down")`)
- Assert on app state (`_launch_target`, `_base_branch`, `_session_map`) and DOM queries (`app.query()`)

The `mcp` package is in the `dev` group as well as the `mcp` extra. A plain
`uv run pytest` re-syncs the environment *without* extras, which would
otherwise drop `mcp` and silently skip `tests/test_mcp_server.py`.
