from __future__ import annotations

import io
from pathlib import Path

import pytest

from fujimoto import debug


@pytest.fixture(autouse=True)
def _no_leaked_logger() -> None:
    """Make sure a test never leaves the process-wide logger enabled."""
    debug.disable()
    yield
    debug.disable()


def _logger(*, redact: bool = False) -> tuple[debug.DebugLogger, io.StringIO]:
    stream = io.StringIO()
    return (
        debug.DebugLogger(Path("/dev/null"), redact=redact, stream=stream),
        stream,
    )


class TestRedactText:
    def test_empty(self) -> None:
        assert debug.redact_text("") == "[REDACTED-EMPTY]"

    def test_alphanumeric_keeps_length(self) -> None:
        token = debug.redact_text("myproject")
        assert token.startswith("[REDACTED-")
        assert token.endswith("-9]")
        assert "CONTAINS" not in token

    def test_specials_are_listed_once_in_order(self) -> None:
        assert debug.redact_text("a.b-c.d").endswith("-CONTAINS.-]")

    def test_same_value_same_token(self) -> None:
        assert debug.redact_text("repo") == debug.redact_text("repo")

    def test_different_values_differ(self) -> None:
        assert debug.redact_text("repo-a") != debug.redact_text("repo-b")

    def test_unicode_is_treated_as_special(self) -> None:
        # Alphanumeric unicode stays out of CONTAINS; punctuation shows up.
        assert "CONTAINS" not in debug.redact_text("café")
        assert debug.redact_text("a→b").endswith("-CONTAINS→]")


class TestRedactPath:
    def test_home_collapses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))
        assert debug.redact_path("/Users/bob") == "~"
        # `git` is a name the user chose, so it is redacted — but the `~`
        # prefix still shows the path was home-relative.
        assert debug.redact_path("/Users/bob/git").startswith("~/[REDACTED-")

    def test_safe_components_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))
        out = debug.redact_path("/Users/bob/.fujimoto/worktrees/proj/20260827-thing")
        assert out.startswith("~/.fujimoto/worktrees/")
        assert "proj" not in out
        assert "20260827-thing" not in out
        assert out.count("/") == 4

    def test_non_home_path_redacts_unknown_components(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))
        out = debug.redact_path("/Users/alice/secretproj")
        assert out.startswith("/Users/")
        assert "alice" not in out
        assert "secretproj" not in out

    def test_accepts_path_objects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))
        assert debug.redact_path(Path("/tmp")) == "/tmp"


class TestFingerprintSalt:
    """Fingerprints correlate within a log and mean nothing outside it."""

    def test_same_value_correlates_within_a_run(self, tmp_path: Path) -> None:
        debug.enable(redact=True, log_dir=tmp_path)
        first = debug.redact_text("myproject")
        second = debug.redact_text("myproject")
        debug.disable()
        assert first == second

    def test_fingerprint_differs_across_runs(self, tmp_path: Path) -> None:
        debug.enable(redact=True, log_dir=tmp_path)
        first = debug.redact_text("myproject")
        debug.disable()
        debug.enable(redact=True, log_dir=tmp_path)
        second = debug.redact_text("myproject")
        debug.disable()
        assert first != second
        # Only the fingerprint changes; the shape is still reported.
        assert first.endswith("-9]") and second.endswith("-9]")

    def test_salt_is_never_written_to_the_log(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=True, log_dir=tmp_path)
        debug.log("evt", value=debug.rv("myproject"))
        salt = debug._salt.hex()
        debug.disable()
        text = logger.path.read_text()
        assert salt not in text
        assert "salted per run" in text

    def test_known_plaintext_cannot_be_matched_offline(self, tmp_path: Path) -> None:
        # A reader who guesses the value and hashes it unsalted must not match.
        import hashlib

        debug.enable(redact=True, log_dir=tmp_path)
        token = debug.redact_text("fujimoto")
        debug.disable()
        naive = hashlib.sha256(b"fujimoto").hexdigest()[:4]
        assert naive not in token


class TestPathLeaks:
    """Regression: ordinary words are names, and must not survive redaction.

    `fujimoto` and `git` were previously in the safe-component list, which
    leaked the project name of any repo called one of them — including
    fujimoto's own, and `~/git/<project>` for everyone else.
    """

    @pytest.fixture(autouse=True)
    def _home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))

    @pytest.mark.parametrize(
        "component", ["fujimoto", "git", "logs", "src", "main", "master", "myproject"]
    )
    def test_ordinary_words_are_redacted(self, component: str) -> None:
        out = debug.redact_path(f"/Users/bob/parent/{component}")
        assert component not in out

    def test_project_name_never_survives_a_home_path(self) -> None:
        out = debug.redact_path("/Users/bob/git/fujimoto")
        assert "fujimoto" not in out
        assert "git" not in out
        assert out.startswith("~/")

    def test_username_never_survives(self) -> None:
        assert "bob" not in debug.redact_path("/Users/bob/anything")
        assert "alice" not in debug.redact_path("/Users/alice/anything")

    def test_dotted_config_dirs_survive(self) -> None:
        assert debug.redact_path("/Users/bob/.fujimoto/logs").startswith("~/.fujimoto/")
        assert debug.redact_path("/Users/bob/.claude/projects") == (
            "~/.claude/projects"
        )

    def test_command_output_does_not_leak_a_project_path(self) -> None:
        stream = io.StringIO()
        logger = debug.DebugLogger(Path("/dev/null"), redact=True, stream=stream)
        logger.output("stdout", "/Users/bob/git/fujimoto")
        assert "fujimoto" not in stream.getvalue()
        assert "bob" not in stream.getvalue()

    def test_env_values_do_not_leak_a_project_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PWD", "/Users/bob/git/fujimoto")
        monkeypatch.setenv("FUJIMOTO_GIT_ROOT", "/Users/bob/git")
        monkeypatch.setattr(debug.shutil, "which", lambda name: None)
        logger = debug.enable(redact=True, log_dir=tmp_path)
        debug.log_environment()
        debug.disable()
        text = logger.path.read_text()
        assert "name=PWD" in text  # still logged, just redacted
        assert "/git/fujimoto" not in text
        assert "bob" not in text


