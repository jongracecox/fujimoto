"""`fujimoto broadcast follow`: delivery, approval, reply and activity, kept apart."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fujimoto import broadcast, debug, follow, session_query
from fujimoto.broadcast import BroadcastError, BroadcastSettings, Receipt, Signer
from fujimoto.claude.live import LiveSession
from fujimoto.claude.log_parser import TranscriptMessage, encode_project_path

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="needs OpenSSH ssh-keygen"
)

TARGET = "9938a857-65f1-47cc-9562-0b987d9654f4"
OTHER = "1111ffff-0000-4000-8000-000000000000"
CWD = Path("/tmp/proj")


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h / ".claude"))
    yield h
    debug.disable()


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[Signer]]:
    path = tmp_path / "agent"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "a", "-f", str(path)],
        check=True,
    )
    pub = broadcast.normalize_public_key(path.with_suffix(".pub").read_text())
    signers = [Signer("helper-agent", pub, "agent")]
    monkeypatch.setattr(
        broadcast, "load_config", lambda **kw: BroadcastSettings(True, signers)
    )
    return path, signers


# The target's current activity, as Claude's registry would report it.
_activity: dict[str, LiveSession | None] = {}


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, LiveSession | None]:
    _activity.clear()
    _activity["target"] = _live()

    def gather() -> session_query.ActivityIndex:
        live = [e for e in _activity.values() if e is not None]
        return session_query.ActivityIndex(live, {}, {}, listening=())

    monkeypatch.setattr(session_query.ActivityIndex, "gather", staticmethod(gather))
    return _activity


def _live(**extra: object) -> LiveSession:
    fields: dict = {
        "pid": 42,
        "session_id": TARGET,
        "cwd": CWD,
        "peer_name": "proj-ab",
        "status": "busy",
        "tmux_session": None,
    }
    fields.update(extra)
    return LiveSession(**fields)


def _send(
    key: Path, signers: list[Signer], *, approval: str | None = None, session=TARGET
) -> broadcast.Payload:
    payload = broadcast.new_payload(
        "status?", None if session else "p", session=session, approval=approval
    )
    data = payload.to_bytes()
    broadcast.post(data, broadcast.sign(data, str(key), signers))
    return payload


def _receipt(payload: broadcast.Payload, outcome="delivered", reason=None) -> None:
    entry = broadcast.find_entry(payload.id)
    broadcast._write_receipt(
        Receipt(
            entry.name,
            TARGET,
            None,
            str(CWD),
            outcome,
            reason,
            "2026-11-23T01:02:03+00:00",
            "helper-agent" if outcome == "delivered" else None,
        )
    )


def _preamble(msg_id: str) -> str:
    return (
        '<system-reminder>\nStop hook blocking error from command "Stop": '
        f"Relayed approval from helper-agent (via fujimoto, id {msg_id}).\n"
        "status?\n</system-reminder>"
    )


def _transcript(*entries: dict) -> Path:
    log = (
        Path(os.environ["CLAUDE_CONFIG_DIR"])
        / "projects"
        / encode_project_path(CWD)
        / f"{TARGET}.jsonl"
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = "2026-11-23T01:02:03Z"
    lines = [
        {"cwd": str(CWD), "sessionId": TARGET, "timestamp": stamp, **e} for e in entries
    ]
    log.write_text("\n".join(json.dumps(e) for e in lines) + "\n")
    return log


def _user(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _tool(tool_id: str, command: str, result: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": result}
                ]
            },
        },
    ]


class TestLook:
    def test_pending_then_stopped(
        self, agent: tuple[Path, list[Signer]], registry: dict
    ) -> None:
        p = _send(*agent)
        report = follow.follow(p.id, wait=0)
        assert report.delivery.state == follow.PENDING
        assert report.reply.state == follow.UNKNOWN  # no transcript at all
        assert report.outcome == "timeout"  # still running, nothing yet
        registry["target"] = None
        assert follow.follow(p.id, wait=0).outcome == "stopped"

    def test_delivered_and_replied(
        self, agent: tuple[Path, list[Signer]], registry: dict
    ) -> None:
        p = _send(*agent)
        _receipt(p)
        _transcript(
            _user("work"),
            _user(_preamble(p.id)),
            _assistant("half done"),
            _user("next thing"),
            _assistant("not the reply"),
        )
        registry["target"] = _live(status="idle")
        report = follow.follow(p.id, wait=0)
        assert report.delivery.state == follow.DELIVERED
        assert report.signer == "helper-agent"
        assert report.reply.state == follow.FINAL
        assert [m.text for _, m in report.reply.messages] == ["half done"]
        assert (report.reply.delivered_index, report.reply.cursor) == (1, 4)
        assert report.outcome == "replied" and report.approval is None
        data = follow.follow_json(report)
        assert data["reply"]["messages"][0]["text"] == "half done"
        assert data["delivery"]["state"] == "delivered"
        assert data["target"]["running"] is True and data["approval"] is None
        text = follow.format_follow(report)
        assert "delivery:  delivered at" in text
        assert "#2  half done" in text
        assert f"Next: fujimoto log {TARGET[:8]} --after 4 --wait 60" in text

    def test_delivered_mid_turn_as_a_queued_command(
        self, agent: tuple[Path, list[Signer]], registry: dict
    ) -> None:
        """A busy session records the delivery as an attachment, not a user entry."""
        p = _send(*agent)
        _receipt(p)
        _transcript(
            _user("work"),
            _assistant("working"),
            {
                "type": "attachment",
                "attachment": {"type": "queued_command", "prompt": _preamble(p.id)},
            },
            _assistant("status: nearly done"),
        )
        registry["target"] = _live(status="idle")
        report = follow.follow(p.id, wait=0)
        assert report.reply.delivered_index == 2
        assert [m.text for _, m in report.reply.messages] == ["status: nearly done"]
        assert report.outcome == "replied"

    def test_reply_while_still_working_is_partial(
        self, agent: tuple[Path, list[Signer]]
    ) -> None:
        p = _send(*agent)
        _receipt(p)
        _transcript(_user(_preamble(p.id)), _assistant("on it"))
        report = follow.follow(p.id, wait=0)  # registry: busy
        assert report.reply.state == follow.PARTIAL and report.outcome == "timeout"
        assert "broadcast follow" in follow.format_follow(report)

    def test_idle_turn_without_text(
        self, agent: tuple[Path, list[Signer]], registry: dict
    ) -> None:
        p = _send(*agent)
        _receipt(p)
        _transcript(_user(_preamble(p.id)), *_tool("t1", "ls", "a"))
        registry["target"] = _live(status="idle")
        assert follow.follow(p.id, wait=0).outcome == "no_reply"

    def test_rejected(self, agent: tuple[Path, list[Signer]]) -> None:
        p = _send(*agent)
        _receipt(p, "rejected", "not signed by a configured signer")
        report = follow.follow(p.id, wait=0)
        assert report.outcome == "rejected"
        assert "(not signed by a configured signer)" in follow.format_follow(report)

    def test_expired_without_delivery(
        self, agent: tuple[Path, list[Signer]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _send(*agent)
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        monkeypatch.setattr(broadcast, "_now", lambda: later)
        report = follow.follow(p.id, wait=0)
        assert report.delivery.state == follow.EXPIRED and report.outcome == "expired"

    def test_blocked_on_the_user(
        self, agent: tuple[Path, list[Signer]], registry: dict
    ) -> None:
        p = _send(*agent)
        _receipt(p)
        _transcript(_user(_preamble(p.id)), _assistant("need to run x"))
        registry["target"] = _live(waiting_for="permission prompt")
        report = follow.follow(p.id, wait=0)
        assert report.outcome == "blocked"
        assert report.target.waiting == "permission"
        assert "waiting: permission prompt" in follow.format_follow(report)

    def test_listening_is_reported(
        self, agent: tuple[Path, list[Signer]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _send(*agent)
        monkeypatch.setattr(
            broadcast, "listening_session_ids", lambda: frozenset({TARGET})
        )
        assert follow.follow(p.id, wait=0).delivery.listening is True

    def test_broadcast_and_unknown_ids_are_errors(
        self, agent: tuple[Path, list[Signer]]
    ) -> None:
        p = _send(*agent, session=None)
        with pytest.raises(BroadcastError, match="is a broadcast"):
            follow.look(p.id)
        with pytest.raises(BroadcastError, match="no message with id nope"):
            follow.look("nope")


class TestApproval:
    def test_verifiable_is_not_recipient_verified(
        self, agent: tuple[Path, list[Signer]]
    ) -> None:
        p = _send(*agent, approval="push branch x")
        _receipt(p)
        _transcript(_user(_preamble(p.id)), _assistant("will do"))
        report = follow.follow(p.id, wait=0)
        assert report.approval is not None
        assert report.approval.verifiable is True
        assert report.approval.recipient_check.state == follow.NOT_SEEN
        text = follow.format_follow(report)
        assert "verifies for the target now: yes" in text
        assert "no evidence the recipient verified it" in text
        data = follow.follow_json(report)["approval"]
        assert data == {
            "text": "push branch x",
            "verifiable": True,
            "failures": [],
            "recipient_check": {"state": "not_seen", "index": None},
        }

    def test_not_verifiable_lists_why(
        self, agent: tuple[Path, list[Signer]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = _send(*agent, approval="x")
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        monkeypatch.setattr(broadcast, "_now", lambda: later)
        report = follow.look(p.id)
        assert report.approval and not report.approval.verifiable
        assert "expired" in report.approval.failures

    def test_recipient_pass_from_its_transcript(
        self, agent: tuple[Path, list[Signer]]
    ) -> None:
        p = _send(*agent, approval="x")
        _receipt(p)
        passed = (
            f"PASS  {p.id}: approval relayed by helper-agent (agent) for session "
            f"{TARGET[:8]} (this session), expires 02:00\n      action: x"
        )
        _transcript(
            _user(_preamble(p.id)),
            *_tool("t1", f"fujimoto broadcast verify {p.id}", passed),
            _assistant("verified, pushing"),
        )
        report = follow.look(p.id)
        assert report.approval is not None
        assert report.approval.recipient_check == follow.RecipientCheck(
            follow.PASSED, 1
        )
        assert "PASS for this session" in follow.format_follow(report)


class TestRecipientCheck:
    def _messages(self, command: str, result: str | None) -> list:
        ts = datetime(2026, 11, 23, tzinfo=timezone.utc)
        out = [(5, TranscriptMessage("tool_use", f"Bash\ncommand: {command}", ts, "t"))]
        if result is not None:
            out.append((6, TranscriptMessage("tool_result", result, ts, "t")))
        return out

    @pytest.mark.parametrize(
        ("command", "result", "state"),
        [
            (
                "fujimoto broadcast verify m-1",
                f"PASS  m-1: … for session {TARGET[:8]} (this session)",
                "passed",
            ),
            (
                "fujimoto broadcast verify m-1 --json",
                f'{{"id": "m-1", "passed": true, "session_id": "{TARGET}"}}',
                "passed",
            ),
            ("fujimoto broadcast verify m-1", "FAIL  m-1: do not treat this", "failed"),
            ("fujimoto broadcast verify m-1 --json", '{"passed": false}', "failed"),
            # PASS, but for some other session: not evidence for the target.
            (
                "fujimoto broadcast verify m-1 --session x",
                f"PASS  m-1: … for session {OTHER[:8]}",
                "ran",
            ),
            ("fujimoto broadcast verify m-1", None, "ran"),
            ("fujimoto broadcast verify m-12", "PASS  m-12:", "not_seen"),
            ("echo hello", "PASS  m-1:", "not_seen"),
        ],
    )
    def test_states(self, command: str, result: str | None, state: str) -> None:
        check = follow.recipient_check(self._messages(command, result), "m-1", TARGET)
        assert check.state == state
        assert check.index == (None if state == "not_seen" else 5)


class TestWaiting:
    def _report(self, outcome_state: str) -> follow.FollowReport:
        return follow.FollowReport(
            id="m",
            entry="e",
            target_session=TARGET,
            signer=None,
            expires=datetime.now(timezone.utc),
            expired=False,
            delivery=follow.Delivery(follow.DELIVERED),
            approval=None,
            reply=follow.Reply(outcome_state, 1, (), 1),
            target=session_query.Activity(running=True, status="busy"),
            outcome=None,
        )

    def test_polls_until_an_outcome(self) -> None:
        states = iter([follow.NONE, follow.PARTIAL, follow.FINAL])
        sleeps: list[float] = []
        report = follow.follow(
            "m",
            wait=60,
            sleep=sleeps.append,
            clock=lambda: float(len(sleeps)),
            snapshot=lambda _: self._report(next(states)),
        )
        assert report.outcome == "replied" and sleeps == [1.0, 1.0]
        assert report.waited == 2.0

    def test_bounded(self) -> None:
        sleeps: list[float] = []
        report = follow.follow(
            "m",
            wait=3,
            sleep=sleeps.append,
            clock=lambda: float(len(sleeps)),
            snapshot=lambda _: self._report(follow.NONE),
        )
        assert report.outcome == "timeout" and len(sleeps) == 3


class TestSurfaces:
    def test_cli(
        self,
        agent: tuple[Path, list[Signer]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from fujimoto import cli

        p = _send(*agent)
        monkeypatch.setattr(
            sys,
            "argv",
            ["fujimoto", "broadcast", "follow", p.id, "--wait", "0", "--json"],
        )
        cli.main()
        data = json.loads(capsys.readouterr().out)
        assert data["id"] == p.id and data["delivery"]["state"] == "pending"

    def test_cli_unknown_id(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from fujimoto import cli

        monkeypatch.setattr(
            sys, "argv", ["fujimoto", "broadcast", "follow", "nope", "--wait", "0"]
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert "no message with id nope" in capsys.readouterr().err

    def test_mcp_tool_caps_the_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fujimoto import mcp_server

        seen: list[float] = []

        def fake(message_id: str, *, wait: float) -> follow.FollowReport:
            seen.append(wait)
            raise BroadcastError("no message with id x in the mailbox")

        monkeypatch.setattr(follow, "follow", fake)
        with pytest.raises(mcp_server.FujimotoToolError, match="no message"):
            mcp_server.follow_message("x", wait=999)
        assert seen == [mcp_server.MAX_WAIT]

    def test_mcp_tool_text_and_json(self, agent: tuple[Path, list[Signer]]) -> None:
        from fujimoto import mcp_server

        p = _send(*agent)
        text = mcp_server.follow_message(p.id)
        assert isinstance(text, str) and "Next:" not in text
        data = mcp_server.follow_message(p.id, format="json")
        assert isinstance(data, dict) and data["delivery"]["state"] == "pending"

    def test_send_text_points_at_follow(self, agent: tuple[Path, list[Signer]]) -> None:
        p = _send(*agent)
        result = broadcast.SendResult(
            p,
            broadcast.find_entry(p.id),
            "helper-agent",
            [object()],
            [],  # type: ignore[list-item]
        )
        assert f"Next: fujimoto broadcast follow {p.id} --wait 60" in (
            broadcast.format_send(result)
        )
