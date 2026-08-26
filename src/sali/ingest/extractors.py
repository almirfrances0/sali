"""Pull plain text out of a file. Text formats are stdlib; PDF uses pypdf if it's installed,
otherwise the PDF path reports 'needs_pypdf' cleanly (every other format still works)."""

from __future__ import annotations

from pathlib import Path

# Read as text: source, config, data, docs, logs — and extensionless files (READMEs, dotfiles).
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".sql", ".sh", ".bash", ".html", ".htm", ".xml",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".c", ".h", ".cpp", ".hpp", ".go", ".rs", ".java", ".rb",
}
_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB cap — this box is RAM-constrained (§ env), don't slurp a huge file


def extract_text(path: Path) -> tuple[str, str]:
    """Return (text, status). status ∈ ok | empty | unsupported | error | needs_pypdf."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix and suffix not in _TEXT_SUFFIXES:
        return "", "unsupported"
    try:
        raw = path.read_bytes()[:_MAX_BYTES]
    except OSError:
        return "", "error"
    text = raw.decode("utf-8", "replace").strip()
    return (text, "ok") if text else ("", "empty")


def _extract_pdf(path: Path) -> tuple[str, str]:
    try:
        import pypdf  # type: ignore[import-not-found]  # optional — [optional-dependencies].pdf
    except ImportError:
        return "", "needs_pypdf"
    try:
        reader = pypdf.PdfReader(str(path))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:  # noqa: BLE001 - a corrupt/encrypted PDF is a soft failure, not a crash
        return "", "error"
    return (text, "ok") if text else ("", "empty")
