import importlib.util
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "validation"
        / "check-doc-reference-assets.py"
    )
    spec = importlib.util.spec_from_file_location(
        "check_doc_reference_assets", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_reference_assets_resolve_inside_built_site(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    site = tmp_path / "site"
    page = site / "reference/tools/index.html"
    target = site / "reference/generated/tools.json"
    page.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    page.write_text(
        '<div class="generated-reference" '
        'data-reference-json="../generated/tools.json"></div>',
        encoding="utf-8",
    )
    target.write_text('{"tools": []}\n', encoding="utf-8")

    assert checker.check_generated_reference_assets(site) == []
    assert checker.generated_reference_targets(site) == [
        (page.resolve(), "../generated/tools.json", target.resolve())
    ]


def test_generated_reference_assets_report_missing_target(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    site = tmp_path / "site"
    page = site / "reference/configuration/index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        '<div class="generated-reference" '
        'data-reference-json="../generated/configuration.json"></div>',
        encoding="utf-8",
    )

    errors = checker.check_generated_reference_assets(site)

    assert len(errors) == 1
    assert "configuration.json" in errors[0]
    assert "is missing" in errors[0]


def test_generated_reference_assets_reject_escape(tmp_path: Path) -> None:
    checker = _load_checker()
    site = tmp_path / "site"
    page = site / "reference/tools/index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        '<div class="generated-reference" '
        'data-reference-json="../../../../secret.json"></div>',
        encoding="utf-8",
    )

    errors = checker.check_generated_reference_assets(site)

    assert len(errors) == 1
    assert "escapes site root" in errors[0]
