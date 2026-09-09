from dataclasses import fields
from pathlib import Path

from workgate.executor.search.core import (
    ParsedSearchMatch,
    SearchConfig,
    SearchDisplayWindow,
    SearchWindowMatch,
    build_rg_argv,
    clamp_search_result_limit,
    display_windows,
    line_in_ranges,
    merge_line_ranges,
    parse_rg_match_record,
    parse_search_line_range,
    split_line_scoped_search_path,
)


def test_search_config_contains_only_runner_settings() -> None:
    assert [field.name for field in fields(SearchConfig)] == [
        "rg_bin",
        "max_results",
        "max_output_bytes",
    ]


def test_clamp_search_result_limit_uses_configured_positive_ceiling() -> None:
    assert clamp_search_result_limit(None, 200) == 200
    assert clamp_search_result_limit(0, 200) == 200
    assert clamp_search_result_limit(-5, 200) == 1
    assert clamp_search_result_limit(25, 200) == 25
    assert clamp_search_result_limit(500, 200) == 200


def test_build_rg_argv_keeps_query_after_option_terminator() -> None:
    config = SearchConfig("custom-rg", 200, 4096)

    argv = build_rg_argv(
        config,
        "-needle",
        path_args=["src", "tests"],
        glob_args=["*.py"],
        glob="!generated/*",
        regex=False,
        case_sensitive=False,
        gitignore=False,
    )

    assert argv == [
        "custom-rg",
        "--json",
        "--line-number",
        "--column",
        "--no-ignore",
        "--fixed-strings",
        "--ignore-case",
        "--glob",
        "*.py",
        "--glob",
        "!generated/*",
        "--",
        "-needle",
        "src",
        "tests",
    ]


def test_build_rg_argv_defaults_to_current_directory_scope() -> None:
    config = SearchConfig("rg", 200, 4096)

    argv = build_rg_argv(
        config,
        "needle",
        path_args=[],
        glob_args=[],
        glob=None,
        regex=True,
        case_sensitive=True,
        gitignore=True,
    )

    assert argv[-3:] == ["--", "needle", "."]


def test_parse_rg_match_record_projects_match_fields() -> None:
    record = (
        b'{"type":"match","data":{"path":{"text":"src/app.py"},'
        b'"lines":{"text":"hello needle\\n"},"line_number":7,'
        b'"submatches":[{"start":6,"end":12}]}}'
    )

    assert parse_rg_match_record(record) == ParsedSearchMatch(
        path="src/app.py",
        line=7,
        column=7,
        text="hello needle",
    )


def test_parse_rg_match_record_ignores_non_matches_and_invalid_json() -> None:
    assert parse_rg_match_record(b'{"type":"begin","data":{}}') is None
    assert parse_rg_match_record(b"not-json") is None
    assert parse_rg_match_record(b"[]") is None


def test_search_line_selector_parsing_and_filtering() -> None:
    assert parse_search_line_range("10") == (10, None)
    assert parse_search_line_range("10-20") == (10, 20)
    assert parse_search_line_range("10+3") == (10, 12)
    assert parse_search_line_range("10+") is None
    assert split_line_scoped_search_path("src/app.py:2-4,8") == (
        "src/app.py",
        ((2, 4), (8, None)),
    )
    assert line_in_ranges(3, ((2, 4), (8, None))) is True
    assert line_in_ranges(6, ((2, 4), (8, None))) is False


def test_invalid_search_line_selector_is_rejected() -> None:
    try:
        split_line_scoped_search_path("src/app.py:10+")
    except ValueError as exc:
        assert str(exc) == "invalid search line selector: 10+"
    else:
        raise AssertionError("malformed line selector was accepted")


def test_merge_line_ranges_coalesces_adjacent_and_open_ended_ranges() -> None:
    assert merge_line_ranges(((5, 7), (1, 2), (3, 4), (9, None), (8, 8))) == (
        (1, None),
    )


def test_display_windows_merge_context_and_respect_line_scopes() -> None:
    windows = display_windows(
        [
            SearchWindowMatch("src/app.py", 2, None),
            SearchWindowMatch("src/app.py", 4, None),
            SearchWindowMatch("src/other.py", 10, ((10, 10),)),
        ],
        "/workspace",
        radius=1,
    )
    workspace = Path("/workspace")

    assert windows == [
        SearchDisplayWindow(
            read_path=str(workspace / "src" / "app.py"),
            start=1,
            end=5,
            match_lines=frozenset({2, 4}),
        ),
        SearchDisplayWindow(
            read_path=str(workspace / "src" / "other.py"),
            start=10,
            end=10,
            match_lines=frozenset({10}),
        ),
    ]
