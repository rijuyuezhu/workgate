import pytest

from workgate.tools.mcp_text import render_tool_text


@pytest.mark.parametrize(
    ("name", "structured", "expected"),
    [
        ("read", {"content": "[a#1]\n1:x"}, "[a#1]\n1:x"),
        (
            "search",
            {"numbered_content": "[a#1]\n1:x", "stderr": "", "count": 1},
            "[a#1]\n1:x",
        ),
        (
            "search",
            {"numbered_content": "", "stderr": "", "count": 0},
            "No matches.",
        ),
        (
            "tree_view",
            {"entries": ["src/", "  app.py"], "truncated": True},
            "src/\n  app.py\n[tree truncated]",
        ),
        (
            "tree_view",
            {
                "entries": [],
                "message": "Path does not exist: missing/project",
                "nearest_existing_parent": ".",
                "nearest_parent_entries": ["actual/", "README.md"],
                "nearest_parent_entries_truncated": False,
            },
            (
                "Path does not exist: missing/project\n"
                "Nearest existing parent: .\n"
                "actual/\nREADME.md"
            ),
        ),
        ("glob_search", {"paths": ["a.py", "b.py"]}, "a.py\nb.py"),
        (
            "edit_lines",
            {"context": {"numbered_content": "[a#2]\n1:y"}},
            "[a#2]\n1:y",
        ),
        (
            "hashline_edit",
            {
                "hunks": [
                    {"context": {"numbered_content": "[a#2]\n1:y"}},
                    {"context": {"numbered_content": "[b#3]\n4:z"}},
                ]
            },
            "[a#2]\n1:y\n\n[b#3]\n4:z",
        ),
        (
            "bash",
            {
                "mode": "command",
                "result": {
                    "stdout": "ok\n",
                    "stderr": "warn\n",
                    "exit_code": 7,
                    "timed_out": False,
                    "truncated": False,
                },
            },
            "ok\n[stderr]\nwarn\n[exit code 7]",
        ),
        (
            "run_python_code",
            {
                "mode": "job",
                "result": {"job_id": "job_1", "status": "running"},
            },
            "Job job_1 (running).",
        ),
        (
            "bash",
            {
                "mode": "pty",
                "result": {"shell_id": "sh_1"},
            },
            "Persistent shell sh_1 started.",
        ),
        (
            "read_persistent_shell_output",
            {"output": "terminal\n"},
            "terminal\n",
        ),
        (
            "job",
            {
                "operation": "poll",
                "outputs": [
                    {
                        "job": {"job_id": "job_1", "status": "succeeded"},
                        "output": "done\n",
                        "message": "job completed",
                    }
                ],
            },
            "[job_1 succeeded]\ndone\njob completed",
        ),
        (
            "job",
            {
                "operation": "poll",
                "outputs": [
                    {
                        "job": {
                            "job_id": "job_managed",
                            "status": "succeeded",
                            "progress": {"phase": "copying", "bytes": 7},
                            "result": {"bytes": 10, "sha256": "abc"},
                            "error": "",
                        },
                        "output": "",
                        "message": "job completed with exit code 0",
                    }
                ],
            },
            (
                "[job_managed succeeded]\n"
                "job completed with exit code 0\n"
                '[result]\n{"bytes": 10, "sha256": "abc"}'
            ),
        ),
        (
            "job",
            {
                "operation": "poll",
                "outputs": [
                    {
                        "job": {
                            "job_id": "job_running",
                            "status": "running",
                            "progress": {"phase": "copying", "bytes": 7},
                            "result": None,
                            "error": None,
                        },
                        "output": "",
                        "message": None,
                    }
                ],
            },
            (
                "[job_running running]\n"
                '[progress]\n{"bytes": 7, "phase": "copying"}'
            ),
        ),
        (
            "job",
            {
                "operation": "poll",
                "outputs": [
                    {
                        "job": {
                            "job_id": "job_failed",
                            "status": "failed",
                            "error": "RuntimeError: copy failed",
                        },
                        "output": "",
                        "message": "job completed with exit code 1",
                    }
                ],
            },
            (
                "[job_failed failed]\n"
                "job completed with exit code 1\n"
                "[error]\nRuntimeError: copy failed"
            ),
        ),
        (
            "job",
            {
                "operation": "list",
                "jobs": [
                    {"job_id": "job_1", "status": "running", "name": "build"}
                ],
                "message": "Executor jobs unavailable while the session is not active.",
            },
            (
                "Executor jobs unavailable while the session is not active.\n"
                "job_1\trunning\tbuild"
            ),
        ),
        (
            "job",
            {
                "operation": "cancel",
                "cancelled": [
                    {
                        "job": {"job_id": "job_1"},
                        "killed": True,
                        "stderr": "cleanup warning",
                    }
                ],
            },
            "job_1: stopped\n[stderr]\ncleanup warning",
        ),
        (
            "job",
            {
                "operation": "retry",
                "retried": [{"job_id": "job_2", "status": "running"}],
            },
            "job_2: running",
        ),
        (
            "activate_agent_skill",
            {
                "content": "# Skill\n",
                "related_files": ["README.md", "references/guide.md"],
            },
            ("# Skill\n\n[related files]\nREADME.md\nreferences/guide.md"),
        ),
        ("read_agent_skill_file", {"content": "guide"}, "guide"),
        ("fetch", {"text": "document"}, "document"),
        (
            "session_start",
            {
                "session_id": "sess_1",
                "workdir": "/workspace/repo",
                "message": "Reuse this session_id.",
            },
            "Session sess_1 ready in /workspace/repo.\nReuse this session_id.",
        ),
        (
            "session_change_workdir",
            {
                "session_id": "sess_1",
                "workdir": "/workspace/other",
                "message": "",
            },
            "Session sess_1 ready in /workspace/other.",
        ),
    ],
)
def test_render_tool_text(name, structured, expected):
    assert render_tool_text(name, structured) == expected


def test_render_tool_text_falls_back_for_unlisted_or_malformed_results():
    assert render_tool_text("version", {"version": "1"}) is None
    assert render_tool_text("bash", {"unexpected": True}) is None
    assert render_tool_text("read", {"content": 1}) is None
