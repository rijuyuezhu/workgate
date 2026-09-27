from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_scripts_root_contains_only_ownership_readme() -> None:
    root_files = {
        path.name
        for path in (_REPO_ROOT / "scripts").iterdir()
        if path.is_file()
    }
    assert root_files == {"README.md"}
