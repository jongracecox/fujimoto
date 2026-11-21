# Development workflow

## Commands

```sh
uv sync                                        # Install dependencies
uv run fujimoto                          # Run locally (must be inside a git repo)
uv run pytest                                  # Run tests with coverage
uv run nox                                     # Run tests across all supported Python versions
uv run nox -s tests-3.14                       # Run tests against a single Python version
uv run nox -s tests_textual                    # Run dependency-pin matrix (textual)
uv tool install --force --reinstall --no-cache .  # Install globally (re-run after code changes)
```

## Linting and type checking

Pre-commit hooks handle all linting and formatting automatically — do not run `ruff`, `ty`, or other linters manually. Let the hooks run at commit time and fix any issues they report. Any new linting or formatting tools should be added to `.pre-commit-config.yaml`, not run ad hoc.

Current hooks:
- **ruff** — linting and formatting
- **ty** — type checking (strict: no `unresolved-attribute` allowed)

All widget state must use typed instance variables, not dynamic attributes on Textual widgets.

## Releases

Releases are published to PyPI by `.github/workflows/release.yml` on `v*`
tag push. The package version is **derived from the git tag** by `hatch-vcs`
(`dynamic = ["version"]` in `pyproject.toml`) — do not add a static `version`
field, do not edit a version string when cutting a release. The release flow,
recovery procedures, and one-time setup are documented in
[CONTRIBUTING.md](../../CONTRIBUTING.md#releasing).