class TestOwnedNames:
    """fujimoto's own directory names are constants, not user data.

    Hashing a constant is what re-opened the leak: a reader knows
    `~/.cache/<X>` is always "fujimoto", so that fingerprint could be matched
    wherever else it appeared — including the project name.
    """

    @pytest.fixture(autouse=True)
    def _home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))

    def test_cache_dir_is_readable(self) -> None:
        assert debug.redact_path("/Users/bob/.cache/fujimoto/sessions.json") == (
            "~/.cache/fujimoto/sessions.json"
        )

    def test_log_dir_and_log_name_are_readable(self) -> None:
        out = debug.redact_path(
            "/Users/bob/.fujimoto/logs/fujimoto-20260827-114659-59485.log"
        )
        assert out == "~/.fujimoto/logs/fujimoto-20260827-114659-59485.log"

    def test_claude_projects_dir_is_readable(self) -> None:
        assert debug.redact_path("/Users/bob/.claude/projects") == "~/.claude/projects"

    def test_same_name_outside_an_owned_parent_is_redacted(self) -> None:
        # The crux: a *repo* called fujimoto must still be hidden.
        out = debug.redact_path("/Users/bob/git/fujimoto")
        assert "fujimoto" not in out

    def test_owned_name_does_not_leak_the_project_fingerprint(
        self, tmp_path: Path
    ) -> None:
        logger = debug.enable(redact=True, log_dir=tmp_path)
        cache = debug.rp("/Users/bob/.cache/fujimoto/sessions.json")
        project = debug.rv("fujimoto")
        debug.disable()
        del logger
        # The cache path is readable, so it publishes no fingerprint that could
        # be matched against the redacted project name.
        assert cache == "~/.cache/fujimoto/sessions.json"
        assert project.startswith("[REDACTED-")
        fingerprint = project.split("-")[1]
        assert fingerprint not in cache

    def test_meta_json_under_a_redacted_worktree(self) -> None:
        out = debug.redact_path("/Users/bob/git/proj/.fujimoto/meta.json")
        assert out.endswith("/.fujimoto/meta.json")
        assert "proj" not in out


class TestRedactId:
    """A widget id's prefix says which row was involved; the tail can name a
    project."""

    @pytest.mark.parametrize(
        "item_id",
        ["action-create", "action-switch-project", "sa-fork", "sa-viewlog"],
    )
    def test_static_ids_survive_whole(self, item_id: str) -> None:
        assert debug.redact_id(item_id) == item_id

    @pytest.mark.parametrize(
        ("item_id", "prefix"),
        [
            ("ds-myproject--direct-1", "ds-"),
            ("cs-abc12345-def6-7890", "cs-"),
            ("fp-0", "fp-"),
        ],
    )
    def test_dynamic_ids_keep_only_the_prefix(self, item_id: str, prefix: str) -> None:
        out = debug.redact_id(item_id)
        assert out.startswith(prefix)
        assert "myproject" not in out
        assert "abc12345" not in out

    def test_prefixless_id_is_fully_redacted(self) -> None:
        assert debug.redact_id("weird_id").startswith("[REDACTED-")

    def test_rid_is_a_noop_when_not_redacting(self, tmp_path: Path) -> None:
        debug.enable(redact=False, log_dir=tmp_path)
        assert debug.rid("ds-myproject--direct-1") == "ds-myproject--direct-1"
        debug.disable()
        assert debug.rid("ds-myproject--direct-1") == "ds-myproject--direct-1"


class TestRedactRef:
    """`main` is every repo's; a branch someone named is not."""

    @pytest.mark.parametrize("ref", ["main", "master", "HEAD", "origin/main"])
    def test_git_vocabulary_survives(self, ref: str) -> None:
        assert debug.redact_ref(ref) == ref

    def test_named_branch_is_redacted(self) -> None:
        out = debug.redact_ref("worktree/20260827-thing")
        assert "20260827-thing" not in out

    def test_rref_is_a_noop_when_not_redacting(self, tmp_path: Path) -> None:
        debug.enable(redact=False, log_dir=tmp_path)
        assert debug.rref("worktree/thing") == "worktree/thing"
        debug.disable()
        assert debug.rref("worktree/thing") == "worktree/thing"

    def test_rref_redacts_when_enabled(self, tmp_path: Path) -> None:
        debug.enable(redact=True, log_dir=tmp_path)
        assert debug.rref("main") == "main"
        assert debug.rref("worktree/thing").startswith("[REDACTED-")
        debug.disable()


