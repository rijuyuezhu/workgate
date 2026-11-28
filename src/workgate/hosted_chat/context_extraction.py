"""Bounded document text extraction, adapted from upstream v6.0.3
(f0ecf8f8039345f24d0de4402983766787f4574e), webui/attachments.py.
"""

from pathlib import Path


class ContextExtractionError(ValueError):
    """Safe extraction error, mapped to the HTTP boundary by Control."""

    def __init__(self, status_code: int, *, detail: str):
        super().__init__(detail)
        self.status_code = status_code


def extract_context_text(path: Path, name: str, content_type: str) -> dict:
    """Explicitly process *supported* Agent context uploads.

    Plain text is native. PDF and DOCX extraction are optional, imported only
    for matching formats. Scanned pages, vector indexing and code deployments
    require separate processors; never mark those files processed here.
    """
    mime = content_type.lower().split(";")[0].strip()
    extension = Path(name).suffix.lower()
    if (
        mime.startswith("text/")
        or mime
        in {"application/json", "application/xml", "application/javascript"}
        or extension
        in {
            ".txt",
            ".md",
            ".csv",
            ".json",
            ".py",
            ".js",
            ".ts",
            ".go",
            ".rs",
            ".yaml",
            ".yml",
        }
    ):
        if path.stat().st_size > 1024 * 1024:
            raise ContextExtractionError(
                413, detail="Agent text context exceeds 1 MiB"
            )
        try:
            value = path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise ContextExtractionError(
                415, detail="Agent text context must be UTF-8"
            ) from exc
        method = "utf8"
    elif mime == "application/pdf" or extension == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ContextExtractionError(
                501, detail="PDF extraction requires the hosted-chat extra"
            ) from exc
        try:
            reader = PdfReader(path)
            if len(reader.pages) > 64:
                raise ContextExtractionError(
                    413, detail="PDF context exceeds 64 pages"
                )
            snippets = []
            total = 0
            for page in reader.pages:
                snippet = page.extract_text() or ""
                total += len(snippet.encode("utf-8")) + bool(snippets)
                if total > 128 * 1024:
                    raise ContextExtractionError(
                        413, detail="Extracted Agent context exceeds 128 KiB"
                    )
                snippets.append(snippet)
            value = "\n".join(snippets)
        except ContextExtractionError:
            raise
        except Exception as exc:
            raise ContextExtractionError(
                422, detail="PDF text extraction failed"
            ) from exc
        method = "pypdf"
    elif (
        mime
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or extension == ".docx"
    ):
        try:
            from docx import Document
        except ImportError as exc:
            raise ContextExtractionError(
                501, detail="DOCX extraction requires the hosted-chat extra"
            ) from exc
        try:
            paragraphs = []
            total = 0
            for paragraph in Document(str(path)).paragraphs:
                total += len(paragraph.text.encode("utf-8")) + bool(paragraphs)
                if total > 128 * 1024:
                    raise ContextExtractionError(
                        413, detail="Extracted Agent context exceeds 128 KiB"
                    )
                paragraphs.append(paragraph.text)
            value = "\n".join(paragraphs)
        except ContextExtractionError:
            raise
        except Exception as exc:
            raise ContextExtractionError(
                422, detail="DOCX text extraction failed"
            ) from exc
        method = "docx"
    else:
        raise ContextExtractionError(
            501,
            detail="Agent context extraction is unsupported for this file type",
        )
    if not value.strip():
        raise ContextExtractionError(
            422,
            detail="File contains no extractable text; OCR is not configured",
        )
    if len(value.encode("utf-8")) > 128 * 1024:
        raise ContextExtractionError(
            413, detail="Extracted Agent context exceeds 128 KiB"
        )
    return {"extracted_text": value, "extraction": method, "processed": True}
