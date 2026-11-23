from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fujimoto import broadcast, debug
from fujimoto.broadcast import BroadcastError, Payload
from fujimoto.broadcast import BroadcastSettings, Signer

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="needs OpenSSH ssh-keygen"
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h / ".claude"))
    yield h
    debug.disable()


def _keypair(dir_: Path, name: str) -> tuple[Path, str]:
    path = dir_ / name
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            f"{name}@test",
            "-f",
            str(path),
        ],
        check=True,
    )
    return path, broadcast.normalize_public_key(path.with_suffix(".pub").read_text())


@pytest.fixture
def alice(tmp_path: Path) -> tuple[Path, Signer]:
    path, pub = _keypair(tmp_path, "alice")
    return path, Signer("alice", pub, "user")


@pytest.fixture
def mallory(tmp_path: Path) -> tuple[Path, Signer]:
    path, pub = _keypair(tmp_path, "mallory")
    return path, Signer("mallory", pub)


def _signed(key: Path, signers: list[Signer], payload: Payload) -> tuple[bytes, bytes]:
    data = payload.to_bytes()
    return data, broadcast.sign(data, str(key), signers)


def _payload(
    message: str = "hello",
    project: str | None = None,
    ttl: timedelta = timedelta(hours=1),
) -> Payload:
    return broadcast.new_payload(message, project, ttl)


def _use_config(monkeypatch: pytest.MonkeyPatch, config: BroadcastSettings) -> None:
    monkeypatch.setattr(broadcast, "load_config", lambda **kw: config)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


class TestPayload:
    def test_roundtrip(self) -> None:
        p = broadcast.new_payload("  hi there  ", "proj", now=NOW)
        q = Payload.from_bytes(p.to_bytes())
        assert q == p
        assert q.message == "hi there"
        assert q.expires - q.created == broadcast.DEFAULT_TTL

    def test_ids_are_unique(self) -> None:
        assert _payload().id != _payload().id

    @pytest.mark.parametrize(
        "message", ["", "   ", "x" * (broadcast.MAX_MESSAGE_CHARS + 1)]
    )
    def test_new_payload_rejects_bad_message(self, message: str) -> None:
        with pytest.raises(BroadcastError):
            broadcast.new_payload(message, None)

    @pytest.mark.parametrize(
        "raw",
        [
            b"not json",
            b"[]",
            b'{"v": 2}',
            b'{"v":1,"id":"a b","project":null,"created":"2026-11-20T12:00:00+00:00","expires":"2026-11-20T13:00:00+00:00","message":"m"}',
            b'{"v":1,"id":"a","project":3,"created":"2026-11-20T12:00:00+00:00","expires":"2026-11-20T13:00:00+00:00","message":"m"}',
            b'{"v":1,"id":"a","project":null,"created":"2026-11-20T12:00:00+00:00","expires":"2026-11-20T13:00:00+00:00","message":""}',
            b'{"v":1,"id":"a","project":null,"created":"nope","expires":"2026-11-20T13:00:00+00:00","message":"m"}',
            b'{"v":1,"id":"a","project":null,"created":"2026-11-20T12:00:00","expires":"2026-11-20T13:00:00","message":"m"}',
        ],
    )
    def test_from_bytes_rejects(self, raw: bytes) -> None:
        with pytest.raises(BroadcastError):
            Payload.from_bytes(raw)

    def test_from_bytes_rejects_long_message(self) -> None:
        p = Payload("a", None, NOW, NOW + timedelta(hours=1), "x")
        raw = json.loads(p.to_bytes())
        raw["message"] = "x" * (broadcast.MAX_MESSAGE_CHARS + 1)
        with pytest.raises(BroadcastError, match="too long"):
            Payload.from_bytes(json.dumps(raw).encode())

    @pytest.mark.parametrize(
        ("created", "expires", "now", "error"),
        [
            (NOW, NOW + timedelta(hours=1), NOW, None),
            (NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=1), "expired"),
            (NOW, NOW, NOW, "lifetime"),
            (NOW, NOW + timedelta(hours=25), NOW, "lifetime"),
            (NOW + timedelta(hours=1), NOW + timedelta(hours=2), NOW, "future"),
        ],
    )
    def test_check_time(
        self, created: datetime, expires: datetime, now: datetime, error: str | None
    ) -> None:
        p = Payload("a", None, created, expires, "m")
        if error is None:
            p.check_time(now)
        else:
            with pytest.raises(BroadcastError, match=error):
                p.check_time(now)

    def test_scope(self) -> None:
        everyone = Payload("a", None, NOW, NOW, "m")
        one = Payload("a", "proj", NOW, NOW, "m")
        assert everyone.covers("x") and everyone.covers(None)
        assert one.covers("proj") and not one.covers("other") and not one.covers(None)
        assert everyone.scope_label == "all projects"
        assert one.scope_label == "project proj"


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("90m", 5400), ("1h", 3600), ("1d", 86400), (" 2H ", 7200)],
)
def test_parse_ttl(text: str, seconds: int) -> None:
    assert broadcast.parse_ttl(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", "1", "1w", "0s", "25h", "-1h"])
def test_parse_ttl_rejects(text: str) -> None:
    with pytest.raises(BroadcastError):
        broadcast.parse_ttl(text)


# ---------------------------------------------------------------------------
# Keys and signers
# ---------------------------------------------------------------------------


class TestKeys:
    def test_normalize_literal_drops_comment(self, alice: tuple[Path, Signer]) -> None:
        _, signer = alice
        assert (
            broadcast.normalize_public_key(signer.public_key + " me@host")
            == signer.public_key
        )

    def test_normalize_path(self, alice: tuple[Path, Signer]) -> None:
        path, signer = alice
        assert (
            broadcast.normalize_public_key(str(path.with_suffix(".pub")))
            == signer.public_key
        )

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "ssh-dss AAAA",
            "ssh-ed25519",
            "ssh-ed25519 !!!notbase64",
            "/no/such/file.pub",
        ],
    )
    def test_normalize_rejects(self, text: str) -> None:
        with pytest.raises(BroadcastError):
            broadcast.normalize_public_key(text)

    @pytest.mark.parametrize("name", ["laptop", "remote-agent", "jon@mac", "a.b_c-1"])
    def test_signer_name_ok(self, name: str) -> None:
        assert broadcast.validate_signer_name(name) == name

    @pytest.mark.parametrize("name", ["", "-x", "has space", 'q"uote', "x" * 65])
    def test_signer_name_rejected(self, name: str) -> None:
        with pytest.raises(BroadcastError):
            broadcast.validate_signer_name(name)

    def test_encode_decode_signers(
        self, alice: tuple[Path, Signer], mallory: tuple[Path, Signer]
    ) -> None:
        signers = [alice[1], mallory[1]]
        token = broadcast.encode_signers(signers)
        assert " " not in token
        assert broadcast.decode_signers(token) == signers

    @pytest.mark.parametrize("token", ["!!", "e30=", "W1siYSIsICJ4Il1d"])
    def test_decode_signers_rejects(self, token: str) -> None:
        with pytest.raises(BroadcastError):
            broadcast.decode_signers(token)

    def test_allowed_signers_is_namespaced(self, alice: tuple[Path, Signer]) -> None:
        line = broadcast.allowed_signers_text([alice[1]])
        assert line == f'alice namespaces="fujimoto-broadcast" {alice[1].public_key}\n'


