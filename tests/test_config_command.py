from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fujimoto import config_command as cc
from fujimoto.config import ConfigError
from fujimoto.git import GitError
from fujimoto.global_config import global_config_path
from fujimoto.project_config import template_text


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    with patch("fujimoto.config_command.get_main_worktree_root", return_value=root):
        yield root


@pytest.fixture
def no_repo():
    with patch(
        "fujimoto.config_command.get_main_worktree_root",
        side_effect=GitError("not a repo"),
    ):
        yield


def _global(text: str) -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestReport:
    def test_lists_every_key_even_when_nothing_is_set(self, repo: Path) -> None:
        report = cc.build_report()
        assert [k.spec.name for k in report.keys] == [s.name for s in cc.KEYS]
        assert all(k.effective == (None, None) for k in report.keys)
        text = cc.format_report(report)
        for spec in cc.KEYS:
            assert f"{spec.name} = (unset)" in text
            assert spec.description in text
        assert "global: (unset) · project: (unset)" in text
        assert "[not created]" in text

    def test_json_has_unset_keys_with_scopes(self, repo: Path) -> None:
        data = cc.report_json(cc.build_report())
        model = next(k for k in data["keys"] if k["key"] == "model")  # type: ignore[union-attr]
        assert model == {
            "key": "model",
            "description": cc.spec_for("model").description,
            "scopes": ["global", "project"],
            "settable": True,
            "global": None,
            "project": None,
            "effective": None,
            "source": None,
        }
        copy = next(k for k in data["keys"] if k["key"] == "copy")  # type: ignore[union-attr]
        assert copy["settable"] is False
        json.dumps(data)  # serialisable

    def test_effective_prefers_project(self, repo: Path) -> None:
        _global("model: opus\nquick_terminal: true\n")
        (repo / ".fujimoto.yaml").write_text(
            "model: sonnet\nclaude_args: --verbose\ncopy: [.env]\n"
        )
        report = cc.build_report()
        by = {k.spec.name: k for k in report.keys}
        assert by["model"].effective == ("sonnet", cc.Scope.PROJECT)
        assert by["model"].global_value == "opus"
        assert by["quick_terminal"].effective == (True, cc.Scope.GLOBAL)
        text = cc.format_report(report)
        assert "model = sonnet  (project)" in text
        assert "claude_args = --verbose  (project)" in text
        assert "on_error = (unset)" in text
        assert "[exists]" in text

    def test_invalid_files_are_reported_not_raised(self, repo: Path) -> None:
        _global("modle: x\n")
        (repo / ".fujimoto.yaml").write_text("nope: 1\n")
        report = cc.build_report()
        assert report.global_file.error and report.project_file.error
        text = cc.format_report(report)
        assert text.count("[invalid]") == 2
        assert "  ! " in text

    def test_no_repository(self, no_repo: None) -> None:
        report = cc.build_report()
        assert report.project_file.path is None
        assert "(not in a git repository)" in cc.format_report(report)
        assert cc.report_json(report)["files"]["project"] is None  # type: ignore[index]

    def test_environment(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FUJIMOTO_META_KEY", "")
        monkeypatch.setenv("FUJIMOTO_TMUX_PREFIX", "C-z")
        monkeypatch.delenv("FUJIMOTO_LOG_DIR", raising=False)
        text = cc.format_report(cc.build_report())
        assert "FUJIMOTO_META_KEY = ''" in text
        assert "FUJIMOTO_TMUX_PREFIX = C-z" in text
        assert "FUJIMOTO_LOG_DIR = (default: ~/.fujimoto/logs)" in text


class TestGetSetUnset:
    def test_set_global_then_get(self, no_repo: None) -> None:
        result = cc.set_value("model", "opus", cc.Scope.GLOBAL)
        assert result.changed and result.path == global_config_path()
        assert cc.get_value("model") == ("opus", cc.Scope.GLOBAL)
        assert cc.get_value("model", cc.Scope.GLOBAL) == ("opus", cc.Scope.GLOBAL)
        assert not cc.set_value("model", "opus", cc.Scope.GLOBAL).changed

    def test_set_project_keeps_template(self, repo: Path) -> None:
        path = repo / ".fujimoto.yaml"
        path.write_text(template_text())
        cc.set_value("model", "sonnet", cc.Scope.PROJECT)
        cc.set_value("claude_args", "--plugin-dir './my plugins'", cc.Scope.PROJECT)
        cc.set_value("on_error", "Continue", cc.Scope.PROJECT)
        assert path.read_text().startswith(template_text())
        assert yaml.safe_load(path.read_text()) == {
            "model": "sonnet",
            "claude_args": ["--plugin-dir", "./my plugins"],
            "on_error": "continue",
        }
        assert cc.get_value("model") == ("sonnet", cc.Scope.PROJECT)
        assert cc.get_value("model", cc.Scope.GLOBAL) == (None, None)

    def test_unset(self, repo: Path) -> None:
        cc.set_value("model", "opus", cc.Scope.PROJECT)
        assert cc.unset_value("model", cc.Scope.PROJECT).changed
        assert not cc.unset_value("model", cc.Scope.PROJECT).changed
        assert cc.get_value("model") == (None, None)

    @pytest.mark.parametrize(
        ("text", "expected"), [("on", True), ("False", False), ("1", True)]
    )
    def test_bool_parsing(self, no_repo: None, text: str, expected: bool) -> None:
        cc.set_value("quick_terminal", text, cc.Scope.GLOBAL)
        assert cc.get_value("quick_terminal")[0] is expected

    @pytest.mark.parametrize(
        ("key", "value", "scope", "message"),
        [
            ("bogus", "x", cc.Scope.GLOBAL, "unknown config key"),
            ("quick_terminal", "true", cc.Scope.PROJECT, "--global"),
            ("claude_args", "x", cc.Scope.GLOBAL, "--project"),
            ("quick_terminal", "maybe", cc.Scope.GLOBAL, "true/false"),
            ("model", "  ", cc.Scope.GLOBAL, "must not be empty"),
            ("on_error", "explode", cc.Scope.PROJECT, "abort, continue"),
            ("claude_args", "'unbalanced", cc.Scope.PROJECT, "cannot split"),
            ("copy", ".env", cc.Scope.PROJECT, "edit .fujimoto.yaml"),
        ],
    )
    def test_set_errors(
        self, repo: Path, key: str, value: str, scope: cc.Scope, message: str
    ) -> None:
        with pytest.raises(ConfigError, match=message):
            cc.set_value(key, value, scope)

    def test_unset_structured_key_refused(self, repo: Path) -> None:
        with pytest.raises(ConfigError, match="structured"):
            cc.unset_value("init", cc.Scope.PROJECT)

    def test_project_needs_a_repository(self, no_repo: None) -> None:
        with pytest.raises(ConfigError, match="git repository"):
            cc.set_value("model", "x", cc.Scope.PROJECT)

    def test_get_on_invalid_file_raises(self, no_repo: None) -> None:
        _global("modle: x\n")
        with pytest.raises(ConfigError, match="Invalid"):
            cc.get_value("model")

    def test_get_wrong_scope(self, no_repo: None) -> None:
        with pytest.raises(ConfigError, match="--global"):
            cc.get_value("quick_terminal", cc.Scope.PROJECT)


class TestFormatValue:
    def test_structured(self) -> None:
        assert cc.format_value([{"path": ".env"}]) == '[{"path": ".env"}]'


class TestCli:
    """`fujimoto config …` through `main()`."""

    def run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        *argv: str,
    ) -> tuple[int, str, str]:
        from fujimoto.cli import main

        monkeypatch.setattr(sys, "argv", ["fujimoto", "config", *argv])
        code = 0
        try:
            main()
        except SystemExit as exc:
            code = int(exc.code or 0)
        out, err = capsys.readouterr()
        return code, out, err

    def test_bare_config_lists(self, repo: Path, monkeypatch, capsys) -> None:
        code, out, _ = self.run(monkeypatch, capsys)
        assert code == 0
        assert "model = (unset)" in out and "Environment (read-only)" in out

    def test_list_json(self, repo: Path, monkeypatch, capsys) -> None:
        code, out, _ = self.run(monkeypatch, capsys, "list", "--json")
        assert code == 0
        assert {k["key"] for k in json.loads(out)["keys"]} >= {"model", "copy"}

    def test_json_before_action(self, repo: Path, monkeypatch, capsys) -> None:
        code, out, _ = self.run(monkeypatch, capsys, "--json")
        assert code == 0 and json.loads(out)["files"]

    def test_set_get_unset(self, repo: Path, monkeypatch, capsys) -> None:
        code, out, _ = self.run(monkeypatch, capsys, "set", "model", "opus", "--global")
        assert code == 0 and out.startswith("Set model in ")
        code, out, _ = self.run(monkeypatch, capsys, "set", "model", "opus", "--global")
        assert out.startswith("Unchanged: model")
        code, out, _ = self.run(monkeypatch, capsys, "get", "model")
        assert (code, out) == (0, "opus\n")
        code, out, _ = self.run(monkeypatch, capsys, "get", "model", "--json")
        assert json.loads(out) == {"key": "model", "value": "opus", "source": "global"}
        code, out, _ = self.run(monkeypatch, capsys, "unset", "model", "--global")
        assert code == 0 and out.startswith("Removed model")
        code, out, _ = self.run(monkeypatch, capsys, "unset", "model", "--global")
        assert out.startswith("Not set: model")

    def test_get_unset_exits_1(self, repo: Path, monkeypatch, capsys) -> None:
        code, out, _ = self.run(monkeypatch, capsys, "get", "model", "--project")
        assert (code, out) == (1, "")
        code, out, _ = self.run(monkeypatch, capsys, "get", "model", "--json")
        assert code == 1 and json.loads(out)["value"] is None

    def test_set_requires_a_scope(self, repo: Path, monkeypatch, capsys) -> None:
        code, _, err = self.run(monkeypatch, capsys, "set", "model", "opus")
        assert code == 2 and "--global" in err

    def test_value_after_double_dash(self, repo: Path, monkeypatch, capsys) -> None:
        code, _, _ = self.run(
            monkeypatch,
            capsys,
            "set",
            "claude_args",
            "--project",
            "--",
            "--verbose",
            "a b",
        )
        assert code == 0
        data = yaml.safe_load((repo / ".fujimoto.yaml").read_text())
        assert data == {"claude_args": ["--verbose", "a b"]}

    def test_value_twice_is_an_error(self, repo: Path, monkeypatch, capsys) -> None:
        code, _, err = self.run(
            monkeypatch, capsys, "set", "claude_args", "x", "--project", "--", "y"
        )
        assert code == 1 and "before or after --" in err

    def test_double_dash_only_for_set(self, repo: Path, monkeypatch, capsys) -> None:
        code, _, err = self.run(monkeypatch, capsys, "get", "model", "--", "x")
        assert code == 2 and "config set" in err

    def test_error_exits_1(self, repo: Path, monkeypatch, capsys) -> None:
        code, _, err = self.run(monkeypatch, capsys, "set", "bogus", "x", "--global")
        assert code == 1 and "unknown config key" in err