class TestGitRefArgs:
    """Git's own ref vocabulary stays readable in a command line."""

    @pytest.mark.parametrize(
        "ref",
        [
            "refs/remotes/origin/HEAD",
            "refs/heads/main",
            "origin/main",
            "HEAD",
            "main",
        ],
    )
    def test_refs_survive(self, ref: str) -> None:
        assert debug.redact_arg(ref) == ref

    @pytest.mark.parametrize(
        "ref", ["origin/my-feature", "worktree/20260827-thing", "refs/heads/secret"]
    )
    def test_user_branch_names_do_not(self, ref: str) -> None:
        out = debug.redact_arg(ref)
        assert "secret" not in out
        assert "my-feature" not in out
        assert "20260827-thing" not in out

    def test_ref_words_are_not_safe_as_directories(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A directory called `origin` is a name the user chose.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))
        assert "origin" not in debug.redact_path("/Users/bob/parent/origin")


class TestRedactArg:
    def test_empty(self) -> None:
        assert debug.redact_arg("") == ""

    @pytest.mark.parametrize(
        "arg", ["rev-parse", "worktree", "main", "master", "--show-toplevel", "-b"]
    )
    def test_kept_verbatim(self, arg: str) -> None:
        assert debug.redact_arg(arg) == arg

    def test_branch_name_redacted(self) -> None:
        out = debug.redact_arg("worktree/20260827-thing")
        assert "20260827-thing" not in out
        assert out.count("/") == 1

    def test_flag_with_path_value_keeps_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))
        out = debug.redact_arg("--working-directory=/Users/bob/proj")
        assert out.startswith("--working-directory=~/")
        assert "proj" not in out

    def test_tilde_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/bob")))
        assert debug.redact_arg("~") == "~"

    def test_uppercase_token_redacted(self) -> None:
        assert debug.redact_arg("HEAD~3").startswith("[REDACTED-")


class TestIsSecretName:
    @pytest.mark.parametrize(
        "name",
        [
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "MY_SECRET",
            "DB_PASSWORD",
            "AUTH_COOKIE",
        ],
    )
    def test_secrets(self, name: str) -> None:
        assert debug.is_secret_name(name)

    @pytest.mark.parametrize("name", ["HOME", "FUJIMOTO_GIT_ROOT", "TERM"])
    def test_non_secrets(self, name: str) -> None:
        assert not debug.is_secret_name(name)


class TestDebugLogger:
    def test_event_has_timestamp_and_fields(self) -> None:
        logger, stream = _logger()
        logger.event("thing.happened", count=2, name="abc")
        line = stream.getvalue().strip()
        assert line.endswith("thing.happened count=2 name=abc")
        assert line.startswith("20")

    def test_event_without_fields(self) -> None:
        logger, stream = _logger()
        logger.event("bare")
        assert stream.getvalue().strip().endswith("bare")

    def test_values_with_spaces_are_quoted(self) -> None:
        logger, stream = _logger()
        logger.event("e", detail="two words", empty="", missing=None)
        assert 'detail="two words"' in stream.getvalue()
        assert 'empty=""' in stream.getvalue()
        assert "missing=None" in stream.getvalue()

    def test_event_once_dedups_identical_payloads(self) -> None:
        logger, stream = _logger()
        logger.event_once("k", "state", value=1)
        logger.event_once("k", "state", value=1)
        logger.event_once("k", "state", value=2)
        assert stream.getvalue().count("state value=1") == 1
        assert stream.getvalue().count("state value=2") == 1

    def test_event_once_keys_are_independent(self) -> None:
        logger, stream = _logger()
        logger.event_once("a", "state", value=1)
        logger.event_once("b", "state", value=1)
        assert stream.getvalue().count("state value=1") == 2

    def test_section_and_raw(self) -> None:
        logger, stream = _logger()
        logger.section("environment")
        assert "===== environment " in stream.getvalue()

    def test_output_is_indented(self) -> None:
        logger, stream = _logger()
        logger.output("stdout", "line one\nline two")
        assert "    | line one" in stream.getvalue()
        assert "    | line two" in stream.getvalue()

    def test_output_skips_empty(self) -> None:
        logger, stream = _logger()
        logger.output("stdout", "")
        assert stream.getvalue() == ""

    def test_output_truncates(self) -> None:
        logger, stream = _logger()
        logger.output("stdout", "x" * (debug.MAX_OUTPUT_CHARS + 50))
        assert "…" in stream.getvalue()
        assert len(stream.getvalue()) < debug.MAX_OUTPUT_CHARS + 200

    def test_output_redacts_tokens_when_enabled(self) -> None:
        logger, stream = _logger(redact=True)
        logger.output("stdout", "branch worktree/20260827-thing")
        assert "20260827-thing" not in stream.getvalue()
        assert "branch" in stream.getvalue()

    def test_exception_includes_traceback(self) -> None:
        logger, stream = _logger()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            logger.exception("failed", exc)
        text = stream.getvalue()
        assert "failed exception=ValueError message=boom" in text
        assert "    ! Traceback" in text

    def test_redaction_helpers_off(self) -> None:
        logger, _ = _logger()
        assert logger.value("proj") == "proj"
        assert logger.path_value("/a/b") == "/a/b"
        assert logger.args(["git", "checkout", "worktree/x"]) == (
            "git checkout worktree/x"
        )

    def test_redaction_helpers_on(self) -> None:
        logger, _ = _logger(redact=True)
        assert logger.value("proj").startswith("[REDACTED-")
        assert "checkout" in logger.args(["checkout", "worktree/x"])
        assert "worktree/x" not in logger.args(["checkout", "worktree/x"])

    def test_secret_is_always_redacted(self) -> None:
        logger, _ = _logger()
        assert logger.secret("hunter2") == "[SECRET-7]"

    def test_write_after_close_is_silent(self) -> None:
        logger, _ = _logger()
        logger.close()
        logger.event("after")  # must not raise
        logger.raw("after")