class TestSignVerify:
    def test_roundtrip_names_the_signer(
        self, alice: tuple[Path, Signer], mallory: tuple[Path, Signer]
    ) -> None:
        key, signer = alice
        data, sig = _signed(key, [signer], _payload())
        assert broadcast.verify(data, sig, [mallory[1], signer]) == "alice"

    def test_unknown_signer_rejected(
        self, alice: tuple[Path, Signer], mallory: tuple[Path, Signer]
    ) -> None:
        data, sig = _signed(mallory[0], [mallory[1]], _payload())
        with pytest.raises(BroadcastError, match="not signed by a configured signer"):
            broadcast.verify(data, sig, [alice[1]])

    def test_tampered_rejected(self, alice: tuple[Path, Signer]) -> None:
        data, sig = _signed(alice[0], [alice[1]], _payload("safe"))
        with pytest.raises(BroadcastError, match="does not match"):
            broadcast.verify(data.replace(b"safe", b"evil"), sig, [alice[1]])

    def test_other_namespace_rejected(
        self, alice: tuple[Path, Signer], tmp_path: Path
    ) -> None:
        f = tmp_path / "p.json"
        f.write_bytes(_payload().to_bytes())
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-Y",
                "sign",
                "-f",
                str(alice[0]),
                "-n",
                "git",
                str(f),
            ],
            check=True,
            capture_output=True,
        )
        with pytest.raises(BroadcastError):
            broadcast.verify(f.read_bytes(), Path(f"{f}.sig").read_bytes(), [alice[1]])

    def test_verify_needs_signers(self) -> None:
        with pytest.raises(BroadcastError, match="no signers"):
            broadcast.verify(b"x", b"y", [])

    def test_sign_with_literal_public_key_uses_agent(
        self, alice: tuple[Path, Signer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake(args: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
            calls.append(args)
            keyfile = Path(args[args.index("-f") + 1])
            assert keyfile.read_text().strip() == alice[1].public_key
            Path(args[-1] + ".sig").write_bytes(b"SIG")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(broadcast, "_ssh_keygen", fake)
        assert broadcast.sign(b"data", alice[1].public_key, [alice[1]]) == b"SIG"

    def test_sign_picks_agent_key_matching_a_signer(
        self,
        alice: tuple[Path, Signer],
        mallory: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            broadcast,
            "_agent_keys",
            lambda: [mallory[1].public_key, alice[1].public_key],
        )
        path = broadcast.resolve_signing_key(None, [alice[1]], tmp_path)
        assert path.read_text().strip() == alice[1].public_key

    def test_sign_without_matching_agent_key(
        self, alice: tuple[Path, Signer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(broadcast, "_agent_keys", lambda: [])
        with pytest.raises(BroadcastError, match="no signing key"):
            broadcast.sign(b"data", None, [alice[1]])

    def test_sign_failure_reports_stderr(
        self, alice: tuple[Path, Signer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            broadcast,
            "_ssh_keygen",
            lambda args, **kw: subprocess.CompletedProcess(
                args, 255, b"", b"agent refused operation"
            ),
        )
        with pytest.raises(BroadcastError, match="agent refused"):
            broadcast.sign(b"data", str(alice[0]), [alice[1]])

    def test_missing_ssh_keygen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: object, **kw: object) -> None:
            raise FileNotFoundError

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(BroadcastError, match="ssh-keygen not found"):
            broadcast._ssh_keygen(["-Y", "verify"])
        assert broadcast._agent_keys() == []

    def test_agent_keys_parses_and_skips(
        self, alice: tuple[Path, Signer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = f"{alice[1].public_key} comment\nThe agent has no identities.\n"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a, 0, out, ""),
        )
        assert broadcast._agent_keys() == [alice[1].public_key]


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------


class TestMailbox:
    def test_post_writes_private_pair(self, alice: tuple[Path, Signer]) -> None:
        data, sig = _signed(alice[0], [alice[1]], _payload())
        entry = broadcast.post(data, sig)
        assert entry.path.read_bytes() == data
        assert entry.sig_path.read_bytes() == sig
        assert entry.path.stat().st_mode & 0o777 == 0o600
        assert broadcast.messages_dir().stat().st_mode & 0o777 == 0o700
        assert broadcast.list_entries() == [entry]

    def test_duplicate_id_refused(self, alice: tuple[Path, Signer]) -> None:
        data, sig = _signed(alice[0], [alice[1]], _payload())
        broadcast.post(data, sig)
        with pytest.raises(BroadcastError, match="already"):
            broadcast.post(data, sig)

    def test_list_entries_ignores_temp_and_sig(
        self, alice: tuple[Path, Signer]
    ) -> None:
        broadcast.ensure_dirs()
        (broadcast.messages_dir() / ".x.json.tmp").write_text("{}")
        (broadcast.messages_dir() / ".y.json").write_text("{}")
        assert broadcast.list_entries() == []

    def test_list_entries_without_mailbox(self) -> None:
        assert broadcast.list_entries() == []

    def test_read_private_file_refuses_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "t"
        target.write_text("x")
        link = tmp_path / "l"
        link.symlink_to(target)
        with pytest.raises(OSError):
            broadcast._read_private_file(link)

    def test_read_private_file_refuses_large_and_dirs(self, tmp_path: Path) -> None:
        big = tmp_path / "big"
        big.write_bytes(b"x" * (broadcast.MAX_FILE_BYTES + 1))
        with pytest.raises(BroadcastError, match="too large"):
            broadcast._read_private_file(big)
        with pytest.raises(BroadcastError, match="regular"):
            broadcast._read_private_file(Path("/dev/null"))

    def test_private_dir_check(self, tmp_path: Path) -> None:
        d = tmp_path / "d"
        d.mkdir(mode=0o700)
        assert broadcast._private_dir_ok(d)
        os.chmod(d, 0o777)
        assert not broadcast._private_dir_ok(d)
        assert not broadcast._private_dir_ok(tmp_path / "missing")

    def test_prune_removes_long_expired(self, alice: tuple[Path, Signer]) -> None:
        data, sig = _signed(alice[0], [alice[1]], _payload())
        entry = broadcast.post(data, sig)
        receipt_dir = broadcast.receipts_dir() / entry.name
        receipt_dir.mkdir(parents=True)
        (receipt_dir / "s.json").write_text("{}")
        assert broadcast.prune(datetime.now(timezone.utc)) == 0
        later = datetime.now(timezone.utc) + timedelta(days=9)
        assert broadcast.prune(later) == 1
        assert broadcast.list_entries() == []
        assert not receipt_dir.exists()

    def test_prune_unreadable_by_age(self) -> None:
        broadcast.ensure_dirs()
        bad = broadcast.messages_dir() / "1-bad.json"
        bad.write_text("garbage")
        old = time.time() - 9 * 86400
        os.utime(bad, (old, old))
        assert broadcast.prune() == 1


# ---------------------------------------------------------------------------
# Waiter
# ---------------------------------------------------------------------------


HOOK = {"session_id": "sess-1", "hook_event_name": "Stop", "cwd": "/w/proj"}


def _wait_posting(
    signers: list[Signer],
    project: str | None,
    post: list[tuple[bytes, bytes]],
    hook: dict | None = None,
    polls: int = 3,
) -> tuple[int, str]:
    """Run the waiter; post the given messages on its first sleep."""
    pending = list(post)

    def sleep(_: float) -> None:
        while pending:
            broadcast.post(*pending.pop(0))

    return broadcast.wait(hook or HOOK, signers, project, sleep=sleep, max_polls=polls)


class TestWait:
    def test_delivers_signed_message(self, alice: tuple[Path, Signer]) -> None:
        msg = _signed(alice[0], [alice[1]], _payload("summarise to Slack", "proj"))
        code, text = _wait_posting([alice[1]], "proj", [msg])
        assert code == broadcast.EXIT_DELIVER
        assert text.startswith(
            "Broadcast from the user (via fujimoto, signed by alice, id "
        )
        assert "next safe stopping point" in text
        assert text.endswith("summarise to Slack")
        (row,) = broadcast.status()
        assert [(r.outcome, r.session_id, r.cwd) for r in row.receipts] == [
            ("delivered", "sess-1", "/w/proj")
        ]
        assert broadcast.list_listeners() == []  # lock released

    def test_several_messages_in_one_wake(self, alice: tuple[Path, Signer]) -> None:
        msgs = [_signed(alice[0], [alice[1]], _payload(m)) for m in ("one", "two")]
        code, text = _wait_posting([alice[1]], "proj", msgs)
        assert code == 2 and "one" in text and "two" in text and "---" in text

    def test_out_of_scope_ignored_silently(self, alice: tuple[Path, Signer]) -> None:
        msg = _signed(alice[0], [alice[1]], _payload("x", "other"))
        assert _wait_posting([alice[1]], "proj", [msg]) == (0, "")
        assert broadcast.status()[0].receipts == []

    def test_forged_message_rejected_with_receipt(
        self, alice: tuple[Path, Signer], mallory: tuple[Path, Signer]
    ) -> None:
        forged = _signed(mallory[0], [mallory[1]], _payload("rm -rf"))
        assert _wait_posting([alice[1]], "proj", [forged]) == (0, "")
        (receipt,) = broadcast.status()[0].receipts
        assert receipt.outcome == "rejected"
        assert "not signed" in (receipt.reason or "")

    def test_expired_message_rejected(self, alice: tuple[Path, Signer]) -> None:
        old = Payload("old-1", None, NOW, NOW + timedelta(minutes=5), "late")
        assert _wait_posting(
            [alice[1]], "proj", [_signed(alice[0], [alice[1]], old)]
        ) == (0, "")
        assert broadcast.status()[0].receipts[0].reason == "expired"

    def test_unreadable_entry_rejected(self, alice: tuple[Path, Signer]) -> None:
        def sleep(_: float) -> None:
            broadcast.ensure_dirs()
            (broadcast.messages_dir() / f"{time.time_ns():020d}-x.json").write_text(
                "{}"
            )

        assert broadcast.wait(HOOK, [alice[1]], "proj", sleep=sleep, max_polls=2) == (
            0,
            "",
        )
        assert broadcast.status()[0].receipts[0].reason.startswith("unreadable")

    def test_new_process_does_not_replay_older_messages(
        self, alice: tuple[Path, Signer]
    ) -> None:
        broadcast.post(*_signed(alice[0], [alice[1]], _payload("before")))
        start = dict(HOOK, hook_event_name="SessionStart", source="startup")
        assert broadcast.wait(
            start, [alice[1]], "proj", sleep=lambda _: None, max_polls=1
        ) == (0, "")

    def test_rearm_delivers_what_arrived_between_waiters(
        self, alice: tuple[Path, Signer]
    ) -> None:
        signers = [alice[1]]
        assert broadcast.wait(
            HOOK, signers, "proj", sleep=lambda _: None, max_polls=1, claude_pid=4242
        ) == (0, "")
        broadcast.post(*_signed(alice[0], signers, _payload("while nobody waited")))
        code, text = broadcast.wait(
            HOOK, signers, "proj", sleep=lambda _: None, max_polls=1, claude_pid=4242
        )
        assert code == 2 and "while nobody waited" in text

    def test_same_payload_reposted_not_delivered_twice(
        self, alice: tuple[Path, Signer]
    ) -> None:
        signers = [alice[1]]
        msg = _signed(alice[0], signers, _payload("once"))
        assert _wait_posting(signers, "proj", [msg])[0] == 2
        entry = broadcast.list_entries()[0]
        copy = (
            broadcast.messages_dir()
            / f"{time.time_ns():020d}-{entry.name.split('-', 1)[1]}.json"
        )
        shutil.copy(entry.sig_path, Path(f"{copy}.sig"))
        shutil.copy(entry.path, copy)
        assert broadcast.wait(
            HOOK, signers, "proj", sleep=lambda _: None, max_polls=1
        ) == (0, "")

    def test_second_waiter_stands_down(self, alice: tuple[Path, Signer]) -> None:
        broadcast.ensure_dirs()
        lock = broadcast.state_dir() / "777.lock"
        lock.write_text(
            json.dumps(
                {"pid": os.getppid(), "session_id": "s", "project": "p", "cwd": "/c"}
            )
        )
        assert broadcast.wait(HOOK, [alice[1]], "p", claude_pid=777, max_polls=1) == (
            0,
            "",
        )
        assert lock.exists()
        (listener,) = broadcast.list_listeners()
        assert listener.pid == os.getppid()

    def test_dead_waiters_lock_taken_over(self, alice: tuple[Path, Signer]) -> None:
        broadcast.ensure_dirs()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        lock = broadcast.state_dir() / "777.lock"
        lock.write_text(json.dumps({"pid": dead.pid, "session_id": "s"}))
        msg = _signed(alice[0], [alice[1]], _payload("hi"))
        code, _ = broadcast.wait(
            HOOK,
            [alice[1]],
            "p",
            sleep=lambda _: broadcast.post(*msg),
            max_polls=2,
            claude_pid=777,
        )
        assert code == 2
        assert not lock.exists()

    def test_garbage_lock_taken_over(self, alice: tuple[Path, Signer]) -> None:
        broadcast.ensure_dirs()
        (broadcast.state_dir() / "777.lock").write_text("nope")
        assert broadcast.wait(
            HOOK, [alice[1]], "p", sleep=lambda _: None, max_polls=1, claude_pid=777
        ) == (0, "")

    def test_listeners_skip_garbage(self) -> None:
        broadcast.ensure_dirs()
        (broadcast.state_dir() / "a.lock").write_text("nope")
        assert broadcast.list_listeners() == []


class TestFindClaudePid:
    def test_finds_registered_ancestor(self, home: Path) -> None:
        reg = home / ".claude" / "sessions"
        reg.mkdir(parents=True)
        (reg / f"{os.getppid()}.json").write_text("{}")
        assert broadcast.find_claude_pid(os.getpid()) == os.getppid()

    def test_none_without_registry(self) -> None:
        assert broadcast.find_claude_pid() is None
        assert broadcast.find_claude_pid(1) is None

    def test_parent_pid_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", ""),
        )
        assert broadcast._parent_pid(123) is None


class TestMain:
    def test_main_prints_text_and_exit_code(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seen: dict[str, object] = {}

        def fake_wait(
            hook: dict, signers: list[Signer], project: str | None, **kw: object
        ) -> tuple[int, str]:
            seen.update(hook=hook, signers=signers, project=project)
            return 2, "wake up"

        monkeypatch.setattr(broadcast, "wait", fake_wait)
        monkeypatch.setattr(broadcast, "find_claude_pid", lambda: None)
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(HOOK)))
        code = broadcast.main(
            [
                "wait",
                "--signers",
                broadcast.encode_signers([alice[1]]),
                "--project",
                "p",
            ]
        )
        assert code == 2
        assert capsys.readouterr().err == "wake up\n"
        assert seen == {"hook": HOOK, "signers": [alice[1]], "project": "p"}

    @pytest.mark.parametrize("stdin", ["", "not json", "[1]"])
    def test_main_tolerates_bad_stdin(
        self, stdin: str, alice: tuple[Path, Signer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            broadcast, "wait", lambda hook, *a, **kw: (0, "") if hook == {} else (9, "")
        )
        monkeypatch.setattr(broadcast, "find_claude_pid", lambda: None)
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(stdin))
        assert (
            broadcast.main(["wait", "--signers", broadcast.encode_signers([alice[1]])])
            == 0
        )

    def test_main_bad_signers_exits_quietly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("FUJIMOTO_LOG_DIR", str(tmp_path / "logs"))
        assert broadcast.main(["wait", "--signers", "!!", "--debug"]) == 0
        assert not debug.is_enabled()
        assert (
            "broadcast.wait outcome=error"
            in next((tmp_path / "logs").glob("*.log")).read_text()
        )


def test_end_to_end_with_a_real_waiter_process(
    alice: tuple[Path, Signer], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook command, run as a process, wakes on a real `send`."""
    signers = [alice[1]]
    _use_config(
        monkeypatch,
        BroadcastSettings(enabled=True, signers=signers, signing_key=str(alice[0])),
    )
    cmd = broadcast.hook_command("proj", signers)
    env = dict(
        os.environ, HOME=str(home), PYTHONPATH=str(Path(broadcast.__file__).parents[1])
    )
    proc = subprocess.Popen(
        cmd, shell=True, stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(HOOK).encode())
    proc.stdin.close()
    deadline = time.monotonic() + 10
    while not broadcast.list_listeners() and time.monotonic() < deadline:
        time.sleep(0.05)
    result = broadcast.send("end to end", "proj", wait=5)
    assert proc.wait(timeout=10) == 2
    assert proc.stderr is not None
    assert b"end to end" in proc.stderr.read()
    assert [r.outcome for r in result.receipts] == ["delivered"]


# ---------------------------------------------------------------------------
# Send / submit / status / output
# ---------------------------------------------------------------------------


class TestSend:
    def test_send_needs_signers(self) -> None:
        with pytest.raises(BroadcastError, match="no signers"):
            broadcast.send("x", None, config=BroadcastSettings())

    def test_send_with_key_not_a_signer_fails_before_posting(
        self, alice: tuple[Path, Signer], mallory: tuple[Path, Signer]
    ) -> None:
        config = BroadcastSettings(enabled=True, signers=[alice[1]])
        with pytest.raises(BroadcastError):
            broadcast.send("x", None, key=str(mallory[0]), config=config)
        assert broadcast.list_entries() == []

    def test_send_without_listeners(self, alice: tuple[Path, Signer]) -> None:
        config = BroadcastSettings(
            enabled=True, signers=[alice[1]], signing_key=str(alice[0])
        )
        result = broadcast.send("hello", None, config=config)
        assert result.signer == "alice" and result.listeners == []
        out = broadcast.format_send(result)
        assert "No sessions are listening" in out
        assert broadcast.send_json(result)["listening"] == 0

    def test_send_reports_pending_listeners(self, alice: tuple[Path, Signer]) -> None:
        broadcast.ensure_dirs()
        (broadcast.state_dir() / "1.lock").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "session_id": "abcdef1234",
                    "project": "p",
                    "cwd": str(Path.home() / "w"),
                }
            )
        )
        (broadcast.state_dir() / "2.lock").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "session_id": "zz",
                    "project": "other",
                    "cwd": None,
                }
            )
        )
        config = BroadcastSettings(
            enabled=True, signers=[alice[1]], signing_key=str(alice[0])
        )
        result = broadcast.send("hello", "p", wait=0.3, config=config)
        assert [li.session_id for li in result.listeners] == ["abcdef1234"]
        assert "1 not confirmed yet" in broadcast.format_send(result)
        shown = broadcast.format_show(config)
        assert "Listening sessions: 2" in shown and "~/w" in shown

    def test_await_receipts_stops_when_all_in(self, alice: tuple[Path, Signer]) -> None:
        entry = broadcast.post(*_signed(alice[0], [alice[1]], _payload()))
        r = broadcast.Receipt(entry.name, "s", "p", "/c", "delivered", None, "t")
        broadcast._write_receipt(r)
        assert broadcast._await_receipts(
            entry, 1, 30, sleep=lambda _: pytest.fail("waited")
        ) == [r]
        ticks = iter([0.0, 0.0, 99.0])
        assert broadcast._await_receipts(
            entry, 2, 1, sleep=lambda _: None, clock=lambda: next(ticks)
        ) == [r]

    def test_submit_presigned(self, alice: tuple[Path, Signer]) -> None:
        data, sig = _signed(alice[0], [alice[1]], _payload("from remote-agent", "p"))
        result = broadcast.submit(
            data, sig, wait=0, config=BroadcastSettings(signers=[alice[1]])
        )
        assert result.signer == "alice" and result.payload.project == "p"

    def test_submit_rejects_expired_and_forged(
        self, alice: tuple[Path, Signer], mallory: tuple[Path, Signer]
    ) -> None:
        config = BroadcastSettings(signers=[alice[1]])
        old = Payload("old", None, NOW, NOW + timedelta(minutes=1), "m")
        with pytest.raises(BroadcastError, match="expired"):
            broadcast.submit(*_signed(alice[0], [alice[1]], old), config=config)
        with pytest.raises(BroadcastError):
            broadcast.submit(
                *_signed(mallory[0], [mallory[1]], _payload()), config=config
            )
        assert broadcast.list_entries() == []

    def test_status_and_formatting(self, alice: tuple[Path, Signer]) -> None:
        assert broadcast.format_status([]) == "No broadcasts in the mailbox."
        e1 = broadcast.post(
            *_signed(alice[0], [alice[1]], _payload("first line\nsecond", "p"))
        )
        expired = Payload("gone", None, NOW, NOW + timedelta(hours=1), "y" * 100)
        e2 = broadcast.post(*_signed(alice[0], [alice[1]], expired))
        (broadcast.messages_dir() / f"{time.time_ns():020d}-junk.json").write_text(
            "junk"
        )
        broadcast._write_receipt(
            broadcast.Receipt(e1.name, "s1", "p", None, "rejected", "expired", "t")
        )
        rows = broadcast.status()
        assert [r.payload.id if r.payload else None for r in rows] == [
            None,
            "gone",
            e1.name.split("-", 1)[1],
        ]
        text = broadcast.format_status(rows)
        assert "(unreadable)" in text and "expired" in text and "“first line”" in text
        assert "…" in text and "✗ p  s1  ? (expired)" in text
        assert broadcast.status("gone")[0].entry == e2.name
        js = broadcast.status_json(rows)
        assert js[0]["id"] is None and js[1]["message"] == "y" * 100

    def test_show(self, alice: tuple[Path, Signer]) -> None:
        empty = broadcast.format_show(BroadcastSettings())
        assert "disabled" in empty and "Signers: none" in empty and "agent key" in empty
        js = broadcast.show_json(
            BroadcastSettings(enabled=True, signers=[alice[1]], signing_key="k")
        )
        assert (
            js["enabled"] is True
            and js["signers"][0]["name"] == "alice"
            and js["listening"] == []
        )


# ---------------------------------------------------------------------------
# Launch integration
# ---------------------------------------------------------------------------


class TestLaunch:
    def test_hook_settings(self, alice: tuple[Path, Signer]) -> None:
        settings = broadcast.hook_settings("proj", [alice[1]], "debug")
        (start,) = settings["hooks"]["SessionStart"][0]["hooks"]
        assert settings["hooks"]["Stop"][0]["hooks"][0] == start
        assert start["async"] is True and start["asyncRewake"] is True
        assert start["timeout"] == broadcast.HOOK_TIMEOUT
        cmd = start["command"]
        assert cmd.startswith(sys.executable) and "-m fujimoto.broadcast wait" in cmd
        assert "--project proj" in cmd and cmd.endswith("--debug")
        assert broadcast.encode_signers([alice[1]]) in cmd
        assert "--project" not in broadcast.hook_command(None, [alice[1]])

    def test_merge_adds_settings(self, tmp_path: Path) -> None:
        ours = {"hooks": {"Stop": [{"hooks": [{"x": 1}]}]}}
        out = broadcast.merge_settings_args(("--model", "opus"), ours, tmp_path)
        assert out[:2] == ("--model", "opus") and out[2] == "--settings"
        assert json.loads(out[3]) == ours

    @pytest.mark.parametrize("form", ["separate", "equals", "file"])
    def test_merge_into_existing(self, form: str, tmp_path: Path) -> None:
        existing = {"env": {"A": "1"}, "hooks": {"Stop": [{"hooks": [{"mine": 1}]}]}}
        ours = {
            "hooks": {
                "Stop": [{"hooks": [{"ours": 1}]}],
                "SessionStart": [{"hooks": [{"ours": 2}]}],
            }
        }
        value = json.dumps(existing)
        if form == "file":
            (tmp_path / "s.json").write_text(value)
            value = "s.json"
        args = (
            ("--settings", value, "--x")
            if form != "equals"
            else (f"--settings={value}", "--x")
        )
        out = broadcast.merge_settings_args(args, ours, tmp_path)
        assert out[0] == "--settings" and out[2] == "--x" and len(out) == 3
        merged = json.loads(out[1])
        assert merged["env"] == {"A": "1"}
        assert merged["hooks"]["Stop"] == [
            {"hooks": [{"mine": 1}]},
            {"hooks": [{"ours": 1}]},
        ]
        assert merged["hooks"]["SessionStart"] == [{"hooks": [{"ours": 2}]}]

    @pytest.mark.parametrize(
        ("args", "match"),
        [
            (("--settings",), "no value"),
            (("--settings", "missing.json"), "cannot read"),
            (("--settings", "[1]"), "cannot read"),
            (("--settings", '{"hooks": 1}'), "non-object"),
            (("--settings", '{"hooks": {"Stop": 1}}'), "malformed Stop"),
        ],
    )
    def test_merge_errors(
        self, args: tuple[str, ...], match: str, tmp_path: Path
    ) -> None:
        with pytest.raises(BroadcastError, match=match):
            broadcast.merge_settings_args(args, {"hooks": {"Stop": []}}, tmp_path)

    def test_launch_args_disabled_and_no_signers(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        assert broadcast.launch_args("p", tmp_path, ("--a",)) == ("--a",)
        _use_config(monkeypatch, BroadcastSettings(enabled=True))
        assert broadcast.launch_args("p", tmp_path, ("--a",)) == ("--a",)

    def test_launch_args_enabled(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_config(monkeypatch, BroadcastSettings(enabled=True, signers=[alice[1]]))
        monkeypatch.setattr(broadcast, "_project_opted_out", lambda wd: False)
        out = broadcast.launch_args("p", tmp_path, ("--a",))
        assert out[:2] == ("--a", "--settings")
        assert "--debug" not in out[2]
        debug.enable(redact=True, log_dir=tmp_path / "logs")
        assert "--debug-redacted" in broadcast.launch_args("p", tmp_path, ())[1]

    def test_launch_args_project_opt_out(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _use_config(monkeypatch, BroadcastSettings(enabled=True, signers=[alice[1]]))
        monkeypatch.setattr("fujimoto.git.get_main_worktree_root", lambda wd: tmp_path)
        (tmp_path / ".fujimoto.yaml").write_text("broadcast: false\n")
        assert broadcast.launch_args("p", tmp_path, ()) == ()
        (tmp_path / ".fujimoto.yaml").write_text("broadcast: true\n")
        assert broadcast.launch_args("p", tmp_path, ())[0] == "--settings"

    def test_opt_out_check_outside_repo(self, tmp_path: Path) -> None:
        assert broadcast._project_opted_out(tmp_path) is False

    def test_launch_args_merge_failure_still_launches(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _use_config(monkeypatch, BroadcastSettings(enabled=True, signers=[alice[1]]))
        monkeypatch.setattr(broadcast, "_project_opted_out", lambda wd: False)
        assert broadcast.launch_args("p", tmp_path, ("--settings", "nope.json")) == (
            "--settings",
            "nope.json",
        )
        assert "broadcast not enabled for this session" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    from fujimoto import cli

    monkeypatch.setattr(sys, "argv", ["fujimoto", "broadcast", *argv])
    cli.main()


class TestCli:
    def test_configure_then_send_and_status(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _cli(monkeypatch, "enable")
        assert "No signers yet" in capsys.readouterr().out
        _cli(
            monkeypatch,
            "add-signer",
            "alice",
            str(alice[0].with_suffix(".pub")),
            "--role",
            "user",
        )
        _cli(monkeypatch, "set-key", str(alice[0]))
        config = broadcast.load_config()
        assert (
            config.enabled
            and config.signers == [alice[1]]
            and config.signing_key == str(alice[0])
        )
        capsys.readouterr()
        _cli(monkeypatch, "send", "hello all", "--all-projects", "--wait", "0")
        assert "to all projects, signed by alice" in capsys.readouterr().out
        _cli(
            monkeypatch,
            "send",
            "for p",
            "--project",
            "p",
            "--ttl",
            "30m",
            "--json",
            "--wait",
            "0",
        )
        sent = json.loads(capsys.readouterr().out)
        assert sent["project"] == "p" and sent["signer"] == "alice"
        _cli(monkeypatch, "status")
        out = capsys.readouterr().out
        assert "“hello all”" in out and "“for p”" in out
        _cli(monkeypatch, "status", sent["id"], "--json")
        (row,) = json.loads(capsys.readouterr().out)
        assert row["id"] == sent["id"]
        _cli(monkeypatch, "show")
        assert "Broadcast: enabled" in capsys.readouterr().out
        _cli(monkeypatch, "show", "--json")
        assert json.loads(capsys.readouterr().out)["signers"][0]["name"] == "alice"

    def test_send_signed_file(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _cli(monkeypatch, "add-signer", "remote-agent", alice[1].public_key)
        data, sig = _signed(alice[0], [alice[1]], _payload("from afar", "p"))
        f = tmp_path / "msg.json"
        f.write_bytes(data)
        Path(f"{f}.sig").write_bytes(sig)
        capsys.readouterr()
        _cli(monkeypatch, "send", "--signed", str(f), "--wait", "0")
        assert "to project p, signed by remote-agent" in capsys.readouterr().out

    def test_send_defaults_to_current_project(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _cli(monkeypatch, "add-signer", "alice", alice[1].public_key)
        monkeypatch.setattr("fujimoto.cli.get_project_name", lambda: "here")
        _cli(monkeypatch, "send", "hi", "--key", str(alice[0]), "--wait", "0")
        assert "to project here" in capsys.readouterr().out

    def test_disable_remove_and_clear_key(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _cli(monkeypatch, "add-signer", "alice", alice[1].public_key)
        _cli(monkeypatch, "set-key", alice[1].public_key + " comment")
        assert broadcast.load_config().signing_key == alice[1].public_key
        _cli(monkeypatch, "set-key")
        _cli(monkeypatch, "remove-signer", "alice")
        _cli(monkeypatch, "disable")
        config = broadcast.load_config()
        assert config == BroadcastSettings()
        assert "Running sessions keep listening" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("argv", "match"),
        [
            (("send",), "either TEXT or --signed"),
            (("send", "x", "--signed", "f"), "either TEXT or --signed"),
            (("send", "--signed", "f", "--all-projects"), "carry their own scope"),
            (("send", "--signed", "/no/such"), "No such file"),
            (("send", "x", "--all-projects", "--ttl", "9w"), "bad --ttl"),
            (("send", "x", "--all-projects"), "no signers"),
            (("add-signer", "bad name", "ssh-ed25519 AAAA"), "signer names"),
            (("add-signer", "a", "not-a-key"), "not an OpenSSH public key"),
            (("remove-signer", "ghost"), "no signer called ghost"),
            (("set-key", "garbage"), "not an OpenSSH public key"),
        ],
    )
    def test_errors_exit_1(
        self,
        argv: tuple[str, ...],
        match: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _cli(monkeypatch, *argv)
        assert exc.value.code == 1
        assert match in capsys.readouterr().err

    def test_duplicate_signer_name(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _cli(monkeypatch, "add-signer", "alice", alice[1].public_key)
        with pytest.raises(SystemExit):
            _cli(monkeypatch, "add-signer", "alice", alice[1].public_key)
        assert "already exists" in capsys.readouterr().err

    def test_send_outside_repo_needs_scope(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from fujimoto.git import GitError

        def no_repo() -> str:
            raise GitError("not a repo")

        monkeypatch.setattr("fujimoto.cli.get_project_name", no_repo)
        with pytest.raises(SystemExit):
            _cli(monkeypatch, "send", "hi")
        assert "pass --project NAME or --all-projects" in capsys.readouterr().err

    def test_settings_write_failure(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def boom(*a: object, **kw: object) -> None:
            raise OSError("read-only")

        monkeypatch.setattr("fujimoto.global_config.update_global_key", boom)
        with pytest.raises(SystemExit):
            _cli(monkeypatch, "enable")
        assert "could not save the global config" in capsys.readouterr().err


def test_start_launch_adds_broadcast_hooks(
    alice: tuple[Path, Signer], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hooks reach claude's command line but are not saved as launch options."""
    from unittest.mock import patch

    from fujimoto import cli

    _use_config(monkeypatch, BroadcastSettings(enabled=True, signers=[alice[1]]))
    monkeypatch.setattr(broadcast, "_project_opted_out", lambda wd: False)
    target = cli.LaunchTarget(
        "proj", tmp_path, "proj/wt", "worktree", claude_args=("--model", "opus")
    )
    with (
        patch("fujimoto.cli._apply_worktree_config", return_value=True),
        patch("fujimoto.cli._session_branch", return_value="b"),
        patch("fujimoto.cli.launch_claude_in_tmux") as mock_launch,
        patch("fujimoto.cli.launch_options.save") as mock_save,
    ):
        cli._start_launch(target, attach=False)
    args = mock_launch.call_args.kwargs["extra_args"]
    assert args[:3] == ("--model", "opus", "--settings")
    assert "fujimoto.broadcast" in args[3]
    assert all(
        "--settings" not in a
        for call in mock_save.call_args_list
        for a in call.args[-1]
    )


# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_when_absent(self) -> None:
        assert broadcast.load_config() == BroadcastSettings()

    def test_roundtrip_keeps_other_keys_and_comments(
        self, alice: tuple[Path, Signer]
    ) -> None:
        from fujimoto.global_config import global_config_path, load_global_config

        path = global_config_path()
        path.parent.mkdir(parents=True)
        path.write_text("# my notes\nmodel: opus\n")
        config = BroadcastSettings(True, [alice[1]], "~/.ssh/k")
        broadcast.save_config(config)
        assert broadcast.load_config() == config
        assert load_global_config().model == "opus"
        assert path.read_text().startswith("# my notes\nmodel: opus\n")

    def test_bad_signer_is_dropped_not_trusted(
        self, alice: tuple[Path, Signer]
    ) -> None:
        from fujimoto.global_config import global_config_path

        path = global_config_path()
        path.parent.mkdir(parents=True)
        path.write_text(
            "broadcast:\n  enabled: true\n  signers:\n"
            f"  - {{name: alice, public_key: '{alice[1].public_key}'}}\n"
            "  - {name: 'bad name', public_key: 'ssh-ed25519 AAAA'}\n"
            "  - {name: junk, public_key: 'not a key'}\n"
        )
        assert broadcast.load_config().signers == [replace(alice[1], role="agent")]

    def test_invalid_file_fails_closed(self) -> None:
        from fujimoto.global_config import global_config_path

        path = global_config_path()
        path.parent.mkdir(parents=True)
        path.write_text("broadcast:\n  enabled: true\n  surprise: 1\n")
        assert broadcast.load_config() == BroadcastSettings()
        with pytest.raises(BroadcastError):
            broadcast.load_config(strict=True)


# ---------------------------------------------------------------------------
# Single-session target
# ---------------------------------------------------------------------------

SID_A = "9938a857-65f1-47cc-9562-0b987d9654f4"
SID_B = "9938ffff-0000-4000-8000-000000000000"


def _listen(lock: str, session_id: str, project: str = "p") -> None:
    broadcast.ensure_dirs()
    (broadcast.state_dir() / f"{lock}.lock").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "session_id": session_id,
                "project": project,
                "cwd": "/c",
            }
        )
    )


class TestSessionTarget:
    @pytest.fixture(autouse=True)
    def _names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            broadcast,
            "_registry_names",
            lambda: {
                SID_A: ("fujimoto/smoke", "fujimoto-smoke"),
                SID_B: ("other/thing", None),
            },
        )

    def test_payload_session_roundtrip_and_bytes(self) -> None:
        plain = broadcast.new_payload("m", "p", now=NOW)
        assert b"session" not in plain.to_bytes()  # old payload bytes unchanged
        targeted = broadcast.new_payload("m", None, now=NOW, session=SID_A)
        assert Payload.from_bytes(targeted.to_bytes()) == targeted
        assert targeted.scope_label == "session 9938a857"

    @pytest.mark.parametrize("bad", ['"short"', "3", '"has space!!"'])
    def test_bad_session_rejected(self, bad: str) -> None:
        raw = json.loads(broadcast.new_payload("m", None, now=NOW).to_bytes())
        raw["v"] = 2
        data = json.dumps(raw).replace('"v":', f'"session": {bad}, "v":', 1)
        with pytest.raises(BroadcastError, match="bad session"):
            Payload.from_bytes(data.encode())

    def test_covers_requires_the_exact_session(self) -> None:
        p = broadcast.new_payload("m", None, now=NOW, session=SID_A)
        assert p.covers("any", SID_A)
        assert not p.covers("any", SID_B) and not p.covers("any", None)

    def test_waiter_delivers_only_to_its_target(
        self, alice: tuple[Path, Signer]
    ) -> None:
        signers = [alice[1]]
        mine = dict(HOOK, session_id=SID_A)
        # Arm the target first: a waiter doesn't replay what came before it.
        assert broadcast.wait(
            mine, signers, "p", sleep=lambda _: None, max_polls=1, claude_pid=1234
        ) == (0, "")
        msg = _signed(
            alice[0], signers, broadcast.new_payload("just you", None, session=SID_A)
        )
        other = dict(HOOK, session_id=SID_B)
        assert _wait_posting(signers, "p", [msg], hook=other) == (0, "")
        assert broadcast.status()[0].receipts == []  # silently out of scope
        code, text = broadcast.wait(
            mine, signers, "p", sleep=lambda _: None, max_polls=1, claude_pid=1234
        )
        assert code == 2 and "just you" in text

    @pytest.mark.parametrize(
        "ref", ["fujimoto/smoke", "fujimoto-smoke", SID_A, "9938a8"]
    )
    def test_resolve_target(self, ref: str) -> None:
        _listen("1", SID_A)
        _listen("2", SID_B)
        target = broadcast.resolve_target(ref)
        assert target.session_id == SID_A and target.tmux_session == "fujimoto/smoke"

    @pytest.mark.parametrize(
        ("ref", "match"),
        [
            ("9938", "matches 2 sessions"),
            ("nope", "no listening session"),
            ("993", "no listening session"),
        ],
    )
    def test_resolve_target_errors(self, ref: str, match: str) -> None:
        _listen("1", SID_A)
        _listen("2", SID_B)
        with pytest.raises(BroadcastError, match=match) as exc:
            broadcast.resolve_target(ref)
        assert "fujimoto/smoke" in str(exc.value) and "other/thing" in str(exc.value)

    def test_resolve_with_nobody_listening(self) -> None:
        with pytest.raises(BroadcastError, match="listening: none"):
            broadcast.resolve_target("x")

    def test_send_to_one_session(self, alice: tuple[Path, Signer]) -> None:
        _listen("1", SID_A, "p")
        _listen("2", SID_B, "p")
        config = BroadcastSettings(
            enabled=True, signers=[alice[1]], signing_key=str(alice[0])
        )
        result = broadcast.send(
            "hi", "p", session="fujimoto/smoke", wait=0.2, config=config
        )
        assert result.payload.session == SID_A and result.payload.project is None
        assert [li.session_id for li in result.listeners] == [SID_A]
        assert broadcast.send_json(result)["session"] == SID_A
        assert "to session 9938a857" in broadcast.format_send(result)
        assert broadcast.status_json(broadcast.status())[0]["session"] == SID_A

    def test_show_lists_names(self) -> None:
        _listen("1", SID_A)
        assert "fujimoto/smoke" in broadcast.format_show(BroadcastSettings())
        assert (
            broadcast.show_json(BroadcastSettings())["listening"][0]["tmux_session"]
            == "fujimoto/smoke"
        )

    def test_cli_send_session(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _listen("1", SID_A)
        _cli(monkeypatch, "add-signer", "alice", alice[1].public_key)
        capsys.readouterr()
        from fujimoto import cli

        def run(*argv: str) -> None:
            monkeypatch.setattr(sys, "argv", ["fujimoto", *argv])
            cli.main()

        run("send", "fujimoto-smoke", "hi", "--key", str(alice[0]), "--wait", "0")
        assert "Message" in capsys.readouterr().out
        run("send", "9938a8", "hi", "--json", "--key", str(alice[0]), "--wait", "0")
        assert json.loads(capsys.readouterr().out)["session"] == SID_A
        with pytest.raises(SystemExit) as exc:
            run("send", "nobody", "hi")
        assert exc.value.code == 1 and "no listening session" in capsys.readouterr().err
        with pytest.raises(SystemExit):  # the old flag is gone from broadcast send
            _cli(monkeypatch, "send", "hi", "--session", "x")


def test_registry_names_reads_live_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from fujimoto.claude import live

    monkeypatch.setattr(
        live,
        "list_live_sessions",
        lambda: [live.LiveSession(1, SID_A, None, "peer", "idle", "t/s")],
    )
    assert broadcast._registry_names() == {SID_A: ("t/s", "peer")}

    def boom() -> list:
        raise OSError

    monkeypatch.setattr(live, "list_live_sessions", boom)
    assert broadcast._registry_names() == {}


def test_targeted_payload_is_version_2_and_old_waiters_reject_it() -> None:
    targeted = json.loads(
        broadcast.new_payload("m", None, now=NOW, session=SID_A).to_bytes()
    )
    assert targeted["v"] == 2
    assert json.loads(broadcast.new_payload("m", None, now=NOW).to_bytes())["v"] == 1
    # v1 with a session, or v2 without one, is malformed.
    for raw in (
        {**targeted, "v": 1},
        {k: v for k, v in targeted.items() if k != "session"},
    ):
        with pytest.raises(BroadcastError, match="version 2"):
            Payload.from_bytes(json.dumps(raw).encode())


def test_targeted_message_reads_as_a_message(alice: tuple[Path, Signer]) -> None:
    signers = [alice[1]]
    mine = dict(HOOK, session_id=SID_A)
    broadcast.wait(mine, signers, "p", sleep=lambda _: None, max_polls=1, claude_pid=99)
    broadcast.post(
        *_signed(alice[0], signers, broadcast.new_payload("hi", None, session=SID_A))
    )
    _, text = broadcast.wait(
        mine, signers, "p", sleep=lambda _: None, max_polls=1, claude_pid=99
    )
    assert text.startswith("Message from the user (via fujimoto, signed by alice, id ")
    (row,) = broadcast.status()
    assert row.signer == "alice" and row.delivered == 1 and row.rejected == 0


def test_prune_keeps_a_week(alice: tuple[Path, Signer]) -> None:
    broadcast.post(*_signed(alice[0], [alice[1]], _payload()))
    assert broadcast.prune(datetime.now(timezone.utc) + timedelta(days=5)) == 0
    assert broadcast.prune(datetime.now(timezone.utc) + timedelta(days=9)) == 1


# ---------------------------------------------------------------------------
# TUI: the `b` message log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_opens_the_message_log_and_a_message(
    alice: tuple[Path, Signer],
) -> None:
    from textual.widgets import ListView, Static

    from fujimoto.cli import SessionApp
    from tests.test_cli import _patch_git_info

    signers = [alice[1]]
    msg = broadcast.post(
        *_signed(
            alice[0], signers, _payload("[bold]not markup[/] line one\nline two", "p")
        )
    )
    broadcast._write_receipt(
        broadcast.Receipt(msg.name, SID_A, "p", "/w", "delivered", None, "t", "alice")
    )
    broadcast._write_receipt(
        broadcast.Receipt(msg.name, SID_B, "p", "/v", "rejected", "expired", "t")
    )
    old = Payload("old-1", None, NOW, NOW + timedelta(hours=1), "earlier")
    broadcast.post(*_signed(alice[0], signers, old))
    (broadcast.messages_dir() / f"{time.time_ns():020d}-junk.json").write_text("junk")

    with _patch_git_info():
        app = SessionApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.check_action("show_messages", ()) is True
            await pilot.press("b")
            await pilot.pause()
            assert app._on_messages and not app._on_home
            assert app.check_action("show_messages", ()) is False
            listing = app.query_one("#messages-list", ListView)
            rows = [str(item.query_one(Static).render()) for item in listing.children]
            assert len(rows) == 3 and "unreadable" in rows[0]
            assert "Broadcast to project p" in rows[2] and "alice" in rows[2]
            assert "✓1" in rows[2] and "✗1" in rows[2]
            assert "[bold]not markup[/] line one" in rows[2]  # shown literally
            assert "expired" in rows[1]
            listing.index = 2
            await pilot.press("enter")
            await pilot.pause()
            assert app._on_message_detail
            text = " ".join(
                str(s.render()) for s in app.query("#message-detail Static")
            )
            assert "line two" in text and "signed by alice" in text
            assert "(expired)" in text and "/v" in text
            await pilot.press("escape")
            await pilot.pause()
            assert app._on_messages and not app._on_message_detail
            assert app.query_one("#messages-list", ListView).index == 2
            await pilot.press("escape")
            await pilot.pause()
            assert app._on_home


