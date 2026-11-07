"""Local, review-only source candidate extraction for REALM Creator.

No model prompts, canonical worldpack generation, SQLite changes or network I/O.
An exact source mention is evidence of text, never authority for its truth.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

SCHEMA = "realm.creator.candidate_review.v1"
MAX_SOURCE_BYTES = 1024 * 1024
MAX_DOCX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 128
MAX_COMPRESSION_RATIO = 80
MAX_CANDIDATES = 256
MAX_EVIDENCE_PER_CANDIDATE = 8
W_P = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
W_T = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
LABEL = re.compile(
    r"^\s*(?:#{1,6}\s+)?"
    r"(planet|station|ship|species|race|faction|location|item|technology)"
    r"\s*:\s*(.*?)\s*$", re.IGNORECASE
)
CATEGORIES = {"race": "species"}


class CandidateError(ValueError):
    """Input was unsafe, malformed or exceeded a bounded offline review limit."""


def _docx_paragraphs(source: bytes) -> list[str]:
    try:
        with ZipFile(BytesIO(source)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
                raise CandidateError("DOCX member count is invalid")
            seen_names: set[str] = set()
            for info in infos:
                name = info.filename
                normalized_name = PurePosixPath(name).as_posix().casefold()
                if normalized_name in seen_names:
                    raise CandidateError("DOCX archive has duplicate member names")
                seen_names.add(normalized_name)
                parts = PurePosixPath(name).parts
                if (
                    not name or name.startswith("/") or "\\" in name
                    or any(part in {".", ".."} for part in parts)
                    or ":" in name or info.file_size > MAX_DOCX_MEMBER_BYTES
                    or (info.file_size > 0 and info.compress_size == 0)
                    or (
                        info.compress_size > 0 and
                        info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                    )
                    or ((info.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise CandidateError("DOCX archive contains unsafe member")
            try:
                member = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise CandidateError("DOCX lacks word/document.xml") from exc
            with archive.open(member) as handle:
                xml = handle.read(MAX_DOCX_MEMBER_BYTES + 1)
            if len(xml) > MAX_DOCX_MEMBER_BYTES:
                raise CandidateError("DOCX document XML is oversized")
    except BadZipFile as exc:
        raise CandidateError("Invalid DOCX archive") from exc
    # Decode before scanning: a raw ASCII-byte check misses UTF-16 DTDs.
    # DOCX review intentionally supports UTF-8 XML only; other encodings
    # must be explicitly reviewed before becoming an input capability.
    try:
        xml_text = xml.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CandidateError("DOCX document XML must be UTF-8") from exc
    if "\x00" in xml_text:
        raise CandidateError("DOCX document XML contains NUL or unsupported encoding")
    header = xml_text.lstrip()
    if header.startswith("<?xml"):
        declaration = header.split("?>", 1)[0]
        encoding = re.search(r"\bencoding\s*=\s*(['\"])(.*?)\1", declaration, re.IGNORECASE)
        if encoding and encoding.group(2).casefold().replace("_", "-") not in {"utf-8", "utf8"}:
            raise CandidateError("DOCX document XML encoding must be UTF-8")
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise CandidateError("DOCX XML declarations are not accepted")
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError) as exc:
        raise CandidateError("Invalid DOCX document XML") from exc
    return [
        "".join(span.text or "" for span in paragraph.iter(W_T))
        for paragraph in root.iter(W_P)
    ]


def preview_source(path: str | Path) -> dict:
    """Return bounded private/local candidates, never a playable worldpack.

    Labels such as 'Planet: Example' identify PROPOSED review candidates.
    Evidence SOURCE_CONFIRMED means the exact line occurs in these bytes;
    it does not mean a canon or lore assertion has been source-confirmed.
    """
    file = Path(path)
    ext = file.suffix.casefold()
    if ext not in {".txt", ".md", ".docx"}:
        raise CandidateError("Only TXT, MD, and DOCX local sources are supported")
    if not file.is_file() or file.stat().st_size > MAX_SOURCE_BYTES:
        raise CandidateError("Input must be a regular source within byte limit")
    source = file.read_bytes()
    if len(source) > MAX_SOURCE_BYTES:
        raise CandidateError("Input exceeds byte limit")
    if ext == ".docx":
        lines = _docx_paragraphs(source)
        unit, fmt = "paragraph", "docx"
    else:
        try:
            text = source.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CandidateError("TXT/MD must be UTF-8") from exc
        if "\x00" in text:
            raise CandidateError("Text source contains NUL")
        lines = text.splitlines()
        unit, fmt = "line", ext[1:]
    candidates: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    for ordinal, raw_line in enumerate(lines, 1):
        match = LABEL.fullmatch(raw_line)
        if match is None:
            continue
        category = CATEGORIES.get(match[1].casefold(), match[1].casefold())
        name = unicodedata.normalize("NFC", match[2]).strip()
        if (
            not 1 <= len(name) <= 100
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            continue
        key = (category, name.casefold())
        if key not in index:
            if len(candidates) >= MAX_CANDIDATES:
                raise CandidateError("Too many candidate names for bounded review")
            candidate = {
                "id": f"candidate-{len(candidates) + 1:04d}",
                "category": category,
                "name": name,
                "status": "PROPOSED",
                "review_status": "UNREVIEWED",
                "evidence": [],
            }
            index[key] = candidate
            candidates.append(candidate)
        evidence = index[key]["evidence"]
        if len(evidence) < MAX_EVIDENCE_PER_CANDIDATE:
            evidence.append({
                "unit": unit,
                "ordinal": ordinal,
                "text": raw_line,
                "source_status": "SOURCE_CONFIRMED",
                "line_sha256": hashlib.sha256(raw_line.encode("utf-8")).hexdigest(),
            })
    return {
        "schema": SCHEMA,
        "source": {
            "format": fmt,
            "sha256": hashlib.sha256(source).hexdigest(),
            "bytes": len(source),
            "custody": "PRIVATE_LOCAL_REVIEW",
        },
        "candidates": candidates,
        "boundaries": {
            "navigation": "UNKNOWN",
            "pressure_hatches": "UNKNOWN",
            "rights": "UNVERIFIED",
            "canon": "UNAPPROVED",
            "worldpack_release": "HELD",
        },
        "effect": "REVIEW_ONLY_NO_IMPORT",
    }


def main(argv: list[str] | None = None) -> int:
    """Explicit local preview only: JSON may contain private source quotes."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Review labeled local source mentions (no world import)")
    parser.add_argument("--private-local-preview", action="store_true", required=True)
    parser.add_argument("source", help="Explicit local TXT, MD, or DOCX path")
    args = parser.parse_args(argv)
    try:
        result = preview_source(args.source)
    except (CandidateError, OSError) as exc:
        parser.error(f"Source rejected: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
