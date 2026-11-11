"""Produce reviewable world candidates from text, then validate explicit edits.

This module never accesses a world database or starts a game/model process.
Source text is untrusted evidence, not a prompt or gameplay authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Mapping

from realm.worldpack import WorldpackError, validate_worldpack


DRAFT_SCHEMA = "realm.creator.draft.v1"
APPROVAL_SCHEMA = "realm.creator.approval.v1"
MAX_FILES = 8
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_PROPOSALS = 64
MAX_EXCERPT_CHARS = 600
MAX_SECTION_CHARS = 4096
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,99}\.(?:txt|md|markdown)$")
_HEADING = re.compile(r"^#{1,3}[ \t]+(.+?)\s*#*\s*$")
_PLAIN_HEADING = re.compile(
    r"^(?:chapter|part|section)\s+"
    r"(?:\d{1,3}(?:\.\d{1,3})?|[ivxlcdm]{1,12}|"
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|twenty)"
    r"(?:\s*(?:[:.\-\u2013\u2014])\s*\S.*|\s+\S.*)?$",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROPOSAL_ID = re.compile(r"^src-[0-9a-f]{16}$")


class CreatorDraftError(ValueError):
    """A source, approval or provenance boundary was rejected."""


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise CreatorDraftError("Input is not a bounded JSON-compatible document") from exc
    return hashlib.sha256(encoded).hexdigest()


def _exact_object(value: object, required: set[str], context: str) -> dict:
    if not isinstance(value, dict) or set(value) != required:
        raise CreatorDraftError(f"{context} must contain exactly {sorted(required)}")
    return value


def _safe_source_text(text: str) -> bool:
    """Reject invisibly formatted, bidi-reordered and control-spoofed input."""
    return all(char in "\n\r\t" or unicodedata.category(char) not in
               {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"} for char in text)


def _validate_draft(draft: object) -> dict:
    """Check untrusted persisted draft shape before dereferencing its fields."""
    obj = _exact_object(draft, {"schema", "world_id", "name", "status", "sources",
                                "proposals", "worldpack", "live_world_modified", "sha256"}, "draft")
    if (obj["schema"] != DRAFT_SCHEMA or obj["status"] != "REVIEW_REQUIRED" or
            obj["worldpack"] is not None or obj["live_world_modified"] is not False):
        raise CreatorDraftError("Draft is not an unreviewed, unimported Creator proposal")
    if (not isinstance(obj["world_id"], str) or not isinstance(obj["name"], str) or
            not isinstance(obj["sha256"], str) or not _SHA256.fullmatch(obj["sha256"])):
        raise CreatorDraftError("Invalid draft identity or digest")
    if not isinstance(obj["sources"], list) or not 1 <= len(obj["sources"]) <= MAX_FILES:
        raise CreatorDraftError("Invalid source manifest")
    seen_names: set[str] = set()
    sha_set: set[str] = set()
    total = 0
    for index, record in enumerate(obj["sources"]):
        source = _exact_object(record, {"name", "sha256", "bytes"}, f"sources[{index}]")
        if (not isinstance(source["name"], str) or not _FILE.fullmatch(source["name"]) or
                source["name"] in seen_names or not isinstance(source["sha256"], str) or
                not _SHA256.fullmatch(source["sha256"]) or
                type(source["bytes"]) is not int or not 1 <= source["bytes"] <= MAX_FILE_BYTES):
            raise CreatorDraftError(f"Invalid sources[{index}] manifest record")
        seen_names.add(source["name"])
        sha_set.add(source["sha256"])
        total += source["bytes"]
    if total > MAX_TOTAL_BYTES:
        raise CreatorDraftError("Draft source manifest exceeds the intake cap")
    if not isinstance(obj["proposals"], list) or not 1 <= len(obj["proposals"]) <= MAX_PROPOSALS:
        raise CreatorDraftError("Draft proposals must be a bounded nonempty list")
    proposal_ids: set[str] = set()
    for index, record in enumerate(obj["proposals"]):
        section = _exact_object(record, {"proposal_id", "heading", "excerpt", "excerpt_truncated",
                                         "provenance", "status", "room_candidate"},
                                f"proposals[{index}]")
        if (not isinstance(section["proposal_id"], str) or
                not _PROPOSAL_ID.fullmatch(section["proposal_id"]) or
                section["proposal_id"] in proposal_ids or
                not isinstance(section["heading"], str) or
                not 1 <= len(section["heading"]) <= 100 or
                not _safe_source_text(section["heading"]) or
                not isinstance(section["excerpt"], str) or
                len(section["excerpt"]) > MAX_EXCERPT_CHARS or
                not _safe_source_text(section["excerpt"]) or
                type(section["excerpt_truncated"]) is not bool or
                section["status"] != "UNREVIEWED" or section["room_candidate"] is not False):
            raise CreatorDraftError(f"Invalid proposals[{index}] section")
        provenance = _exact_object(section["provenance"],
                                   {"source_sha256", "start_line", "end_line"},
                                   f"proposals[{index}].provenance")
        if (not isinstance(provenance["source_sha256"], str) or
                provenance["source_sha256"] not in sha_set or
                type(provenance["start_line"]) is not int or
                type(provenance["end_line"]) is not int or
                not 1 <= provenance["start_line"] <= provenance["end_line"] <= 1000000):
            raise CreatorDraftError(f"Invalid proposals[{index}] source reference")
        proposal_ids.add(section["proposal_id"])
    unsigned = {key: value for key, value in obj.items() if key != "sha256"}
    if obj["sha256"] != _digest(unsigned):
        raise CreatorDraftError("Draft content differs from its recorded digest")
    return obj


def _heading_label(line: str) -> str | None:
    """Recognize explicit Markdown or plain DOCX-extraction chapter headings."""
    markdown = _HEADING.fullmatch(line)
    if markdown:
        return markdown.group(1).strip()[:100]
    stripped = line.strip()
    if 1 <= len(stripped) <= 100 and _PLAIN_HEADING.fullmatch(stripped):
        return stripped
    return None


def _section_chunks(lines: list[str], label: str, body_start_line: int,
                    *, heading_line: int | None = None) -> list[tuple[str, str, int, int]]:
    """Chunk on whole lines, preserving the source's inclusive line ranges.

    Exceptional individual long source lines are not split without a column
    provenance schema. They remain reviewable through their original line.
    """
    if not lines:
        line = heading_line or body_start_line
        return [(label[:100], "", line, line)]
    chunks: list[tuple[str, int, int]] = []
    start = 0
    chars = 0
    for index, line in enumerate(lines):
        width = len(line) + 1
        if chars and chars + width > MAX_SECTION_CHARS:
            chunks.append(("\n".join(lines[start:index]).strip(), start, index - 1))
            start = index
            chars = 0
        chars += width
    chunks.append(("\n".join(lines[start:]).strip(), start, len(lines) - 1))
    result = []
    for index, (content, first, last) in enumerate(chunks):
        chunk_label = label[:100] if len(chunks) == 1 else (
            f"{label[:70]} (segment {index + 1} of {len(chunks)})")
        first_line = (heading_line if index == 0 and heading_line is not None
                      else body_start_line + first)
        result.append((chunk_label, content, first_line, body_start_line + last))
    return result


def _sections(text: str) -> list[tuple[str, str, int, int]]:
    """Reviewable bounded sections; never presumed to be rooms or canon."""
    lines = text.splitlines()
    headings = [(index, label) for index, line in enumerate(lines)
                if (label := _heading_label(line)) is not None]
    if not headings:
        return _section_chunks(lines, "Unclassified source", 1)
    results: list[tuple[str, str, int, int]] = []
    if headings[0][0] > 0 and "\n".join(lines[:headings[0][0]]).strip():
        results.extend(_section_chunks(lines[:headings[0][0]], "Unclassified preamble", 1))
    for index, (line_index, label) in enumerate(headings):
        next_index = (headings[index + 1][0] if index + 1 < len(headings)
                      else len(lines))
        results.extend(_section_chunks(lines[line_index + 1:next_index], label,
                                       line_index + 2, heading_line=line_index + 1))
    return results


def prepare_draft(world_id: str, name: str, sources: Mapping[str, bytes]) -> dict:
    """Return a content-hashed review draft; never infer canonical truth or rooms."""
    # Reuse the canonical engine's ID/name validator without touching SQLite.
    try:
        validate_worldpack({"schema": "realm.worldpack.v1", "world_id": world_id,
                            "name": name, "start_room": "unpublished",
                            "rooms": [{"id": "unpublished", "name": "Unpublished",
                                       "description": "Unpublished draft", "exits": {}}]})
    except WorldpackError as exc:
        raise CreatorDraftError(str(exc)) from exc
    if not isinstance(sources, Mapping) or not 1 <= len(sources) <= MAX_FILES:
        raise CreatorDraftError(f"Provide 1-{MAX_FILES} UTF-8 .txt/.md/.markdown source files")
    if any(not isinstance(filename, str) for filename in sources):
        raise CreatorDraftError("Source names must be basename-only strings")

    records: list[dict] = []
    proposals: list[dict] = []
    total = 0
    seen = set()
    for filename, contents in sorted(sources.items()):
        if not isinstance(filename, str) or not _FILE.fullmatch(filename) or filename in seen:
            raise CreatorDraftError("Source names must be unique, basename-only .txt/.md/.markdown files")
        seen.add(filename)
        if not isinstance(contents, bytes) or not 1 <= len(contents) <= MAX_FILE_BYTES:
            raise CreatorDraftError(f"Source {filename!r} must have 1-{MAX_FILE_BYTES} bytes")
        total += len(contents)
        if total > MAX_TOTAL_BYTES:
            raise CreatorDraftError("Combined source bytes exceed the bounded intake cap")
        try:
            decoded = contents.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CreatorDraftError(f"Source {filename!r} must be UTF-8 text") from exc
        if not _safe_source_text(decoded):
            raise CreatorDraftError(f"Source {filename!r} contains prohibited control/format characters")
        if not decoded.strip():
            raise CreatorDraftError(f"Source {filename!r} contains no nonblank text")
        source_sha = hashlib.sha256(contents).hexdigest()
        records.append({"name": filename, "sha256": source_sha, "bytes": len(contents)})
        for heading, section, first, last in _sections(decoded):
            if len(proposals) >= MAX_PROPOSALS:
                raise CreatorDraftError(f"More than {MAX_PROPOSALS} source sections; split the import")
            proposal_id = "src-" + _digest([filename, source_sha, first, last, heading])[:16]
            proposals.append({
                "proposal_id": proposal_id,
                "heading": heading,
                "excerpt": section[:MAX_EXCERPT_CHARS],
                "excerpt_truncated": len(section) > MAX_EXCERPT_CHARS,
                "provenance": {"source_sha256": source_sha, "start_line": first, "end_line": last},
                "status": "UNREVIEWED",
                "room_candidate": False,
            })
    draft = {"schema": DRAFT_SCHEMA, "world_id": world_id, "name": name,
             "status": "REVIEW_REQUIRED", "sources": records, "proposals": proposals,
             "worldpack": None, "live_world_modified": False}
    draft["sha256"] = _digest(draft)
    return draft


def compile_approved_worldpack(draft: dict, approval: dict) -> dict:
    """Compile host-reviewed room edits to a validated, *unimported* worldpack.

    The host/user approval represented by this JSON is NOT authenticated here;
    caller integration must enforce identity, authority and approval custody.
    """
    draft = _validate_draft(draft)
    _exact_object(approval, {"schema", "draft_sha256", "world_id", "name", "start_room",
                             "rooms", "items", "acknowledged_review"}, "approval")
    if approval["schema"] != APPROVAL_SCHEMA or approval["acknowledged_review"] is not True:
        raise CreatorDraftError("Exact schema and explicit host review acknowledgement are required")
    if (approval["draft_sha256"] != draft["sha256"] or
            approval["world_id"] != draft["world_id"] or approval["name"] != draft["name"]):
        raise CreatorDraftError("Approval does not match this source revision/world")
    proposals = {proposal["proposal_id"] for proposal in draft["proposals"]}
    if not isinstance(approval["rooms"], list) or not approval["rooms"]:
        raise CreatorDraftError("Approval must contain at least one authored room")
    rooms = []
    lineage_rooms: dict[str, list[str]] = {}
    for index, room in enumerate(approval["rooms"]):
        _exact_object(room, {"id", "name", "description", "exits", "source_proposal_ids"},
                      f"rooms[{index}]")
        ids = room["source_proposal_ids"]
        if (not isinstance(ids, list) or not ids or
                any(not isinstance(item, str) or item not in proposals for item in ids) or
                len(ids) != len(set(ids))):
            raise CreatorDraftError(f"rooms[{index}] needs one or more valid source proposal IDs")
        rooms.append({key: room[key] for key in ("id", "name", "description", "exits")})
        if isinstance(room["id"], str):
            lineage_rooms[room["id"]] = list(ids)
    if not isinstance(approval["items"], list):
        raise CreatorDraftError("Approval items must be a list")
    items = []
    lineage_items: dict[str, list[str]] = {}
    for index, item in enumerate(approval["items"]):
        _exact_object(item, {"id", "name", "description", "room", "source_proposal_ids"},
                      f"items[{index}]")
        ids = item["source_proposal_ids"]
        if (not isinstance(ids, list) or not ids or
                any(not isinstance(part, str) or part not in proposals for part in ids) or
                len(ids) != len(set(ids))):
            raise CreatorDraftError(f"items[{index}] needs one or more valid source proposal IDs")
        items.append({key: item[key] for key in ("id", "name", "description", "room")})
        if isinstance(item["id"], str):
            lineage_items[item["id"]] = list(ids)
    try:
        pack = validate_worldpack({"schema": "realm.worldpack.v1",
                                   "world_id": approval["world_id"], "name": approval["name"],
                                   "start_room": approval["start_room"],
                                   "rooms": rooms, "items": items})
    except WorldpackError as exc:
        raise CreatorDraftError(str(exc)) from exc
    return {"schema": "realm.creator.compilation.v1", "draft_sha256": draft["sha256"],
            "approval_sha256": _digest(approval), "worldpack": pack,
            "worldpack_sha256": _digest(pack),
            "lineage": {"source_link_status": "UNVERIFIED_CITATION",
                        "semantic_relation": "NOT_ENTAILMENT",
                        "rooms": lineage_rooms, "items": lineage_items},
            "world_imported": False, "requires_separate_host_import": True,
            "approval_authenticated": False}