@pytest.mark.asyncio
async def test_message_log_empty_and_unreadable_detail() -> None:

    from fujimoto.cli import SessionApp
    from tests.test_cli import _patch_git_info

    with _patch_git_info():
        app = SessionApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            labels = " ".join(
                str(lb.render()) for lb in app.query("#messages-panel Label")
            )
            assert "No broadcasts or messages" in labels and "(0 items)" in labels
            await app._show_message_detail(broadcast.StatusRow("1-x", None, []))
            await pilot.pause()
            text = " ".join(
                str(s.render()) for s in app.query("#message-detail Static")
            )
            assert "unreadable" in text and "No session has received it" in text
            await app.action_show_messages()  # off the home screen: ignored
            assert app._on_message_detail


# ---------------------------------------------------------------------------
# Signer roles, relayed approval, signer re-verification
# ---------------------------------------------------------------------------


class TestRoles:
    def test_preambles(self) -> None:
        p = broadcast.new_payload("x", None, now=NOW)
        user = broadcast.preamble(p, "laptop", "user")
        assert user.startswith(
            f"Broadcast from the user (via fujimoto, signed by laptop, id {p.id})."
        )
        agent = broadcast.preamble(p, "calcifer", "agent")
        assert agent.startswith(
            f"Broadcast from calcifer, an agent with a trusted key (via fujimoto, id {p.id})."
        )
        assert "from the user" not in agent
        approved = broadcast.new_payload(
            "x",
            None,
            now=NOW,
            session=SID_A,
            approval="nightly summary in every session",
        )
        relayed = broadcast.preamble(approved, "calcifer", "agent")
        assert relayed.startswith("Message from calcifer, an agent")
        assert (
            "relaying the user's approval: “nightly summary in every session”"
            in relayed
        )
        assert (
            "that approval is its claim" in relayed
            and "permission prompts still apply" in relayed
        )
        # A user signer's message needs no relayed approval wording.
        assert "relaying" not in broadcast.preamble(approved, "laptop", "user")
        assert "an unknown signer" in broadcast.preamble(p, None, "agent")
        assert all(
            s.endswith("safe stopping point in your current work.")
            for s in (user, agent, relayed)
        )

    def test_signer_role_lookup(self, alice: tuple[Path, Signer]) -> None:
        assert broadcast.signer_role([alice[1]], "alice") == "user"
        assert broadcast.signer_role([alice[1]], "stranger") == "agent"

    def test_roles_travel_in_the_hook_token(
        self, alice: tuple[Path, Signer], mallory: tuple[Path, Signer]
    ) -> None:
        signers = [alice[1], mallory[1]]  # user, agent
        assert broadcast.decode_signers(broadcast.encode_signers(signers)) == signers

    def test_legacy_token_reads_as_user(self, alice: tuple[Path, Signer]) -> None:
        import base64

        token = base64.urlsafe_b64encode(
            json.dumps([["old", alice[1].public_key]]).encode()
        ).decode()
        assert broadcast.decode_signers(token) == [
            Signer("old", alice[1].public_key, "user")
        ]
        bad = base64.urlsafe_b64encode(
            json.dumps([["x", alice[1].public_key, "boss"]]).encode()
        ).decode()
        with pytest.raises(BroadcastError, match="unknown role"):
            broadcast.decode_signers(bad)

    def test_agent_delivery_names_the_agent(self, mallory: tuple[Path, Signer]) -> None:
        signers = [mallory[1]]
        msg = _signed(
            mallory[0],
            signers,
            broadcast.new_payload("status?", "p", approval="ok to report"),
        )
        code, text = _wait_posting(signers, "p", [msg])
        assert code == 2 and text.startswith("Broadcast from mallory, an agent")
        assert "“ok to report”" in text

    def test_approval_payload(self) -> None:
        p = broadcast.new_payload("m", None, now=NOW, approval="  yes  ")
        assert p.approval == "yes" and Payload.from_bytes(p.to_bytes()) == p
        assert b"approval" not in broadcast.new_payload("m", None, now=NOW).to_bytes()
        for bad in ("", " ", "x" * (broadcast.MAX_APPROVAL_CHARS + 1)):
            with pytest.raises(BroadcastError, match="--approval"):
                broadcast.new_payload("m", None, now=NOW, approval=bad)
        raw = json.loads(p.to_bytes())
        for bad_value in (3, "", "y" * (broadcast.MAX_APPROVAL_CHARS + 1)):
            raw["approval"] = bad_value
            with pytest.raises(BroadcastError, match="bad approval"):
                Payload.from_bytes(json.dumps(raw).encode())

    def test_send_with_approval(self, mallory: tuple[Path, Signer]) -> None:
        config = BroadcastSettings(
            enabled=True, signers=[mallory[1]], signing_key=str(mallory[0])
        )
        result = broadcast.send("hi", "p", approval="go", wait=0, config=config)
        assert (
            result.payload.approval == "go"
            and broadcast.send_json(result)["approval"] == "go"
        )
        assert broadcast.status_json(broadcast.status())[0]["approval"] == "go"

    def test_config_roles_roundtrip(
        self, alice: tuple[Path, Signer], mallory: tuple[Path, Signer]
    ) -> None:
        config = BroadcastSettings(True, [alice[1], mallory[1]], None)
        broadcast.save_config(config)
        assert broadcast.load_config() == config
        shown = broadcast.format_show(config)
        assert "alice (user)" in shown and "mallory (agent)" in shown
        assert [s["role"] for s in broadcast.show_json(config)["signers"]] == [
            "user",
            "agent",
        ]


