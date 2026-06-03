from __future__ import annotations

import os
from pathlib import Path

import pytest

from fujimoto.config import ConfigError
from fujimoto.project_config import (
    CONFIG_FILENAME,
    ApplyResult,
    CopyEntry,
    InitCommand,
    LinkEntry,
    LinkType,
    OnError,
    ProjectConfig,
    Trigger,
    When,
    apply_project_config,
    load_project_config,
    template_text,
    write_config_template,
)


def _write_config(root: Path, body: str) -> None:
    (root / CONFIG_FILENAME).write_text(body)


# -- When.runs_on / Trigger --


@pytest.mark.parametrize(
    ("when", "trigger", "expected"),
    [
        (When.ONCE, Trigger.CREATE, True),
        (When.ONCE, Trigger.LAUNCH, False),
        (When.ALWAYS, Trigger.CREATE, True),
        (When.ALWAYS, Trigger.LAUNCH, True),
    ],
)
def test_runs_on_matrix(when: When, trigger: Trigger, expected: bool) -> None:
    assert when.runs_on(trigger) is expected


# -- Parsing --


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)
    assert config.copy_entries == []
    assert config.link_entries == []
    assert config.init_commands == []


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    _write_config(tmp_path, "# only comments\n")
    assert load_project_config(tmp_path) == ProjectConfig()


def test_parse_string_and_dict_entries(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        """
        copy:
          - .env
          - path: secrets.json
            when: always
        link:
          - path: shared.db
            type: symbolic
        init:
          - uv sync
          - run: echo hi
            when: always
            continue_on_error: true
            cwd: /somewhere
        """,
    )
    config = load_project_config(tmp_path)
    assert config.copy_entries == [
        CopyEntry(path=".env", when=When.ONCE),
        CopyEntry(path="secrets.json", when=When.ALWAYS),
    ]
    assert config.link_entries == [
        LinkEntry(path="shared.db", type=LinkType.SYMBOLIC, when=When.ONCE),
    ]
    assert config.init_commands == [
        InitCommand(run="uv sync"),
        InitCommand(
            run="echo hi", when=When.ALWAYS, continue_on_error=True, cwd="/somewhere"
        ),
    ]


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    _write_config(tmp_path, "copy: [unterminated\n")
    with pytest.raises(ConfigError):
        load_project_config(tmp_path)


def test_unknown_field_raises_config_error(tmp_path: Path) -> None:
    _write_config(tmp_path, "copy:\n  - path: .env\n    nope: 1\n")
    with pytest.raises(ConfigError):
        load_project_config(tmp_path)


def test_invalid_enum_value_raises_config_error(tmp_path: Path) -> None:
    _write_config(tmp_path, "copy:\n  - path: .env\n    when: sometimes\n")
    with pytest.raises(ConfigError):
        load_project_config(tmp_path)


# -- Copy --


def test_copy_literal_file(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / ".env").write_text("SECRET=1")
    config = ProjectConfig(copy=[CopyEntry(path=".env")])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert (wt / ".env").read_text() == "SECRET=1"
    assert result.warnings == []


def test_copy_creates_parent_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    (src / "config").mkdir(parents=True)
    wt.mkdir()
    (src / "config" / "dev.yaml").write_text("x")
    config = ProjectConfig(copy=[CopyEntry(path="config/dev.yaml")])
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert (wt / "config" / "dev.yaml").read_text() == "x"


def test_copy_glob(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    (src / "certs").mkdir(parents=True)
    wt.mkdir()
    (src / "certs" / "a.pem").write_text("a")
    (src / "certs" / "b.pem").write_text("b")
    (src / "certs" / "skip.txt").write_text("no")
    config = ProjectConfig(copy=[CopyEntry(path="certs/*.pem")])
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert (wt / "certs" / "a.pem").exists()
    assert (wt / "certs" / "b.pem").exists()
    assert not (wt / "certs" / "skip.txt").exists()


def test_copy_missing_source_warns(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    config = ProjectConfig(copy=[CopyEntry(path="nope.env")])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert any("no match" in w for w in result.warnings)


def test_copy_skipped_when_trigger_mismatch(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / ".env").write_text("x")
    # `once` should not run on LAUNCH
    config = ProjectConfig(copy=[CopyEntry(path=".env", when=When.ONCE)])
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.LAUNCH
    )
    assert not (wt / ".env").exists()


def test_copy_always_overwrites_on_launch(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / ".env").write_text("new")
    (wt / ".env").write_text("old")
    config = ProjectConfig(copy=[CopyEntry(path=".env", when=When.ALWAYS)])
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.LAUNCH
    )
    assert (wt / ".env").read_text() == "new"


# -- Link --


def test_hard_link(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / "data.bin").write_text("shared")
    config = ProjectConfig(link=[LinkEntry(path="data.bin")])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert (wt / "data.bin").read_text() == "shared"
    assert (src / "data.bin").stat().st_ino == (wt / "data.bin").stat().st_ino
    assert result.warnings == []


def test_symbolic_link(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / "data.bin").write_text("shared")
    config = ProjectConfig(link=[LinkEntry(path="data.bin", type=LinkType.SYMBOLIC)])
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert (wt / "data.bin").is_symlink()
    assert (wt / "data.bin").read_text() == "shared"


def test_hard_link_cross_device_falls_back_to_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / "data.bin").write_text("shared")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("cross-device link")

    monkeypatch.setattr(os, "link", _boom)
    config = ProjectConfig(link=[LinkEntry(path="data.bin")])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert (wt / "data.bin").read_text() == "shared"
    assert not (wt / "data.bin").is_symlink()
    assert any("hard link not possible" in w for w in result.warnings)


