"""Nox sessions for matrix testing across Python versions and dependency pins.

Run all default sessions:
    uv run nox

Run tests against a single Python version:
    uv run nox -s tests-3.14

Run the textual matrix:
    uv run nox -s tests_textual
"""

from __future__ import annotations

import nox

nox.options.default_venv_backend = "uv"
nox.options.reuse_existing_virtualenvs = True
nox.options.sessions = ["tests"]

PYTHON_VERSIONS = ["3.11", "3.12", "3.13", "3.14"]
TEXTUAL_VERSIONS = ["8.0.2", "9.0.0"]


@nox.session(python=PYTHON_VERSIONS)
def tests(session: nox.Session) -> None:
    """Run the test suite against a single Python version."""
    session.run_install(
        "uv",
        "sync",
        "--active",
        "--group=dev",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )
    session.run("pytest", *session.posargs)


@nox.session(python=PYTHON_VERSIONS[-1])
@nox.parametrize("textual", TEXTUAL_VERSIONS)
def tests_textual(session: nox.Session, textual: str) -> None:
    """Run the test suite against pinned versions of textual."""
    session.run_install(
        "uv",
        "sync",
        "--active",
        "--group=dev",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )
    session.install(f"textual=={textual}")
    session.run("pytest", *session.posargs)
