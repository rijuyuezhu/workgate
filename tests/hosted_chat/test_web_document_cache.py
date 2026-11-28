"""Owner-private cache integrity, quotas and incremental web reads."""

import asyncio
import json
import os
import time

import pytest

from workgate.hosted_chat import web_document_cache as cache
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.web_documents import parse_document

URL = "https://example.org/paper"


def document(url=URL):
    return parse_document(
        "text/html",
        b"<html><title>Paper</title><h1>Introduction</h1><p>Text of a paper.</p></html>",
        url,
    )


def test_owner_private_cache_and_revalidation(tmp_path):
    db = SQLiteDatabase(tmp_path)
    alice = cache.WebDocumentCache(db, "alice")
    bob = cache.WebDocumentCache(db, "bob")
    assert alice.root != bob.root
    assert alice.load(URL) is None
    alice.store(URL, document())
    path = alice._path(URL)
    assert alice.load(URL) == document()
    assert bob.load(URL) is None
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert alice.root.stat().st_mode & 0o077 == 0
    raw = path.read_bytes()
    bob._path(URL).write_bytes(raw)
    if os.name != "nt":
        bob._path(URL).chmod(0o600)
    assert bob.load(URL) is None
    value = json.loads(raw)
    value["document"]["sections"][0]["offset"] = 3
    path.write_text(json.dumps(value), encoding="utf-8")
    assert alice.load(URL) is None
    path.write_bytes(raw)
    age = time.time() - cache._CACHE_TTL - 5
    os.utime(path, (age, age))
    assert alice.load(URL) is None


def test_cache_eviction_and_file_limits(tmp_path, monkeypatch):
    repo = cache.WebDocumentCache(SQLiteDatabase(tmp_path), "alice")
    monkeypatch.setattr(cache, "_MAX_FILES", 3)
    for index in range(5):
        url = f"https://example.org/{index}"
        repo.store(url, document(url))
        path = repo._path(url)
        now = time.time() - 100 + index
        os.utime(path, (now, now))
    assert len(list(repo.root.glob("*.json"))) == 3
    monkeypatch.setattr(cache, "_MAX_FILE_BYTES", 10)
    assert repo.load("https://example.org/4") is None
    repo.store(URL, document())
    assert not repo._path(URL).exists()
    monkeypatch.setattr(cache, "_MAX_FILE_BYTES", 4096)
    monkeypatch.setattr(cache, "_MAX_OWNER_BYTES", 500)
    repo.store(URL, document())
    assert sum(path.stat().st_size for path in repo.root.glob("*.json")) <= 500


@pytest.mark.parametrize(
    "patch",
    [
        {"url": "http://127.0.0.1/"},
        {"text": ""},
        {"pages": True},
        {"format": "unknown"},
        {"sections": [{"heading": "# fake", "offset": 0}]},
        {"sections": [{"heading": "", "offset": True}]},
        {"title": "Access denied"},
    ],
)
def test_cache_invalid_documents(tmp_path, patch):
    repo = cache.WebDocumentCache(SQLiteDatabase(tmp_path), "alice")
    repo.store(URL, document())
    path = repo._path(URL)
    value = json.loads(path.read_bytes())
    value["document"].update(patch)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert repo.load(URL) is None


