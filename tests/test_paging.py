from __future__ import annotations

import pytest

from fujimoto.paging import DEFAULT, PageStep, parse_page_step


class TestParsePageStep:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("50%", PageStep("percent", 50)),
            (" 25 % ", PageStep("percent", 25)),
            ("100%", PageStep("percent", 100)),
            ("1%", PageStep("percent", 1)),
            ("8", PageStep("rows", 8)),
            (8, PageStep("rows", 8)),
            ("1", PageStep("rows", 1)),
        ],
    )
    def test_valid(self, value: object, expected: PageStep) -> None:
        assert parse_page_step(value) == expected

    @pytest.mark.parametrize(
        "value",
        ["", "%", "0%", "101%", "0", "-3", "0.5", "half", "5 rows", True, 2.5, None],
    )
    def test_invalid(self, value: object) -> None:
        with pytest.raises(ValueError, match="page_step"):
            parse_page_step(value)

    def test_round_trips_through_text(self) -> None:
        for text in ("50%", "8"):
            assert str(parse_page_step(text)) == text

    def test_default_is_half_a_page(self) -> None:
        assert DEFAULT == PageStep("percent", 50)
