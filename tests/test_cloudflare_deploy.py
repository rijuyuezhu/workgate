from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from workgate.hosted._tool_manifest import HOSTED_TOOL_MANIFEST

_ROOT = Path(__file__).resolve().parents[1]


def _load_script(relative: str, name: str) -> ModuleType:
    path = _ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_cloudflare_entry(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    workers = ModuleType("workers")
    workers.__dict__.update(
        {
            "DurableObject": type("DurableObject", (), {}),
            "Response": type("Response", (), {}),
            "WorkerEntrypoint": type("WorkerEntrypoint", (), {}),
        }
    )
    monkeypatch.setitem(sys.modules, "workers", workers)
    return _load_script(
        "deploy/cloudflare/src/entry.py", "workgate_cloudflare_entry"
    )


class _Chunk:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def to_bytes(self) -> bytes:
        return self._data


def _body(*chunks: bytes):
    async def iterator():
        for chunk in chunks:
            yield _Chunk(chunk)

    return iterator()


def test_cloudflare_request_body_reader_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _load_cloudflare_entry(monkeypatch)
    request = SimpleNamespace(body=_body(b"abc", b"def"))
    assert asyncio.run(entry._read_bounded_text(request, 6)) == "abcdef"

    oversized = SimpleNamespace(body=_body(b"abc", b"def"))
    with pytest.raises(entry._RequestBodyTooLarge):
        asyncio.run(entry._read_bounded_text(oversized, 5))

    invalid_utf8 = SimpleNamespace(body=_body(b"\xff"))
    with pytest.raises(UnicodeDecodeError):
        asyncio.run(entry._read_bounded_text(invalid_utf8, 8))


def test_cloudflare_prepare_stages_exact_dependency_light_closure(
    tmp_path: Path,
) -> None:
    prepare = _load_script(
        "deploy/cloudflare/prepare.py", "workgate_cloudflare_prepare"
    )
    dest = tmp_path / "workgate"
    copied = prepare.stage(dest)
    assert (
        tuple(path.relative_to(dest).as_posix() for path in copied)
        == prepare.MODULES
    )
    assert (dest / "hosted/http.py").is_file()
    assert (dest / "hosted/mcp.py").is_file()
    assert not (dest / "executor").exists()
    assert not (dest / "control/mcp").exists()
    assert not (dest / "tools").exists()


def test_cloudflare_prepare_updates_in_place_and_cleans_stale_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = _load_script(
        "deploy/cloudflare/prepare.py", "workgate_cloudflare_prepare_in_place"
    )
    dest = tmp_path / "workgate"
    prepare.stage(dest)
    package_init = dest / "__init__.py"
    stale = dest / "stale.py"
    stale.write_text("old\n", encoding="utf-8")

    def fail_tree_removal(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stage must not remove the live package tree")

    monkeypatch.setattr(prepare.shutil, "rmtree", fail_tree_removal)
    prepare.stage(dest)

    assert package_init.is_file()
    assert not stale.exists()


def test_cloudflare_tool_manifest_matches_canonical_mcp_definitions() -> None:
    generator = _load_script(
        "scripts/generation/generate-hosted-tool-manifest.py",
        "workgate_hosted_tool_manifest_generator",
    )
    generated = asyncio.run(generator.build_manifest())
    assert tuple(generated) == HOSTED_TOOL_MANIFEST


def test_cloudflare_manifest_generator_rejects_unvalidated_schema_keywords() -> (
    None
):
    generator = _load_script(
        "scripts/generation/generate-hosted-tool-manifest.py",
        "workgate_hosted_tool_manifest_schema_guard",
    )
    with pytest.raises(
        RuntimeError, match="unsupported hosted MCP schema keyword"
    ):
        generator._validate_input_schema(
            {"type": "string", "enum": ["one"]}, path="test"
        )
    with pytest.raises(
        RuntimeError, match="unsupported hosted MCP schema keyword"
    ):
        generator._validate_input_schema(
            {"$ref": "#/$defs/SomeInput"}, path="test"
        )


def test_cloudflare_wrangler_declares_sqlite_actor_and_owner_secret() -> None:
    config = json.loads(
        (_ROOT / "deploy/cloudflare/wrangler.jsonc").read_text(encoding="utf-8")
    )
    assert config["compatibility_flags"] == ["python_workers"]
    assert config["exports"]["WorkgateControl"] == {
        "type": "durable-object",
        "storage": "sqlite",
    }
    assert config["durable_objects"]["bindings"] == [
        {"class_name": "WorkgateControl", "name": "WORKGATE_CONTROL"}
    ]
    assert config["secrets"]["required"] == ["WORKGATE_OWNER_TOKEN"]


def test_cloudflare_generated_and_secret_files_are_ignored() -> None:
    ignored = (_ROOT / "deploy/cloudflare/.gitignore").read_text(
        encoding="utf-8"
    )
    for path in (
        ".venv/",
        ".venv-workers/",
        "python_modules/",
        "node_modules/",
        "src/workgate/",
        "dist*/",
        ".wrangler/",
        ".dev.vars*",
        ".env*",
        ".secrets-*.json",
        ".curl-*.cfg",
        ".mcp-*.py",
    ):
        assert path in ignored


def test_cloudflare_sdist_uses_explicit_deployment_allowlist() -> None:
    config = tomllib.loads(
        (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    included = set(config["tool"]["uv"]["build-backend"]["source-include"])
    cloudflare = {
        path for path in included if path.startswith("deploy/cloudflare/")
    }
    assert cloudflare == {
        "deploy/cloudflare/.gitignore",
        "deploy/cloudflare/AGENTS.md",
        "deploy/cloudflare/README.md",
        "deploy/cloudflare/package.json",
        "deploy/cloudflare/package-lock.json",
        "deploy/cloudflare/prepare.py",
        "deploy/cloudflare/pyproject.toml",
        "deploy/cloudflare/pylock.toml",
        "deploy/cloudflare/src/entry.py",
        "deploy/cloudflare/uv.lock",
        "deploy/cloudflare/wrangler.jsonc",
    }