class TestCappedSeries:
    """A repeating series logs its first N items, then says what it left out."""

    def test_first_n_are_logged_then_summarised(self) -> None:
        logger, stream = _logger()
        for i in range(25):
            logger.event_capped("thing", "thing", 10, item=i)
        logger.flush_series()
        text = stream.getvalue()
        assert text.count("thing item=") == 10
        assert "thing item=9" in text
        assert "thing item=10" not in text
        assert "series.summarised series=thing logged=10 not_logged=15 total=25" in text

    def test_return_value_reports_whether_it_wrote(self) -> None:
        logger, _ = _logger()
        assert logger.event_capped("s", "e", 2, item=1) is True
        assert logger.event_capped("s", "e", 2, item=2) is True
        assert logger.event_capped("s", "e", 2, item=3) is False

    def test_series_are_independent(self) -> None:
        logger, stream = _logger()
        for i in range(5):
            logger.event_capped("a", "a", 3, item=i)
            logger.event_capped("b", "b", 3, item=i)
        logger.flush_series()
        text = stream.getvalue()
        assert text.count("a item=") == 3
        assert text.count("b item=") == 3
        assert "series=a logged=3 not_logged=2" in text
        assert "series=b logged=3 not_logged=2" in text

    def test_under_the_limit_reports_nothing(self) -> None:
        logger, stream = _logger()
        for i in range(3):
            logger.event_capped("s", "e", 10, item=i)
        logger.flush_series()
        assert "series.summarised" not in stream.getvalue()

    def test_dedupe_key_does_not_spend_the_cap(self) -> None:
        # The home screen re-renders; re-reading the same subjects must not
        # push distinct ones past the cap.
        logger, stream = _logger()
        for _ in range(3):
            for i in range(4):
                logger.event_capped("s", "e", 3, f"key-{i}", item=i)
        logger.flush_series()
        text = stream.getvalue()
        assert text.count("e item=") == 3
        # 4 distinct subjects seen, not 12 calls.
        assert "logged=3 not_logged=1 total=4" in text

    def test_dedupe_key_still_logs_a_changed_payload(self) -> None:
        logger, stream = _logger()
        logger.event_capped("s", "e", 10, "key", state="idle")
        logger.event_capped("s", "e", 10, "key", state="idle")
        logger.event_capped("s", "e", 10, "key", state="working")
        assert stream.getvalue().count("e state=") == 2

    def test_close_flushes_the_summary(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=False, log_dir=tmp_path)
        for i in range(15):
            debug.log_capped("s", "e", limit=5, item=i)
        debug.disable()
        assert "series.summarised series=s logged=5 not_logged=10" in (
            logger.path.read_text()
        )

    def test_flush_is_idempotent(self) -> None:
        logger, stream = _logger()
        for i in range(5):
            logger.event_capped("s", "e", 1, item=i)
        logger.flush_series()
        logger.flush_series()
        assert stream.getvalue().count("series.summarised") == 1

    def test_log_capped_is_a_noop_when_disabled(self) -> None:
        assert debug.log_capped("s", "e", item=1) is False


