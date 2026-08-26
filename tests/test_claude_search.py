from __future__ import annotations

import json
from pathlib import Path

import pytest

from fujimoto.claude.search import (
    MAX_SNIPPETS,
    ContentMode,
    SearchError,
    _collapse,
    _message_text,
    _snippet,
    compile_matcher,
    iter_hits,
    list_session_logs,
    search_log,
)


# -- Helpers --


def _entry(role: str, content, **extra) -> dict:
    entry = {
        "type": role,
        "cwd": "/repo",
        "gitBranch": "main",
        "timestamp": "2026-08-26T10:00:00Z",
        "message": {"content": content},
    }
    entry["message"].update(extra.pop("message", {}))
    entry.update(extra)
    return entry


def _write_log(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def _simple_log(path: Path, *texts: str) -> Path:
    return _write_log(path, [_entry("user", t) for t in texts])


# -- ContentMode --


def test_content_mode_labels_and_toggle():
    assert ContentMode.RAW.label == "raw"
    assert ContentMode.TEXT.label == "message text"
    assert ContentMode.RAW.toggled() is ContentMode.TEXT
    assert ContentMode.TEXT.toggled() is ContentMode.RAW


# -- compile_matcher --


def test_literal_query_escapes_metacharacters():
    matcher = compile_matcher("a.c")
    assert matcher.present_in("a.c")
    assert not matcher.present_in("abc")


def test_regex_query_is_not_escaped():
    matcher = compile_matcher("a.c", regex=True)
    assert matcher.present_in("abc")


def test_matching_is_case_insensitive():
    assert compile_matcher("NEEDLE").present_in("a needle here")
    assert compile_matcher("ne+dle", regex=True).present_in("NEEEDLE")


def test_invalid_regex_raises_search_error():
    with pytest.raises(SearchError, match="invalid regex"):
        compile_matcher("(unclosed", regex=True)


def test_invalid_regex_is_fine_as_a_literal():
    assert compile_matcher("(unclosed").present_in("say (unclosed here")


def test_matcher_carries_its_mode():
    assert compile_matcher("x", mode=ContentMode.TEXT).mode is ContentMode.TEXT
    assert compile_matcher("x").mode is ContentMode.RAW


# -- list_session_logs --


def test_lists_logs_for_root_and_worktrees_newest_first(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(
        "fujimoto.claude.search.get_claude_projects_dir", lambda: projects
    )
    root = Path("/repo")
    worktree = Path("/repo/wt")

    old = _simple_log(projects / "-repo" / "old.jsonl", "hi")
    new = _simple_log(projects / "-repo-wt" / "new.jsonl", "hi")
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    assert list_session_logs(root, [worktree]) == [new, old]


def test_lists_nothing_when_no_encoded_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "fujimoto.claude.search.get_claude_projects_dir", lambda: tmp_path
    )
    assert list_session_logs(Path("/nope"), []) == []


def test_none_project_root_still_scans_worktrees(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(
        "fujimoto.claude.search.get_claude_projects_dir", lambda: projects
    )
    log = _simple_log(projects / "-repo-wt" / "a.jsonl", "hi")
    assert list_session_logs(None, [Path("/repo/wt")]) == [log]


def test_duplicate_targets_are_listed_once(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(
        "fujimoto.claude.search.get_claude_projects_dir", lambda: projects
    )
    log = _simple_log(projects / "-repo" / "a.jsonl", "hi")
    assert list_session_logs(Path("/repo"), [Path("/repo")]) == [log]


def test_non_jsonl_files_are_ignored(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(
        "fujimoto.claude.search.get_claude_projects_dir", lambda: projects
    )
    (projects / "-repo").mkdir(parents=True)
    (projects / "-repo" / "notes.txt").write_text("needle")
    assert list_session_logs(Path("/repo"), []) == []


# -- search_log: raw mode --


def test_raw_mode_matches_message_text(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "find the needle please")
    hit = search_log(log, compile_matcher("needle"))
    assert hit is not None
    assert hit.match_count == 1
    assert "needle" in hit.snippets[0].text
    assert hit.session.session_id == "a"


def test_raw_mode_matches_tool_output(tmp_path):
    log = _write_log(
        tmp_path / "a.jsonl",
        [_entry("user", [{"type": "tool_result", "content": "needle in output"}])],
    )
    assert search_log(log, compile_matcher("needle")) is not None


def test_raw_mode_matches_json_scaffolding(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "nothing here")
    # `gitBranch` only exists as a JSON key, so this is a raw-only match.
    assert search_log(log, compile_matcher("gitBranch")) is not None


def test_raw_mode_counts_every_occurrence(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "needle", "needle needle")
    hit = search_log(log, compile_matcher("needle"))
    assert hit is not None
    assert hit.match_count == 3


def test_snippets_are_capped(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", *(["needle"] * 10))
    hit = search_log(log, compile_matcher("needle"))
    assert hit is not None
    assert hit.match_count == 10
    assert len(hit.snippets) == MAX_SNIPPETS


def test_dense_matches_do_not_produce_duplicate_snippets(tmp_path):
    """Matches inside an already-emitted window are counted, not re-snippetted."""
    log = _simple_log(tmp_path / "a.jsonl", "needle needle needle")
    hit = search_log(log, compile_matcher("needle", mode=ContentMode.TEXT))
    assert hit is not None
    assert hit.match_count == 3
    # One window covers all three, and highlights all three.
    assert len(hit.snippets) == 1
    assert len(hit.snippets[0].spans) == 3


def test_distant_matches_produce_separate_snippets(tmp_path):
    gap = "-" * 400
    log = _simple_log(tmp_path / "a.jsonl", f"needle{gap}needle{gap}needle")
    hit = search_log(log, compile_matcher("needle", mode=ContentMode.TEXT))
    assert hit is not None
    assert hit.match_count == 3
    assert len(hit.snippets) == 3


def test_no_match_returns_none(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "hello")
    assert search_log(log, compile_matcher("needle")) is None


def test_unreadable_log_returns_none(tmp_path):
    assert search_log(tmp_path / "missing.jsonl", compile_matcher("needle")) is None


def test_unparseable_log_returns_none(tmp_path):
    # Contains the query but yields no session — nothing to offer the user.
    log = tmp_path / "a.jsonl"
    log.write_text("needle, but not JSON\n")
    assert search_log(log, compile_matcher("needle")) is None


def test_regex_matcher_against_a_log(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "error code 4711")
    assert search_log(log, compile_matcher(r"code \d+", regex=True)) is not None
    assert search_log(log, compile_matcher(r"code \d+")) is None


# -- search_log: text mode --


def test_text_mode_matches_string_content(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "find the needle")
    hit = search_log(log, compile_matcher("needle", mode=ContentMode.TEXT))
    assert hit is not None
    assert [s.text for s in hit.snippets] == ["find the needle"]
    # The span points at the match inside the snippet.
    assert hit.snippets[0].spans == ((9, 15),)


def test_text_mode_matches_text_blocks(tmp_path):
    log = _write_log(
        tmp_path / "a.jsonl",
        [_entry("assistant", [{"type": "text", "text": "here is the needle"}])],
    )
    hit = search_log(log, compile_matcher("needle", mode=ContentMode.TEXT))
    assert hit is not None
    assert hit.match_count == 1


def test_text_mode_ignores_tool_output(tmp_path):
    log = _write_log(
        tmp_path / "a.jsonl",
        [
            _entry("user", "hello"),
            _entry("user", [{"type": "tool_result", "content": "needle in output"}]),
        ],
    )
    assert search_log(log, compile_matcher("needle", mode=ContentMode.TEXT)) is None
    assert search_log(log, compile_matcher("needle")) is not None


def test_text_mode_ignores_json_scaffolding(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "nothing here")
    assert search_log(log, compile_matcher("gitBranch", mode=ContentMode.TEXT)) is None


def test_text_mode_skips_unparseable_and_blank_lines(tmp_path):
    log = tmp_path / "a.jsonl"
    log.write_text(
        "\n".join(
            [
                "",
                "not json but has needle",
                "[1, 2, 3]",
                json.dumps(_entry("user", "a real needle")),
            ]
        )
    )
    hit = search_log(log, compile_matcher("needle", mode=ContentMode.TEXT))
    assert hit is not None
    assert hit.match_count == 1


# -- _message_text --


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"message": "not a dict"},
        {"message": {}},
        {"message": {"content": 42}},
        {"message": {"content": ["a string block", None]}},
        {"message": {"content": [{"type": "text"}]}},
        {"message": {"content": [{"type": "tool_use", "text": "x"}]}},
    ],
)
def test_message_text_tolerates_odd_shapes(entry):
    assert _message_text(entry) == ""


def test_message_text_joins_multiple_blocks():
    entry = {
        "message": {
            "content": [
                {"type": "text", "text": "one"},
                {"type": "text", "text": "two"},
            ]
        }
    }
    assert _message_text(entry) == "one\ntwo"


# -- _snippet --


def test_snippet_collapses_whitespace_and_control_characters():
    snippet = _snippet("a\t \nneedle\x00here", 4, 10, compile_matcher("needle"))
    assert snippet.text == "a needle here"
    # Offsets are mapped through the collapse, not recomputed.
    start, end = snippet.spans[0]
    assert snippet.text[start:end] == "needle"


def test_snippet_elides_both_ends():
    source = "x" * 200 + "needle" + "y" * 200
    snippet = _snippet(source, 200, 206, compile_matcher("needle"))
    assert snippet.text.startswith("…")
    assert snippet.text.endswith("…")
    start, end = snippet.spans[0]
    assert snippet.text[start:end] == "needle"


def test_short_source_is_not_elided():
    snippet = _snippet("just needle", 5, 11, compile_matcher("needle"))
    assert snippet.text == "just needle"
    assert snippet.spans == ((5, 11),)


def test_snippet_reports_every_match_in_the_window():
    snippet = _snippet("needle then needle", 0, 6, compile_matcher("needle"))
    assert [snippet.text[a:b] for a, b in snippet.spans] == ["needle", "needle"]


def test_snippet_spans_survive_collapsed_whitespace_before_the_match():
    snippet = _snippet("a  \t  b needle", 8, 14, compile_matcher("needle"))
    assert snippet.text == "a b needle"
    start, end = snippet.spans[0]
    assert snippet.text[start:end] == "needle"


def test_snippet_of_a_regex_match_is_highlighted():
    snippet = _snippet("code 4711 here", 0, 9, compile_matcher(r"code \d+", regex=True))
    start, end = snippet.spans[0]
    assert snippet.text[start:end] == "code 4711"


def test_a_query_matching_only_whitespace_yields_no_span():
    # Collapsing can erase the match entirely; the snippet is still returned.
    snippet = _snippet("a \n b", 1, 4, compile_matcher("\n"))
    assert snippet.text == "a b"
    assert snippet.spans == ()


# -- _collapse --


def test_collapse_maps_offsets_and_strips_edges():
    text, offsets = _collapse("  a \t b  ")
    assert text == "a b"
    # Every source index maps into the collapsed text, and the map has one
    # extra entry so a half-open end offset is addressable.
    assert len(offsets) == len("  a \t b  ") + 1
    assert max(offsets) <= len(text)


def test_collapse_of_all_whitespace_is_empty():
    text, offsets = _collapse("   \t\n  ")
    assert text == ""
    assert set(offsets) == {0}


# -- iter_hits --


def test_empty_log_list_yields_one_empty_batch():
    assert list(iter_hits([], compile_matcher("needle"))) == [(0, ())]


def test_batches_arrive_progressively(tmp_path):
    logs = [
        _simple_log(tmp_path / f"{i}.jsonl", "needle" if i % 2 else "hay")
        for i in range(10)
    ]
    batches = list(iter_hits(logs, compile_matcher("needle"), batch_size=4))
    # 4, 4, then the trailing 2.
    assert [scanned for scanned, _ in batches] == [4, 8, 10]
    assert sum(len(hits) for _, hits in batches) == 5


def test_final_partial_batch_is_not_duplicated(tmp_path):
    logs = [_simple_log(tmp_path / f"{i}.jsonl", "needle") for i in range(4)]
    batches = list(iter_hits(logs, compile_matcher("needle"), batch_size=2))
    assert [scanned for scanned, _ in batches] == [2, 4]
    assert sum(len(hits) for _, hits in batches) == 4


def test_cancellation_stops_the_scan(tmp_path):
    logs = [_simple_log(tmp_path / f"{i}.jsonl", "needle") for i in range(10)]
    calls = {"n": 0}

    def _cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    batches = list(
        iter_hits(
            logs, compile_matcher("needle"), batch_size=2, is_cancelled=_cancelled
        )
    )
    # Only the first full batch was yielded before cancellation bit.
    assert [scanned for scanned, _ in batches] == [2]


def test_hits_preserve_input_order(tmp_path):
    logs = [_simple_log(tmp_path / f"{i}.jsonl", "needle") for i in range(3)]
    hits = [h for _, batch in iter_hits(logs, compile_matcher("needle")) for h in batch]
    assert [h.session.jsonl_path for h in hits] == logs


def test_hit_exposes_parsed_session_metadata(tmp_path):
    log = _write_log(
        tmp_path / "abc.jsonl",
        [_entry("user", "needle", cwd="/repo/wt", gitBranch="worktree/x")],
    )
    hit = search_log(log, compile_matcher("needle"))
    assert hit is not None
    assert hit.session.cwd == Path("/repo/wt")
    assert hit.session.git_branch == "worktree/x"
    assert hit.session.session_id == "abc"


# -- Case sensitivity --


def test_case_insensitive_is_the_default():
    assert compile_matcher("HELLO").present_in("hello")
    assert compile_matcher("hello").present_in("HELLO")


def test_case_sensitive_literal():
    matcher = compile_matcher("Needle", case_sensitive=True)
    assert matcher.present_in("a Needle")
    assert not matcher.present_in("a needle")
    assert not matcher.present_in("a NEEDLE")


def test_case_sensitive_regex():
    matcher = compile_matcher(r"n.edle", regex=True, case_sensitive=True)
    assert matcher.present_in("needle")
    assert not matcher.present_in("NEEDLE")


def test_case_sensitivity_applies_to_a_whole_log(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "the NeEdLe is here")
    assert search_log(log, compile_matcher("needle")) is not None
    assert search_log(log, compile_matcher("needle", case_sensitive=True)) is None
    assert search_log(log, compile_matcher("NeEdLe", case_sensitive=True)) is not None


def test_case_sensitivity_composes_with_content_mode(tmp_path):
    log = _simple_log(tmp_path / "a.jsonl", "nothing relevant")
    # `gitBranch` is a JSON key: raw sees it, message text does not, and the
    # case flag applies independently of that.
    assert search_log(log, compile_matcher("gitbranch")) is not None
    assert search_log(log, compile_matcher("gitbranch", case_sensitive=True)) is None
    assert (
        search_log(log, compile_matcher("gitBranch", case_sensitive=True)) is not None
    )
    assert (
        search_log(
            log,
            compile_matcher("gitBranch", mode=ContentMode.TEXT, case_sensitive=True),
        )
        is None
    )


def test_case_sensitive_snippet_spans_cover_the_match(tmp_path):
    matcher = compile_matcher("Needle", case_sensitive=True)
    snippet = _snippet("say Needle and needle", 4, 10, matcher)
    # Only the correctly-cased occurrence is reported.
    assert [snippet.text[a:b] for a, b in snippet.spans] == ["Needle"]
