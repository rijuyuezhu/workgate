"""Pinned upstream context extraction tests, adapted to native errors."""

import sys
from types import SimpleNamespace

import pytest

from workgate.hosted_chat.context_extraction import (
    ContextExtractionError,
    extract_context_text,
)


def test_context_rejects_unreadable_and_empty_text(tmp_path):
    path = tmp_path / "binary.txt"
    path.write_bytes(b"\xff\xfe\xff")
    with pytest.raises(ContextExtractionError) as err:
        extract_context_text(path, path.name, "text/plain")
    assert err.value.status_code == 415
    path.write_text("  ")
    with pytest.raises(ContextExtractionError) as err:
        extract_context_text(path, path.name, "text/plain")
    assert err.value.status_code == 422
    path.write_text("x" * (128 * 1024 + 1))
    with pytest.raises(ContextExtractionError) as err:
        extract_context_text(path, path.name, "text/plain")
    assert err.value.status_code == 413


def test_context_optional_pdf_docx_fail_closed_when_dependency_absent(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    monkeypatch.setitem(sys.modules, "docx", None)
    path = tmp_path / "file.pdf"
    path.write_bytes(b"%PDF")
    with pytest.raises(ContextExtractionError) as err:
        extract_context_text(path, "file.pdf", "application/pdf")
    assert err.value.status_code == 501
    path = tmp_path / "file.docx"
    path.write_bytes(b"invalid")
    with pytest.raises(ContextExtractionError) as err:
        extract_context_text(
            path,
            "file.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    assert err.value.status_code == 501


def test_real_optional_docx_extraction(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Source-based document content")
    path = tmp_path / "source.docx"
    document.save(path)
    result = extract_context_text(
        path,
        "source.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result["processed"] is True
    assert result["extraction"] == "docx"
    assert "Source-based document content" in result["extracted_text"]


def test_real_optional_pdf_extraction(tmp_path):
    pytest.importorskip("pypdf")
    # A minimal valid single-page PDF with an actual extractable text operator.
    stream = b"BT /F1 12 Tf 72 720 Td (Source PDF text is extractable) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
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
        f"trailer\n<< /Size {len(positions)} /Root 1 0 R >>\n"
        f"startxref\n{marker}\n%%EOF".encode(),
    )
    path = tmp_path / "source.pdf"
    path.write_bytes(data)
    result = extract_context_text(path, "source.pdf", "application/pdf")
    assert result["processed"] is True
    assert result["extraction"] == "pypdf"
    assert "Source PDF text is extractable" in result["extracted_text"]


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("pages", 413),
        ("text_limit", 413),
        ("parser_failure", 422),
        ("no_text", 422),
    ],
)
def test_pdf_context_defensively_bounds_page_count_bytes_and_errors(
    tmp_path, monkeypatch, kind, expected
):
    """Use a minimal reader double to cover failure boundaries independently of optional pypdf."""
    path = tmp_path / "data.pdf"
    path.write_bytes(b"%PDF")

    class Page:
        def extract_text(self):
            if kind == "parser_failure":
                raise RuntimeError("broken page")
            if kind == "text_limit":
                return "a" * (128 * 1024 + 1)
            if kind == "no_text":
                return None
            return "sample"

    def reader(_path):
        return SimpleNamespace(pages=[Page()] * (65 if kind == "pages" else 1))

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=reader))
    with pytest.raises(ContextExtractionError) as error:
        extract_context_text(path, "data.pdf", "application/pdf")
    assert error.value.status_code == expected


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("text_limit", 413),
        ("parser_failure", 422),
        ("no_text", 422),
    ],
)
def test_docx_context_defensively_bounds_bytes_and_errors(
    tmp_path, monkeypatch, kind, expected
):
    path = tmp_path / "data.docx"
    path.write_bytes(b"invalid document")

    def document(_path):
        if kind == "parser_failure":
            raise RuntimeError("broken document")
        value = "a" * (128 * 1024 + 1) if kind == "text_limit" else ""
        return SimpleNamespace(paragraphs=[SimpleNamespace(text=value)])

    monkeypatch.setitem(sys.modules, "docx", SimpleNamespace(Document=document))
    with pytest.raises(ContextExtractionError) as error:
        extract_context_text(
            path,
            "data.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    assert error.value.status_code == expected


def test_context_enforces_binary_rejection_and_raw_text_size_before_read(
    tmp_path,
):
    path = tmp_path / "payload.txt"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ContextExtractionError) as error:
        extract_context_text(path, path.name, "text/plain")
    assert error.value.status_code == 413
    path = tmp_path / "payload.png"
    path.write_bytes(b"image")
    with pytest.raises(ContextExtractionError) as error:
        extract_context_text(path, path.name, "image/png")
    assert error.value.status_code == 501


@pytest.mark.parametrize(
    "name,mime",
    [
        ("reference.TXT", "application/octet-stream"),
        ("reference", "text/plain; charset=utf-8"),
        ("reference", "application/json"),
    ],
)
def test_text_context_accepts_supported_utf8_and_bom(tmp_path, name, mime):
    path = tmp_path / "payload"
    path.write_text("source 中文", encoding="utf-8-sig")
    assert extract_context_text(path, name, mime) == {
        "extracted_text": "source 中文",
        "extraction": "utf8",
        "processed": True,
    }


@pytest.mark.parametrize(
    "module,name,constructor",
    [("pypdf", "a.pdf", "PdfReader"), ("docx", "a.docx", "Document")],
)
def test_joined_text_limit_counts_separators(
    tmp_path, monkeypatch, module, name, constructor
):
    path = tmp_path / name
    path.write_bytes(b"test")
    text = "x" * (64 * 1024)
    pages = [SimpleNamespace(extract_text=lambda: text) for _ in range(2)]
    paragraphs = [SimpleNamespace(text=text) for _ in range(2)]
    monkeypatch.setitem(
        sys.modules,
        module,
        SimpleNamespace(
            **{
                constructor: lambda _: SimpleNamespace(
                    pages=pages, paragraphs=paragraphs
                )
            }
        ),
    )
    with pytest.raises(ContextExtractionError) as error:
        extract_context_text(path, name, "application/octet-stream")
    assert error.value.status_code == 413