class TestGlobalState:
    def test_disabled_helpers_are_noops(self) -> None:
        assert not debug.is_enabled()
        assert debug.logger() is None
        assert debug.log_path() is None
        debug.log("nothing")
        debug.log_once("k", "nothing")
        debug.log_section("nothing")
        debug.log_exception("nothing", ValueError("x"))
        debug.log_command("git", ["status"], returncode=0)
        debug.log_environment()
        assert debug.rv("proj") == "proj"
        assert debug.rp("/a/b") == "/a/b"

    def test_enable_creates_timestamped_file(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=False, log_dir=tmp_path / "logs")
        assert logger.path.parent == tmp_path / "logs"
        assert logger.path.name.startswith("fujimoto-")
        assert logger.path.suffix == ".log"
        assert debug.is_enabled()
        assert debug.log_path() == logger.path
        debug.disable()
        assert "fujimoto debug log" in logger.path.read_text()

    def test_enable_honours_log_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(debug.LOG_DIR_ENV, str(tmp_path / "envlogs"))
        logger = debug.enable(redact=False)
        assert logger.path.parent == tmp_path / "envlogs"

    def test_redacted_header_documents_token_format(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=True, log_dir=tmp_path)
        debug.disable()
        text = logger.path.read_text()
        assert "redaction: on" in text
        assert "redaction token:" in text

    def test_plain_header_omits_legend(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=False, log_dir=tmp_path)
        debug.disable()
        assert "redaction: off" in logger.path.read_text()
        assert "redaction token:" not in logger.path.read_text()

    def test_disable_is_idempotent(self) -> None:
        debug.disable()
        debug.disable()
        assert not debug.is_enabled()

    def test_enabled_helpers_write(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=False, log_dir=tmp_path)
        debug.log("evt", a=1)
        debug.log_once("k", "once", a=1)
        debug.log_once("k", "once", a=1)
        debug.log_section("sect")
        debug.log_exception("oops", ValueError("bad"))
        debug.disable()
        text = logger.path.read_text()
        assert "evt a=1" in text
        assert text.count("once a=1") == 1
        assert "===== sect" in text
        assert "oops exception=ValueError message=bad" in text

    def test_rv_and_rp_follow_redaction_mode(self, tmp_path: Path) -> None:
        debug.enable(redact=True, log_dir=tmp_path)
        assert debug.rv("proj").startswith("[REDACTED-")
        assert debug.rp("/Users/alice") != "/Users/alice"


class TestLogCommand:
    def test_logs_command_and_output(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=False, log_dir=tmp_path)
        debug.log_command(
            "git",
            ["status", "--short"],
            cwd=tmp_path,
            returncode=0,
            stdout="M file.py",
            stderr="warning",
        )
        debug.disable()
        text = logger.path.read_text()
        assert 'run cmd="git status --short"' in text
        assert "rc=0" in text
        assert "    | M file.py" in text
        assert "    | warning" in text

    def test_omits_cwd_and_rc_when_unknown(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=False, log_dir=tmp_path)
        debug.log_command("tmux", ["list-sessions"])
        debug.disable()
        text = logger.path.read_text()
        assert "cwd=" not in text.split("run ", 1)[1].splitlines()[0]
        assert "rc=" not in text.split("run ", 1)[1].splitlines()[0]

    def test_redacts_arguments(self, tmp_path: Path) -> None:
        logger = debug.enable(redact=True, log_dir=tmp_path)
        debug.log_command("git", ["checkout", "worktree/20260827-thing"])
        debug.disable()
        text = logger.path.read_text()
        assert "20260827-thing" not in text
        assert "checkout" in text


