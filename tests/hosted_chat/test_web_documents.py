"""Static public document parsing and passage boundaries."""

import contextlib
import sys
from types import SimpleNamespace

import pytest

from workgate.hosted_chat import web_documents as docs

URL = "https://example.org/paper"


def test_readable_html_and_sections():
    data = b"<html><head><title>A paper</title><style>hide</style></head><body><nav>menu</nav><h1>Introduction</h1><p>Readable &amp; useful.</p><script>secret</script><svg/><h2>Methods</h2><p>Method text.</p></body></html>"
    result = docs.parse_document("text/html", data, URL)
    assert result["title"] == "A paper"
    assert result["format"] == "html"
    assert "Readable & useful." in result["text"]
    assert "menu" not in result["text"] and "secret" not in result["text"]
    assert [s["heading"] for s in result["sections"]] == [
        "# Introduction",
        "## Methods",
    ]
    for section in result["sections"]:
        assert result["text"][section["offset"] :].startswith(
            section["heading"]
        )
    assert docs.html_problem(result) is None


def test_encoding_and_limits(monkeypatch):
    html = '<html><meta charset="gb2312"><title>论文</title><p>研究成果</p></html>'.encode(
        "gbk"
    )
    result = docs.parse_document("text/html", html, URL)
    assert result["title"] == "论文" and "研究成果" in result["text"]
    assert (
        docs.parse_document(
            "text/plain; charset=gbk", "中文".encode("gbk"), URL
        )["text"]
        == "中文"
    )
    assert (
        docs.parse_document("text/plain; charset=x-unknown", b"fallback", URL)[
            "text"
        ]
        == "fallback"
    )
    with pytest.raises(docs.WebResearchError, match="binary"):
        docs.parse_document("application/octet-stream", b"\x00\xff", URL)
    monkeypatch.setattr(docs, "_MAX_TEXT", 10)
    with pytest.raises(docs.WebResearchError, match="text limit"):
        docs.parse_document("text/plain", "汉汉汉汉".encode(), URL)
    with pytest.raises(docs.WebResearchError, match="text limit"):
        docs.parse_document("text/html", b"<p>long body content</p>", URL)


@pytest.mark.parametrize(
    "title,text,expected",
    [
        ("Access denied", "check", "access denied"),
        ("Page", "Loading...", "no extractable"),
        ("Page", "", "no extractable"),
        ("Page", "Please enable JavaScript", "requires JavaScript"),
        ("Page", "A short readable page", None),
    ],
)
def test_html_problem(title, text, expected):
    result = docs.html_problem({"title": title, "text": text})
    if expected is None:
        assert result is None
    else:
        assert expected in result


def _pdf_bytes(stream):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    positions = [0]
    for index, content in enumerate(objects, 1):
        positions.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + content + b"\nendobj\n")
    marker = len(data)
    data.extend(f"xref\n0 {len(positions)}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for offset in positions[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(positions)} /Root 1 0 R >>\nstartxref\n{marker}\n%%EOF".encode()
    )
    return bytes(data)


def test_real_optional_pdf_and_scan_only():
    pytest.importorskip("pypdf")
    raw = _pdf_bytes(
        b"BT /F1 12 Tf 72 720 Td (Source PDF text is extractable) Tj ET"
    )
    result = docs.parse_document("application/pdf", raw, URL + ".pdf")
    assert result["pages"] == 1 and result["sections"] == [
        {"page": 1, "offset": 0}
    ]
    assert "Source PDF text is extractable" in result["text"]
    with pytest.raises(docs.WebResearchError, match="OCR"):
        docs.parse_document("application/pdf", _pdf_bytes(b""), URL + ".pdf")
    with pytest.raises(docs.WebResearchError, match="Unable to parse"):
        docs.parse_document(
            "application/pdf", b"%PDF-1.4 invalid", URL + ".pdf"
        )


@pytest.mark.parametrize(
    "mode", ["missing", "pages", "text", "error", "success"]
)
def test_pdf_budget_and_optional_dependency(monkeypatch, mode):
    def reader(*args, **kwargs):
        if mode == "error":
            raise RuntimeError("bad")
        pages = [
            SimpleNamespace(extract_text=lambda **kw: "First page"),
            SimpleNamespace(extract_text=lambda **kw: "Second page"),
        ]
        return SimpleNamespace(pages=[None] * 501 if mode == "pages" else pages)

    monkeypatch.setitem(
        sys.modules,
        "pypdf",
        None
        if mode == "missing"
        else SimpleNamespace(
            PdfReader=reader,
            apply_configuration=lambda **kw: contextlib.nullcontext(),
        ),
    )
    if mode == "text":
        monkeypatch.setattr(docs, "_MAX_TEXT", 10)
    if mode == "success":
        result = docs.parse_document(
            "application/pdf", b"%PDF-1.4", URL + ".pdf"
        )
        assert result["pages"] == 2
        assert (
            result["sections"][1]["offset"]
            == result["text"].index("[Page 2]") - 2
        )
        tail = docs.passage(
            result,
            query=None,
            offset=result["text"].index("[Page 2]"),
            limit=100,
            cached=True,
        )
        assert tail["page"] == 2 and "Second page" in tail["content"]
    else:
        match = {
            "missing": "requires",
            "pages": "500 page",
            "text": "text limit",
            "error": "Unable to parse",
        }[mode]
        with pytest.raises(docs.WebResearchError, match=match):
            docs.parse_document("application/pdf", b"%PDF-1.4", URL + ".pdf")


@pytest.mark.parametrize(
    "args",
    [
        {"url": None},
        {"url": URL, "offset": -1},
        {"url": URL, "offset": True},
        {"url": URL, "limit": 99},
        {"url": URL, "limit": 12001},
        {"url": URL, "query": " "},
        {"url": URL, "query": 2},
        {"url": URL, "extra": True},
    ],
    ids=["url", "offset", "bool", "small", "large", "blank", "query", "extra"],
)
def test_invalid_fetch_args(args):
    with pytest.raises(docs.WebResearchError):
        docs.validate_fetch(args)


def test_arxiv_and_unicode_passage():
    assert docs.validate_fetch({"url": "https://arxiv.org/abs/2501.01234"}) == (
        "https://arxiv.org/html/2501.01234",
        None,
        0,
        8000,
    )
    text = "ß" * 500 + "Straße " + "x" * 200
    document = {
        "url": URL,
        "title": "Test",
        "text": text,
        "pages": None,
        "sections": [],
    }
    found = docs.passage(
        document, query="STRASSE", offset=0, limit=100, cached=True
    )
    assert "Straße" in found["content"] and found["offset"] < 500
    assert found["next_offset"] == found["offset"] + 100
    assert found["cached"] is True
    assert (
        docs.passage(
            document, query="absent", offset=0, limit=100, cached=False
        )["found"]
        is False
    )
    end = docs.passage(
        document, query=None, offset=len(text) + 1, limit=100, cached=False
    )
    assert end["content"] == "" and end["next_offset"] is None