def test_link_string_form_parses(tmp_path: Path) -> None:
    _write_config(tmp_path, "link:\n  - shared.db\n")
    config = load_project_config(tmp_path)
    assert config.link_entries == [LinkEntry(path="shared.db")]


def test_copy_failure_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil as shutil_mod

    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / ".env").write_text("x")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(shutil_mod, "copy2", _boom)
    config = ProjectConfig(copy=[CopyEntry(path=".env")])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert any("copy: failed" in w for w in result.warnings)


def test_symlink_failure_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / "data.bin").write_text("x")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("symlink denied")

    monkeypatch.setattr(os, "symlink", _boom)
    config = ProjectConfig(link=[LinkEntry(path="data.bin", type=LinkType.SYMBOLIC)])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert any("link: failed" in w for w in result.warnings)


def test_link_missing_source_warns(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    config = ProjectConfig(link=[LinkEntry(path="ghost.bin")])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert any("link: no match" in w for w in result.warnings)


def test_link_skipped_on_trigger_mismatch(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / "data.bin").write_text("x")
    config = ProjectConfig(link=[LinkEntry(path="data.bin", when=When.ONCE)])
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.LAUNCH
    )
    assert not (wt / "data.bin").exists()


def test_link_overwrites_existing_dest(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / "data.bin").write_text("shared")
    (wt / "data.bin").write_text("stale")
    config = ProjectConfig(link=[LinkEntry(path="data.bin", type=LinkType.SYMBOLIC)])
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert (wt / "data.bin").is_symlink()
    assert (wt / "data.bin").read_text() == "shared"


# -- Init --


def test_init_skipped_on_trigger_mismatch(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    config = ProjectConfig(init=[InitCommand(run="touch nope", when=When.ONCE)])
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.LAUNCH
    )
    assert not (wt / "nope").exists()


def test_init_oserror_is_reported(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    # A non-existent cwd makes subprocess.run raise an OSError.
    config = ProjectConfig(init=[InitCommand(run="echo hi", cwd="/no/such/dir")])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert result.init_error is not None


def test_init_runs_and_substitutes_placeholders(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    config = ProjectConfig(init=[InitCommand(run="echo {{ source_dir }} > out.txt")])
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert result.init_error is None
    # cwd defaults to worktree root
    assert (wt / "out.txt").read_text().strip() == str(src)


def test_init_cwd_override(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    config = ProjectConfig(
        init=[InitCommand(run="pwd > here.txt", cwd="{{ source_dir }}")]
    )
    apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert (src / "here.txt").exists()


def test_init_stops_on_failure(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    config = ProjectConfig(
        init=[
            InitCommand(run="exit 1"),
            InitCommand(run="touch should_not_exist"),
        ]
    )
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert result.init_error is not None
    assert not (wt / "should_not_exist").exists()


def test_init_continue_on_error(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    config = ProjectConfig(
        init=[
            InitCommand(run="exit 1", continue_on_error=True),
            InitCommand(run="touch after"),
        ]
    )
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert result.init_error is None
    assert any("failed" in w for w in result.warnings)
    assert (wt / "after").exists()


# -- Template --


def test_template_text_is_valid_config(tmp_path: Path) -> None:
    text = template_text()
    assert CONFIG_FILENAME in text
    # The scaffold is fully commented, so it must parse to an empty config.
    (tmp_path / CONFIG_FILENAME).write_text(text)
    assert load_project_config(tmp_path) == ProjectConfig()


def test_write_config_template_creates_file(tmp_path: Path) -> None:
    dest = write_config_template(tmp_path)
    assert dest == tmp_path / CONFIG_FILENAME
    assert dest.exists()


def test_write_config_template_refuses_overwrite(tmp_path: Path) -> None:
    write_config_template(tmp_path)
    with pytest.raises(ConfigError):
        write_config_template(tmp_path)


def test_apply_result_default() -> None:
    assert ApplyResult().actions == []
    assert ApplyResult().warnings == []
    assert ApplyResult().init_error is None


# -- on_error --


def test_on_error_defaults_to_abort(tmp_path: Path) -> None:
    _write_config(tmp_path, "init:\n  - uv sync\n")
    assert load_project_config(tmp_path).on_error is OnError.ABORT


def test_on_error_parses_continue(tmp_path: Path) -> None:
    _write_config(tmp_path, "on_error: continue\ninit:\n  - uv sync\n")
    assert load_project_config(tmp_path).on_error is OnError.CONTINUE


def test_on_error_invalid_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "on_error: maybe\n")
    with pytest.raises(ConfigError):
        load_project_config(tmp_path)


# -- action log --


def test_actions_record_what_ran(tmp_path: Path) -> None:
    src = tmp_path / "src"
    wt = tmp_path / "wt"
    src.mkdir()
    wt.mkdir()
    (src / ".env").write_text("x")
    (src / "data.bin").write_text("y")
    config = ProjectConfig(
        copy=[CopyEntry(path=".env")],
        link=[LinkEntry(path="data.bin", type=LinkType.SYMBOLIC)],
        init=[InitCommand(run="true")],
    )
    result = apply_project_config(
        config, source_root=src, worktree_root=wt, trigger=Trigger.CREATE
    )
    assert any("copied .env" in a for a in result.actions)
    assert any("symlinked data.bin" in a for a in result.actions)
    assert any("ran: true" in a for a in result.actions)