class TestReverify:
    def test_receipt_without_signer_is_reverified(
        self, alice: tuple[Path, Signer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_config(monkeypatch, BroadcastSettings(True, [alice[1]]))
        entry = broadcast.post(*_signed(alice[0], [alice[1]], _payload()))
        broadcast._write_receipt(
            broadcast.Receipt(entry.name, "s", "p", None, "delivered", None, "t")
        )
        (row,) = broadcast.status()
        assert row.signer == "alice" and row.verified_signer == "alice"
        assert broadcast.status_json([row])[0]["signer"] == "alice"

    def test_named_receipt_skips_the_check(
        self, alice: tuple[Path, Signer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_config(monkeypatch, BroadcastSettings(True, [alice[1]]))
        entry = broadcast.post(*_signed(alice[0], [alice[1]], _payload()))
        broadcast._write_receipt(
            broadcast.Receipt(
                entry.name, "s", "p", None, "delivered", None, "t", "alice"
            )
        )
        monkeypatch.setattr(
            broadcast, "_reverify", lambda *a: pytest.fail("re-verified")
        )
        assert broadcast.status()[0].signer == "alice"

    def test_unverifiable_or_no_signers(
        self,
        alice: tuple[Path, Signer],
        mallory: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = broadcast.post(*_signed(mallory[0], [mallory[1]], _payload()))
        assert broadcast._reverify(entry, []) is None
        assert broadcast._reverify(entry, [alice[1]]) is None


class TestRoleCli:
    def test_add_with_role_and_set_role(
        self,
        alice: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _cli(monkeypatch, "add-signer", "laptop", alice[1].public_key, "--role", "user")
        _cli(monkeypatch, "add-signer", "helper", alice[1].public_key)
        assert [s.role for s in broadcast.load_config().signers] == ["user", "agent"]
        _cli(monkeypatch, "set-role", "helper", "user")
        assert broadcast.load_config().signers[1].role == "user"
        assert "helper is now a user signer" in capsys.readouterr().out
        with pytest.raises(SystemExit):
            _cli(monkeypatch, "set-role", "ghost", "agent")
        assert "no signer called ghost" in capsys.readouterr().err

    def test_send_approval_flag(
        self,
        mallory: tuple[Path, Signer],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from fujimoto import cli

        _cli(monkeypatch, "add-signer", "helper", mallory[1].public_key)
        capsys.readouterr()
        _cli(
            monkeypatch,
            "send",
            "hi",
            "--all-projects",
            "--approval",
            "go ahead",
            "--key",
            str(mallory[0]),
            "--wait",
            "0",
            "--json",
        )
        assert json.loads(capsys.readouterr().out)["approval"] == "go ahead"
        _listen("1", SID_A)
        monkeypatch.setattr(
            broadcast, "_registry_names", lambda: {SID_A: ("t/one", None)}
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fujimoto",
                "send",
                "t/one",
                "hi",
                "--approval",
                "ok",
                "--key",
                str(mallory[0]),
                "--wait",
                "0",
                "--json",
            ],
        )
        cli.main()
        assert json.loads(capsys.readouterr().out)["approval"] == "ok"
