from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto.config import (
    ConfigError,
    build_worktree_path,
    get_git_projects_root,
    get_next_adhoc_session_name,
    get_next_direct_session_name,
    get_project_worktrees_dir,
    get_worktree_root,
    list_projects,
    read_session_meta,
    slugify,
    store_session_meta,
)


class TestSlugify:
    def test_basic_title(self) -> None:
        assert slugify("fix unit tests") == "fix-unit-tests"

    def test_uppercase(self) -> None:
        assert slugify("UPPER CASE") == "upper-case"

    def test_special_characters(self) -> None:
        assert slugify("hello!! world@ #123") == "hello-world-123"

    def test_leading_trailing_hyphens(self) -> None:
        assert slugify("---foo---") == "foo"

    def test_consecutive_hyphens_collapsed(self) -> None:
        assert slugify("a---b") == "a-b"

    def test_already_slugged(self) -> None:
        assert slugify("already-slugged") == "already-slugged"

    def test_numbers_only(self) -> None:
        assert slugify("123") == "123"

    def test_empty_string(self) -> None:
        assert slugify("") == ""

    def test_only_special_chars(self) -> None:
        assert slugify("!!!") == ""

    def test_whitespace_trimmed(self) -> None:
        assert slugify("  spaced out  ") == "spaced-out"


class TestGetWorktreeRoot:
    def test_returns_path_when_set(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"FUJIMOTO_WORKTREE_ROOT": str(tmp_path)}):
            result = get_worktree_root()
            assert result == tmp_path
            assert result.is_dir()

    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        new_dir = tmp_path / "worktrees" / "nested"
        with patch.dict("os.environ", {"FUJIMOTO_WORKTREE_ROOT": str(new_dir)}):
            result = get_worktree_root()
            assert result == new_dir
            assert result.is_dir()

    def test_raises_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigError, match="FUJIMOTO_WORKTREE_ROOT"):
                get_worktree_root()

    def test_raises_when_empty(self) -> None:
        with patch.dict("os.environ", {"FUJIMOTO_WORKTREE_ROOT": ""}):
            with pytest.raises(ConfigError):
                get_worktree_root()

    def test_expands_tilde(self) -> None:
        with patch.dict(
            "os.environ",
            {"FUJIMOTO_WORKTREE_ROOT": "~/test-worktrees"},
        ):
            result = get_worktree_root()
            assert "~" not in str(result)
            assert result.is_absolute()


class TestBuildWorktreePath:
    def test_includes_date_and_slug(self, tmp_path: Path) -> None:
        with (
            patch.dict(
                "os.environ",
                {"FUJIMOTO_WORKTREE_ROOT": str(tmp_path)},
            ),
            patch("fujimoto.config.date") as mock_date,
        ):
            mock_date.today.return_value.strftime.return_value = "20260309"
            result = build_worktree_path("my-project", "fix unit tests")
            assert result == tmp_path / "my-project" / "20260309-fix-unit-tests"

    def test_nested_under_project(self, tmp_path: Path) -> None:
        with (
            patch.dict(
                "os.environ",
                {"FUJIMOTO_WORKTREE_ROOT": str(tmp_path)},
            ),
            patch("fujimoto.config.date") as mock_date,
        ):
            mock_date.today.return_value.strftime.return_value = "20260101"
            result = build_worktree_path("qsic-data", "test")
            assert result.parent.name == "qsic-data"
            assert result.name == "20260101-test"


class TestGetGitProjectsRoot:
    def test_returns_none_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert get_git_projects_root() is None

    def test_returns_none_when_empty(self) -> None:
        with patch.dict("os.environ", {"FUJIMOTO_GIT_ROOT": ""}):
            assert get_git_projects_root() is None

    def test_returns_resolved_path(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"FUJIMOTO_GIT_ROOT": str(tmp_path)}):
            result = get_git_projects_root()
            assert result == tmp_path
            assert result.is_absolute()

    def test_expands_tilde(self) -> None:
        with patch.dict("os.environ", {"FUJIMOTO_GIT_ROOT": "~/git"}):
            result = get_git_projects_root()
            assert result is not None
            assert "~" not in str(result)


