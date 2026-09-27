import pytest

import workgate.ui.http.terminal_protocol as protocol


def test_terminal_argument_validation_is_bounded() -> None:
    assert (
        protocol._bounded_int(
            None, default=10, minimum=1, maximum=20, label="lines"
        )
        == 10
    )
    assert (
        protocol._bounded_int(
            "12", default=10, minimum=1, maximum=20, label="lines"
        )
        == 12
    )
    with pytest.raises(ValueError, match="must be an integer"):
        protocol._bounded_int(
            "bad", default=10, minimum=1, maximum=20, label="lines"
        )
    with pytest.raises(ValueError, match="must be between"):
        protocol._bounded_int(
            0, default=10, minimum=1, maximum=20, label="lines"
        )

    assert protocol._executor_id_arg("executor-a") == "executor-a"
    assert protocol._shell_id("shell-1") == "shell-1"
    with pytest.raises(ValueError, match="shell_id must be"):
        protocol._shell_id("bad/id")


def test_terminal_inventory_normalization_rejects_duplicate_or_malformed_entries() -> (
    None
):
    normalized = protocol._normalize_list(
        "executor-a",
        {
            "shells": [
                {
                    "shell_id": "shell-a",
                    "name": "build",
                    "cwd": "/workspace",
                    "command": "/bin/sh",
                }
            ]
        },
    )
    assert normalized == {
        "executor_id": "executor-a",
        "shells": [
            {
                "shell_id": "shell-a",
                "name": "build",
                "cwd": "/workspace",
                "command": "/bin/sh",
            }
        ],
    }

    with pytest.raises(RuntimeError, match="duplicate terminal sessions"):
        protocol._normalize_list(
            "executor-a",
            {"shells": [{"shell_id": "dup"}, {"shell_id": "dup"}]},
        )
    with pytest.raises(RuntimeError, match="malformed terminal inventory"):
        protocol._normalize_list(
            "executor-a", {"shells": [{"shell_id": "bad id"}]}
        )
    with pytest.raises(RuntimeError, match="malformed terminal inventory"):
        protocol._normalize_list("executor-a", {"not_shells": []})


def test_terminal_start_read_and_kill_normalization_use_current_rest_contract() -> (
    None
):
    assert protocol._normalize_start(
        "executor-a",
        {
            "shell_id": "shell-a",
            "name": None,
            "cwd": "/workspace",
            "command": "/bin/sh",
        },
    ) == {
        "executor_id": "executor-a",
        "shell_id": "shell-a",
        "name": None,
        "cwd": "/workspace",
        "command": "/bin/sh",
    }

    assert protocol._normalize_read(
        "executor-a",
        "shell-a",
        50,
        {"shell_id": "shell-a", "output": "hello"},
    ) == {
        "executor_id": "executor-a",
        "shell_id": "shell-a",
        "output": "hello",
        "lines": 50,
    }
    with pytest.raises(RuntimeError, match="malformed terminal read data"):
        protocol._normalize_read(
            "executor-a",
            "shell-a",
            50,
            {"shell_id": "other", "output": "hello"},
        )

    assert protocol._normalize_kill(
        "executor-a",
        "shell-a",
        {"shell_id": "shell-a", "killed": True, "stderr": None},
    ) == {
        "executor_id": "executor-a",
        "shell_id": "shell-a",
        "killed": True,
        "stderr": None,
    }
    with pytest.raises(RuntimeError, match="malformed terminal kill data"):
        protocol._normalize_kill(
            "executor-a",
            "shell-a",
            {"shell_id": "other", "killed": True},
        )