class TestLogEnvironment:
    def test_records_versions_env_and_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FUJIMOTO_GIT_ROOT", "/Users/bob/git")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
        monkeypatch.setenv("FUJIMOTO_META_KEY", "C-a")
        monkeypatch.delenv("FUJIMOTO_WORKTREE_ROOT", raising=False)
        monkeypatch.setattr(debug.shutil, "which", lambda name: None)
        logger = debug.enable(redact=False, log_dir=tmp_path)
        debug.log_environment()
        debug.disable()
        text = logger.path.read_text()
        assert "===== fujimoto / system" in text
        assert "===== tool versions" in text
        assert "===== environment" in text
        assert 'name=tmux detail="not found on PATH"' in text
        assert "name=FUJIMOTO_GIT_ROOT" in text
        assert "sk-secret-value" not in text
        assert "[SECRET-" in text
        assert "name=PATH entries=" in text

    def test_unset_interesting_vars_are_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setattr(debug.shutil, "which", lambda name: None)
        logger = debug.enable(redact=False, log_dir=tmp_path)
        debug.log_environment()
        debug.disable()
        assert "name=TMUX value=[unset]" in logger.path.read_text()

    def test_redaction_hides_env_values_and_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FUJIMOTO_GIT_ROOT", "/Users/alice/mycode")
        monkeypatch.setattr(debug.shutil, "which", lambda name: None)
        logger = debug.enable(redact=True, log_dir=tmp_path)
        debug.log_environment()
        debug.disable()
        text = logger.path.read_text()
        assert "mycode" not in text
        assert "alice" not in text

    def test_tool_version_uses_command_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Result:
            stdout = "tmux 3.5a\n"
            stderr = ""

        monkeypatch.setattr(debug.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(debug.subprocess, "run", lambda *a, **k: _Result())
        logger = debug.enable(redact=False, log_dir=tmp_path)
        debug.log_environment()
        debug.disable()
        assert "tmux 3.5a (/usr/bin/tmux)" in logger.path.read_text()

    def test_tool_without_version_args_reports_location(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(debug.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            debug.subprocess,
            "run",
            lambda *a, **k: pytest.fail("should not run for `code`"),
        )
        assert debug._tool_version("code") == "found at /usr/bin/code"


class TestInstrumentation:
    """Smoke tests that call sites actually reach the log."""

    def test_claude_discovery_is_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fujimoto.claude import log_parser

        monkeypatch.setattr(
            log_parser, "get_claude_projects_dir", lambda: tmp_path / "projects"
        )
        logger = debug.enable(redact=False, log_dir=tmp_path)
        assert log_parser.get_sessions_for_path(Path("/Users/bob/proj")) == []
        debug.disable()
        assert "claude.discovery" in logger.path.read_text()

    def test_git_commands_are_logged(self, tmp_path: Path) -> None:
        import subprocess

        from fujimoto import git

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        logger = debug.enable(redact=False, log_dir=tmp_path)
        git.get_current_branch(cwd=tmp_path)
        debug.disable()
        assert 'run cmd="git branch --show-current"' in logger.path.read_text()

    def test_failed_git_command_is_logged(self, tmp_path: Path) -> None:
        from fujimoto import git

        logger = debug.enable(redact=False, log_dir=tmp_path)
        with pytest.raises(git.GitError):
            git.get_repo_root(cwd=tmp_path / "nope")
        debug.disable()
        assert "run cmd=" in logger.path.read_text()


class TestSessionStateInstrumentation:
    """`session_state` silently drops records; the log must say so."""

    @pytest.fixture
    def _state(self, tmp_path: Path):
        from unittest.mock import patch

        with patch("pathlib.Path.home", return_value=tmp_path):
            yield tmp_path / ".cache" / "fujimoto" / "sessions.json"

    def _log_text(self, log_dir: Path) -> str:
        return next(log_dir.glob("*.log")).read_text()

    def test_load_logs_records_and_skips(self, tmp_path: Path, _state: Path) -> None:
        import json

        from fujimoto import session_state

        _state.parent.mkdir(parents=True)
        _state.write_text(
            json.dumps(
                {
                    "proj/good": {"cwd": "/tmp/good", "project": "proj"},
                    "proj/bad": {"no_cwd": True},
                }
            )
        )
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert set(session_state.load_state()) == {"proj/good"}
        debug.disable()
        text = self._log_text(log_dir)
        assert "session_state.skipped session=proj/bad" in text
        assert "records=1 skipped=1" in text

    def test_load_logs_missing_file(self, tmp_path: Path, _state: Path) -> None:
        from fujimoto import session_state

        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert session_state.load_state() == {}
        debug.disable()
        assert "session_state.load" in self._log_text(log_dir)
        assert "found=False" in self._log_text(log_dir)

    def test_load_logs_corrupt_file(self, tmp_path: Path, _state: Path) -> None:
        from fujimoto import session_state

        _state.parent.mkdir(parents=True)
        _state.write_text("{not json")
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert session_state.load_state() == {}
        debug.disable()
        assert "error=JSONDecodeError" in self._log_text(log_dir)

    def test_load_logs_non_dict_payload(self, tmp_path: Path, _state: Path) -> None:
        from fujimoto import session_state

        _state.parent.mkdir(parents=True)
        _state.write_text("[]")
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert session_state.load_state() == {}
        debug.disable()
        assert "error=not-an-object" in self._log_text(log_dir)

    def test_mark_open_and_closed_are_logged(
        self, tmp_path: Path, _state: Path
    ) -> None:
        from fujimoto import session_state

        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        session_state.mark_open(
            "proj/wt",
            cwd=tmp_path / "wt",
            project="proj",
            session_type="worktree",
            branch="worktree/thing",
        )
        session_state.mark_closed("proj/wt")
        session_state.mark_closed("proj/ghost")
        debug.disable()
        text = self._log_text(log_dir)
        assert "session_state.mark_open session=proj/wt" in text
        assert "new_record=True" in text
        assert "session_state.mark_closed session=proj/wt removed=True" in text
        assert "session_state.mark_closed session=proj/ghost removed=False" in text
        assert "session_state.save" in text

    def test_touch_and_rename_are_logged(self, tmp_path: Path, _state: Path) -> None:
        from fujimoto import session_state

        session_state.mark_open(
            "proj/wt", cwd=tmp_path / "wt", project="proj", session_type="worktree"
        )
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        session_state.touch("proj/wt", claude_session_id="abc123")
        session_state.touch("proj/ghost")
        session_state.rename("proj/wt", "proj/renamed")
        session_state.rename("proj/ghost", "proj/nope")
        debug.disable()
        text = self._log_text(log_dir)
        assert "session_state.touch session=proj/wt found=True" in text
        assert "claude_session=abc123" in text
        assert "session_state.touch session=proj/ghost found=False" in text
        assert "old=proj/wt new=proj/renamed found=True" in text
        assert "old=proj/ghost new=proj/nope found=False" in text

    def test_prune_logs_each_dropped_record(self, tmp_path: Path, _state: Path) -> None:
        from fujimoto import session_state

        live = tmp_path / "live"
        live.mkdir()
        session_state.mark_open(
            "proj/live", cwd=live, project="proj", session_type="worktree"
        )
        session_state.mark_open(
            "proj/gone",
            cwd=tmp_path / "gone",
            project="proj",
            session_type="worktree",
        )
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert set(session_state.prune()) == {"proj/live"}
        debug.disable()
        text = self._log_text(log_dir)
        assert "session_state.pruned session=proj/gone" in text
        assert "records=2 live=1 dropped=1" in text
        assert "session_state.pruned session=proj/live" not in text

    def test_redaction_hides_session_names(self, tmp_path: Path, _state: Path) -> None:
        from fujimoto import session_state

        log_dir = tmp_path / "logs"
        debug.enable(redact=True, log_dir=log_dir)
        session_state.mark_open(
            "secretproj/wt",
            cwd=tmp_path / "wt",
            project="secretproj",
            session_type="worktree",
            branch="worktree/secretbranch",
        )
        debug.disable()
        text = self._log_text(log_dir)
        assert "secretproj" not in text
        assert "secretbranch" not in text
        assert "[REDACTED-" in text


class TestSearchInstrumentation:
    """A search that finds nothing must be distinguishable from one that
    scanned nothing."""

    def _log_text(self, log_dir: Path) -> str:
        return next(log_dir.glob("*.log")).read_text()

    def _log(self, tmp_path: Path, name: str, body: str) -> Path:
        session_dir = tmp_path / "projects" / name
        session_dir.mkdir(parents=True, exist_ok=True)
        log = session_dir / "11111111-2222-3333-4444-555555555555.jsonl"
        log.write_text(body)
        return log

    def test_compile_logs_query_shape(self, tmp_path: Path) -> None:
        from fujimoto.claude import search

        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        search.compile_matcher("needle", mode=search.ContentMode.TEXT)
        debug.disable()
        text = self._log_text(log_dir)
        assert "search.compile query=needle chars=6" in text
        assert "mode=text" in text

    def test_compile_logs_bad_regex(self, tmp_path: Path) -> None:
        from fujimoto.claude import search

        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        with pytest.raises(search.SearchError):
            search.compile_matcher("(unclosed", regex=True)
        debug.disable()
        assert "search.compile" in self._log_text(log_dir)
        assert "error=" in self._log_text(log_dir)

    def test_compile_redacts_query(self, tmp_path: Path) -> None:
        from fujimoto.claude import search

        log_dir = tmp_path / "logs"
        debug.enable(redact=True, log_dir=log_dir)
        search.compile_matcher("my-secret-term")
        debug.disable()
        text = self._log_text(log_dir)
        assert "my-secret-term" not in text
        assert "chars=14" in text

    def test_scan_logs_counts(self, tmp_path: Path, monkeypatch) -> None:
        from fujimoto.claude import log_parser, search

        entry = {
            "type": "user",
            "cwd": "/tmp/proj",
            "timestamp": "2026-08-27T10:00:00Z",
            "message": {"role": "user", "content": "find the needle here"},
        }
        import json as _json

        hit = self._log(tmp_path, "-tmp-proj", _json.dumps(entry) + "\n")
        monkeypatch.setattr(
            log_parser, "get_claude_projects_dir", lambda: tmp_path / "projects"
        )
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        matcher = search.compile_matcher("needle")
        batches = list(search.iter_hits([hit], matcher))
        debug.disable()
        assert sum(len(b) for _, b in batches) == 1
        text = self._log_text(log_dir)
        assert "search.scan phase=start logs=1" in text
        assert "search.scan phase=done logs=1 scanned=1 hits=1" in text

    def test_empty_log_list_is_visible(self, tmp_path: Path) -> None:
        from fujimoto.claude import search

        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        list(search.iter_hits([], search.compile_matcher("x")))
        debug.disable()
        assert "search.scan phase=start logs=0" in self._log_text(log_dir)

    def test_unreadable_log_is_logged(self, tmp_path: Path) -> None:
        from fujimoto.claude import search

        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        missing = tmp_path / "gone" / "nope.jsonl"
        assert search.search_log(missing, search.compile_matcher("x")) is None
        debug.disable()
        assert "search.log_unreadable" in self._log_text(log_dir)

    def test_discarded_hit_is_logged(self, tmp_path: Path) -> None:
        from fujimoto.claude import search

        # Matches the query but has no parseable entries, so the hit is dropped.
        log = self._log(tmp_path, "-tmp-proj", "needle but not json\n")
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert search.search_log(log, search.compile_matcher("needle")) is None
        debug.disable()
        assert "search.hit_discarded" in self._log_text(log_dir)

    def test_available_logs_are_counted(self, tmp_path: Path, monkeypatch) -> None:
        from fujimoto.claude import log_parser, search

        self._log(tmp_path, "-tmp-proj", "{}\n")
        monkeypatch.setattr(
            log_parser, "get_claude_projects_dir", lambda: tmp_path / "projects"
        )
        monkeypatch.setattr(
            search, "session_dirs_for_path", log_parser.session_dirs_for_path
        )
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        search.list_session_logs(Path("/tmp/proj"), [])
        debug.disable()
        assert "search.logs targets=1" in self._log_text(log_dir)


class TestDiscoveryCapping:
    """The routine outcome is capped; the interesting one is not."""

    def _log_text(self, log_dir: Path) -> str:
        return next(log_dir.glob("*.log")).read_text()

    def test_missing_paths_are_capped(self, tmp_path: Path, monkeypatch) -> None:
        from fujimoto.claude import log_parser

        projects = tmp_path / "projects"
        projects.mkdir(parents=True)
        monkeypatch.setattr(log_parser, "get_claude_projects_dir", lambda: projects)
        monkeypatch.setattr(log_parser, "_cwd_index_cache", None)
        log_dir = tmp_path / "logs"
        logger = debug.enable(redact=False, log_dir=log_dir)
        for i in range(20):
            log_parser.session_dirs_for_path(Path(f"/tmp/nope-{i}"))
        debug.disable()
        text = logger.path.read_text()
        assert text.count("via=none") == 5
        assert "series.summarised series=claude.session_dirs.missing" in text
        assert "logged=5 not_logged=15 total=20" in text

    def test_resolved_paths_have_their_own_budget(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from fujimoto.claude import log_parser

        projects = tmp_path / "projects"
        projects.mkdir(parents=True)
        for i in range(20):
            (projects / f"-tmp-yes-{i}").mkdir()
        monkeypatch.setattr(log_parser, "get_claude_projects_dir", lambda: projects)
        log_dir = tmp_path / "logs"
        logger = debug.enable(redact=False, log_dir=log_dir)
        for i in range(20):
            log_parser.session_dirs_for_path(Path(f"/tmp/nope-{i}"))
            log_parser.session_dirs_for_path(Path(f"/tmp/yes-{i}"))
        debug.disable()
        text = logger.path.read_text()
        # The 15 suppressed misses must not have eaten the resolved budget.
        assert text.count("via=encoded-name") == debug.DEFAULT_SERIES_CAP
        assert text.count("via=none") == 5

    def test_unparseable_logs_are_never_capped(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from fujimoto.claude import log_parser

        projects = tmp_path / "projects"
        # 15 directories, each holding one log that cannot be parsed.
        for i in range(15):
            d = projects / f"-tmp-broken-{i}"
            d.mkdir(parents=True)
            (d / "a.jsonl").write_text("")
        monkeypatch.setattr(log_parser, "get_claude_projects_dir", lambda: projects)
        log_dir = tmp_path / "logs"
        logger = debug.enable(redact=False, log_dir=log_dir)
        for i in range(15):
            log_parser.get_sessions_for_path(Path(f"/tmp/broken-{i}"))
        debug.disable()
        text = logger.path.read_text()
        assert text.count("failed=1") == 15

    def test_found_discoveries_are_capped(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        from fujimoto.claude import log_parser

        projects = tmp_path / "projects"
        entry = _json.dumps(
            {
                "type": "user",
                "cwd": "/tmp/p",
                "timestamp": "2026-08-27T10:00:00Z",
                "message": {"role": "user", "content": "hi"},
            }
        )
        for i in range(20):
            d = projects / f"-tmp-good-{i}"
            d.mkdir(parents=True)
            (
                d / "11111111-2222-3333-4444-55555555555{}.jsonl".format(i % 10)
            ).write_text(entry + "\n")
        monkeypatch.setattr(log_parser, "get_claude_projects_dir", lambda: projects)
        log_dir = tmp_path / "logs"
        logger = debug.enable(redact=False, log_dir=log_dir)
        for i in range(20):
            log_parser.get_sessions_for_path(Path(f"/tmp/good-{i}"))
        debug.disable()
        text = logger.path.read_text()
        assert text.count("claude.discovery path=") == debug.DEFAULT_SERIES_CAP
        assert "series.summarised series=claude.discovery.found" in text


class TestSessionDirLookupInstrumentation:
    """Which lookup strategy resolved a path is the diagnostic value."""

    def _log_text(self, log_dir: Path) -> str:
        return next(log_dir.glob("*.log")).read_text()

    def test_encoded_name_strategy_is_logged(self, tmp_path: Path, monkeypatch) -> None:
        from fujimoto.claude import log_parser

        projects = tmp_path / "projects"
        (projects / "-tmp-proj").mkdir(parents=True)
        monkeypatch.setattr(log_parser, "get_claude_projects_dir", lambda: projects)
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert log_parser.session_dirs_for_path(Path("/tmp/proj"))
        debug.disable()
        assert "claude.session_dirs" in self._log_text(log_dir)
        assert "via=encoded-name dirs=1" in self._log_text(log_dir)

    def test_cwd_index_strategy_is_logged(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        from fujimoto.claude import log_parser

        # A directory whose encoded name does not match the path, so only the
        # recorded-cwd index can find it.
        projects = tmp_path / "projects"
        odd = projects / "-tmp-mangled-name"
        odd.mkdir(parents=True)
        (odd / "a.jsonl").write_text(
            _json.dumps({"type": "user", "cwd": "/tmp/real.proj"}) + "\n"
        )
        monkeypatch.setattr(log_parser, "get_claude_projects_dir", lambda: projects)
        monkeypatch.setattr(log_parser, "_cwd_index_cache", None)
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert log_parser.session_dirs_for_path(Path("/tmp/real.proj"))
        debug.disable()
        text = self._log_text(log_dir)
        assert "via=cwd-index dirs=1" in text
        assert "claude.cwd_index" in text

    def test_failed_lookup_logs_encoded_name(self, tmp_path: Path, monkeypatch) -> None:
        from fujimoto.claude import log_parser

        projects = tmp_path / "projects"
        projects.mkdir(parents=True)
        monkeypatch.setattr(log_parser, "get_claude_projects_dir", lambda: projects)
        monkeypatch.setattr(log_parser, "_cwd_index_cache", None)
        log_dir = tmp_path / "logs"
        debug.enable(redact=False, log_dir=log_dir)
        assert log_parser.session_dirs_for_path(Path("/tmp/nope")) == []
        debug.disable()
        text = self._log_text(log_dir)
        assert "via=none dirs=0" in text
        assert "encoded=-tmp-nope" in text