class TestListProjects:
    def test_returns_empty_when_env_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert list_projects() == []

    def test_returns_empty_when_dir_missing(self) -> None:
        with patch.dict(
            "os.environ",
            {"FUJIMOTO_GIT_ROOT": "/nonexistent/path"},
        ):
            assert list_projects() == []

    def test_returns_git_repos_only(self, tmp_path: Path) -> None:
        # Create a git repo
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        # Create a non-repo dir
        plain = tmp_path / "plain-dir"
        plain.mkdir()
        # Create a file (not a dir)
        (tmp_path / "file.txt").touch()

        with patch.dict(
            "os.environ",
            {"FUJIMOTO_GIT_ROOT": str(tmp_path)},
        ):
            result = list_projects()
            assert len(result) == 1
            assert result[0].name == "my-repo"

    def test_returns_sorted_by_name(self, tmp_path: Path) -> None:
        for name in ["charlie", "alpha", "bravo"]:
            d = tmp_path / name
            d.mkdir()
            (d / ".git").mkdir()

        with patch.dict(
            "os.environ",
            {"FUJIMOTO_GIT_ROOT": str(tmp_path)},
        ):
            result = list_projects()
            names = [p.name for p in result]
            assert names == ["alpha", "bravo", "charlie"]


class TestGetProjectWorktreesDir:
    def test_returns_project_subdir(self, tmp_path: Path) -> None:
        with patch.dict(
            "os.environ",
            {"FUJIMOTO_WORKTREE_ROOT": str(tmp_path)},
        ):
            result = get_project_worktrees_dir("my-project")
            assert result == tmp_path / "my-project"


class TestStoreSessionMeta:
    def test_writes_meta_file(self, tmp_path: Path) -> None:
        store_session_meta(tmp_path, "main")
        meta_path = tmp_path / ".fujimoto-meta.json"
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        assert data["base_branch"] == "main"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        store_session_meta(tmp_path, "main")
        store_session_meta(tmp_path, "develop")
        data = json.loads((tmp_path / ".fujimoto-meta.json").read_text())
        assert data["base_branch"] == "develop"


class TestReadSessionMeta:
    def test_reads_meta_file(self, tmp_path: Path) -> None:
        meta_path = tmp_path / ".fujimoto-meta.json"
        meta_path.write_text(json.dumps({"base_branch": "main"}))
        result = read_session_meta(tmp_path)
        assert result["base_branch"] == "main"

    def test_returns_empty_when_missing(self, tmp_path: Path) -> None:
        result = read_session_meta(tmp_path)
        assert result == {}

    def test_returns_empty_on_invalid_json(self, tmp_path: Path) -> None:
        meta_path = tmp_path / ".fujimoto-meta.json"
        meta_path.write_text("not json")
        result = read_session_meta(tmp_path)
        assert result == {}


class TestGetNextDirectSessionName:
    def test_returns_direct_1_when_no_sessions(self) -> None:
        result = get_next_direct_session_name("proj", set())
        assert result == "proj/direct-1"

    def test_increments_past_existing(self) -> None:
        sessions = {"proj/direct-1", "proj/direct-2"}
        result = get_next_direct_session_name("proj", sessions)
        assert result == "proj/direct-3"

    def test_fills_gaps(self) -> None:
        sessions = {"proj/direct-2"}
        result = get_next_direct_session_name("proj", sessions)
        assert result == "proj/direct-1"


class TestGetNextAdhocSessionName:
    def test_returns_adhoc_1_when_no_sessions(self) -> None:
        result = get_next_adhoc_session_name(set())
        assert result == "adhoc-1"

    def test_increments_past_existing(self) -> None:
        sessions = {"adhoc-1", "adhoc-2"}
        result = get_next_adhoc_session_name(sessions)
        assert result == "adhoc-3"

    def test_fills_gaps(self) -> None:
        sessions = {"adhoc-2"}
        result = get_next_adhoc_session_name(sessions)
        assert result == "adhoc-1"