@pytest.mark.asyncio
async def test_fetch_cache_owner_and_incremental_reads(tmp_path, monkeypatch):
    db = SQLiteDatabase(tmp_path)
    seen = []

    async def download(url):
        seen.append(url)
        return (
            url,
            "text/plain",
            ("Intro " + "x" * 120 + " Straße conclusion " + "y" * 120).encode(),
        )

    monkeypatch.setattr(cache, "_download", download)
    first = await cache.web_fetch(db, "alice", {"url": URL, "limit": 100})
    assert first["cached"] is False and first["next_offset"] == 100
    next_page = await cache.web_fetch(
        db, "alice", {"url": URL, "offset": 100, "limit": 100}
    )
    assert next_page["cached"] is True and next_page["offset"] == 100
    found = await cache.web_fetch(
        db, "alice", {"url": URL, "query": "STRASSE", "limit": 100}
    )
    assert "Straße" in found["content"]
    assert (await cache.web_fetch(db, "bob", {"url": URL}))["cached"] is False
    assert seen == [URL, URL]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "fallback",
        "challenge",
        "shell",
        "empty",
        "nonarxiv",
        "deadline",
        "cache_unavailable",
        "cache_write_failure",
    ],
)
async def test_fetch_errors_and_arxiv_fallback(tmp_path, monkeypatch, mode):
    db = SQLiteDatabase(tmp_path)
    seen = []
    url = "https://arxiv.org/abs/2501.00001" if mode == "fallback" else URL

    async def download(target):
        seen.append(target)
        if mode == "fallback" and "/html/" in target or mode == "nonarxiv":
            raise cache.WebResearchError("HTTP 404 fetching URL")
        if mode == "deadline":
            await asyncio.sleep(1)
        if mode == "challenge":
            return (
                target,
                "text/html",
                b"<html><title>Access denied</title><p>Challenge</p></html>",
            )
        if mode == "shell":
            return (
                target,
                "text/html",
                b"<html><p>Please enable JavaScript</p></html>",
            )
        return (
            target,
            "text/plain",
            b"" if mode == "empty" else b"Readable text",
        )

    monkeypatch.setattr(cache, "_download", download)

    async def fail_render(_url):
        assert mode == "shell"
        raise cache.WebResearchError("Rendering unavailable")

    monkeypatch.setattr(cache, "render_html", fail_render)
    if mode == "deadline":
        monkeypatch.setattr(cache, "_FETCH_TIMEOUT", 0.01)
    if mode == "cache_unavailable":

        def unavailable(*args):
            raise OSError("read only")

        monkeypatch.setattr(cache, "WebDocumentCache", unavailable)
    if mode == "cache_write_failure":

        def fail(*args):
            raise OSError("disk full")

        monkeypatch.setattr(cache.WebDocumentCache, "store", fail)
    if mode in {"fallback", "cache_unavailable", "cache_write_failure"}:
        result = await cache.web_fetch(db, "alice", {"url": url})
        assert result["content"] == "Readable text"
        if mode == "fallback":
            assert seen == [
                "https://arxiv.org/html/2501.00001",
                "https://arxiv.org/pdf/2501.00001",
            ]
        else:
            assert not result["cached"]
    else:
        with pytest.raises(cache.WebResearchError):
            await cache.web_fetch(db, "alice", {"url": url})
        assert len(seen) == 1


def test_pdf_cache_revalidates_page_markers(tmp_path):
    repo = cache.WebDocumentCache(SQLiteDatabase(tmp_path), "alice")
    text = "[Page 1]\nIntro\n\n[Page 2]\nConclusion"
    pdf = {
        "url": URL,
        "title": "PDF",
        "format": "pdf",
        "text": text,
        "pages": 2,
        "sections": [
            {"page": 1, "offset": 0},
            {"page": 2, "offset": text.index("[Page 2]") - 2},
        ],
    }
    repo.store(URL, pdf)
    assert repo.load(URL) == pdf
    path = repo._path(URL)
    value = json.loads(path.read_bytes())
    value["document"]["sections"][1]["offset"] += 1
    path.write_text(json.dumps(value), encoding="utf-8")
    assert repo.load(URL) is None
    value["document"]["sections"][1]["offset"] -= 1
    value["document"]["pages"] = 3
    path.write_text(json.dumps(value), encoding="utf-8")
    assert repo.load(URL) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX private file mode test")
def test_cache_rejects_nonprivate_and_symlink_files(tmp_path):
    repo = cache.WebDocumentCache(SQLiteDatabase(tmp_path), "alice")
    repo.store(URL, document())
    path = repo._path(URL)
    path.chmod(0o644)
    assert repo.load(URL) is None
    path.chmod(0o600)
    other = repo.root / "other.json"
    path.rename(other)
    path.symlink_to(other)
    assert repo.load(URL) is None


@pytest.mark.asyncio
async def test_rendered_shell_cached(tmp_path, monkeypatch):
    db = SQLiteDatabase(tmp_path)
    calls = []

    async def download(url):
        return url, "text/html", b"<html><p>Please enable JavaScript</p></html>"

    async def rendered(url):
        calls.append(url)
        return document(url)

    monkeypatch.setattr(cache, "_download", download)
    monkeypatch.setattr(cache, "render_html", rendered)
    first = await cache.web_fetch(db, "alice", {"url": URL})
    assert not first["cached"]
    assert "Text of a paper." in first["content"]
    assert (await cache.web_fetch(db, "alice", {"url": URL}))["cached"]
    assert calls == [URL]
