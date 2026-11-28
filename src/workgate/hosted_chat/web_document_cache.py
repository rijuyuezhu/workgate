"""Bounded owner-private public document cache and static fetch orchestration."""

import asyncio
import contextlib
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..app_paths import ensure_private_directory
from ..utils.private_files import atomic_write_private_bytes, private_file_lock
from .database import SQLiteDatabase
from .web_documents import (
    _MAX_PAGES,
    _MAX_SECTIONS,
    _MAX_TEXT,
    html_problem,
    parse_document,
    passage,
    validate_fetch,
)
from .web_render import render_html
from .web_research import WebResearchError, _download, _safe_url

_CACHE_TTL = 24 * 60 * 60
_MAX_FILES = 80
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_OWNER_BYTES = 128 * 1024 * 1024
_FETCH_TIMEOUT = 65


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _valid_document(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "url",
        "title",
        "text",
        "pages",
        "sections",
        "format",
    }:
        return False
    text, title = value["text"], value["title"]
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text.encode("utf-8")) > _MAX_TEXT
    ):
        return False
    if not isinstance(title, str) or not 1 <= len(title) <= 2048:
        return False
    if _safe_url(value["url"]) != value["url"]:
        return False
    kind = value["format"]
    pages = value["pages"]
    sections = value["sections"]
    if kind not in {"html", "text", "pdf"} or not isinstance(sections, list):
        return False
    if kind == "pdf":
        if (
            type(pages) is not int
            or not 1 <= pages <= _MAX_PAGES
            or len(sections) != pages
        ):
            return False
    elif pages is not None or len(sections) > _MAX_SECTIONS:
        return False
    previous = -1
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            return False
        offset = section.get("offset")
        if type(offset) is not int or not previous < offset < len(text):
            return False
        previous = offset
        if kind == "pdf":
            if set(section) != {"page", "offset"} or section["page"] != i + 1:
                return False
            marker = ("\n\n" if i else "") + f"[Page {i + 1}]\n"
            if not text.startswith(marker, offset):
                return False
        elif (
            set(section) != {"heading", "offset"}
            or not isinstance(section["heading"], str)
            or not text.startswith(section["heading"], offset)
        ):
            return False
    return kind != "html" or html_problem(value) is None


class WebDocumentCache:
    def __init__(self, database: SQLiteDatabase, owner: str):
        if not isinstance(owner, str) or not owner:
            raise ValueError("Document cache owner required")
        self.root = ensure_private_directory(
            ensure_private_directory(database.root / "web_documents")
            / _hash(owner)
        )
        self.owner = owner

    def _path(self, url: str) -> Path:
        return self.root / (_hash(url) + ".json")

    def load(self, url: str) -> dict | None:
        try:
            path = self._path(url)
            SQLiteDatabase._verify_private_file(path)
            if path.stat().st_mtime < time.time() - _CACHE_TTL:
                return None
            with path.open("rb") as stream:
                raw = stream.read(_MAX_FILE_BYTES + 1)
            if len(raw) > _MAX_FILE_BYTES:
                return None
            value = json.loads(raw)
            if (
                isinstance(value, dict)
                and value.get("owner") == _hash(self.owner)
                and value.get("request") == url
                and _valid_document(value.get("document"))
            ):
                return value["document"]
        except OSError, ValueError, TypeError, RecursionError, UnicodeError:
            pass
        return None

    def store(self, url: str, document: dict) -> None:
        if not _valid_document(document):
            return
        data = json.dumps(
            {"owner": _hash(self.owner), "request": url, "document": document},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(data) > _MAX_FILE_BYTES or len(data) > _MAX_OWNER_BYTES:
            return
        lock = self.root / "cache.lock"
        SQLiteDatabase._verify_private_file(lock, optional=True)
        with private_file_lock(lock, timeout_s=2):
            path = self._path(url)
            atomic_write_private_bytes(path, data)
            entries = []
            for entry in self.root.glob("*.json"):
                try:
                    SQLiteDatabase._verify_private_file(entry)
                    stat = entry.stat()
                    if stat.st_mtime < time.time() - _CACHE_TTL:
                        entry.unlink(missing_ok=True)
                    else:
                        entries.append((stat.st_mtime, stat.st_size, entry))
                except OSError:
                    continue
            entries.sort(reverse=True)
            total = 0
            for index, (_, size, entry) in enumerate(entries):
                total += size
                if index >= _MAX_FILES or total > _MAX_OWNER_BYTES:
                    with contextlib.suppress(OSError):
                        entry.unlink(missing_ok=True)


def _cache(database: SQLiteDatabase, owner: str) -> WebDocumentCache | None:
    try:
        return WebDocumentCache(database, owner)
    except OSError:
        return None


async def _retrieve(url: str) -> dict:
    try:
        resolved, mime, payload = await _download(url)
    except WebResearchError:
        parts = urlsplit(url)
        if parts.hostname not in {"arxiv.org", "www.arxiv.org"} or not (
            parts.path.startswith("/html/") or parts.path.startswith("/pdf/")
        ):
            raise
        alternative = "/pdf/" if parts.path.startswith("/html/") else "/html/"
        fallback = urlunsplit(
            (
                "https",
                "arxiv.org",
                alternative + parts.path.split("/", 2)[2],
                "",
                "",
            )
        )
        resolved, mime, payload = await _download(fallback)
    document = await asyncio.to_thread(parse_document, mime, payload, resolved)
    if document["format"] == "html":
        problem = html_problem(document)
        if problem:
            if "access denied" in problem:
                raise WebResearchError(problem)
            document = await render_html(resolved)
            if html_problem(document):
                raise WebResearchError(
                    "JavaScript rendering returned no readable content"
                )
    if not document["text"].strip():
        raise WebResearchError("Document has no extractable text")
    return document


async def web_fetch(
    database: SQLiteDatabase, owner: str, arguments: dict
) -> dict:
    url, query, offset, limit = validate_fetch(arguments)
    cache = await asyncio.to_thread(_cache, database, owner)
    document = (
        await asyncio.to_thread(cache.load, url) if cache is not None else None
    )
    cached = document is not None
    if document is None:
        try:
            async with asyncio.timeout(_FETCH_TIMEOUT):
                document = await _retrieve(url)
        except TimeoutError:
            raise WebResearchError(
                "Document reading exceeded its deadline"
            ) from None
        if cache is not None:
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.to_thread(cache.store, url, document)
    return passage(
        document, query=query, offset=offset, limit=limit, cached=cached
    )
