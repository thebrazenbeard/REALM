"""Bounded, in-memory provenance JSONL -> review-only Creator candidates.

Input is untrusted supplied metadata and excerpts, *not* original source bytes.
Original source hashes/statuses are copied as claims, never reverified or
promoted to rights, literary canon, import, or game-world release authority.
No filesystem, network, SQLite, or model access occurs in this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from .creator_offline import CandidateError, CATEGORIES, SCHEMA

CHUNK_SCHEMA = "realm.creator.provenance_chunk.v1"
MAX_JSONL_BYTES = 256 * 1024
MAX_LINE_BYTES = 4096
MAX_RECORDS = 256
MAX_CANDIDATES = 128
MAX_EVIDENCE_PER_CANDIDATE = 8
MAX_SOURCE_REVISIONS = 64
MAX_CONFLICTS = 256
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_KIND = re.compile(r"^[A-Z][A-Z0-9_]{2,39}$")
_CATEGORIES = frozenset({
    "planet", "station", "ship", "species", "race", "faction",
    "location", "item", "technology",
})
_SOURCE_STATUSES = frozenset({
    "SOURCE_CONFIRMED", "METADATA_ONLY", "UNVERIFIED", "DISPUTED",
})
_CUSTODY_STATUSES = frozenset({
    "HASH_MATCH_REPORTED", "METADATA_ONLY", "UNVERIFIED",
})


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CandidateError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise CandidateError("Non-finite JSON number is not permitted")


def _fields(value: object, required: set[str], *, optional: set[str] = frozenset()) -> dict:
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - optional:
        raise CandidateError("Unsupported or missing provenance fields")
    return value


def _safe_text(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or (not value and not allow_empty):
        raise CandidateError("Invalid provenance text length or type")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value):
        raise CandidateError("Control or formatting characters in provenance text")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _HEX.fullmatch(value) is None:
        raise CandidateError("Source/evidence SHA-256 must be lowercase hex")
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise CandidateError("Invalid source identity or revision")
    return value


def _count(value: object, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise CandidateError("Invalid bounded source size or ordinal")
    return value


def preview_jsonl_chunks(payload: bytes | str) -> dict:
    """Convert an explicitly supplied UTF-8 JSONL batch to unapproved review.

    This is a declared adapter contract, **not** evidence of compatibility with
    any unpublished Driftspace-specific JSONL producer. A caller must map its
    source-private schema to CHUNK_SCHEMA and review custody/rights separately.
    """
    if type(payload) is str:
        try:
            supplied = payload.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise CandidateError("Invalid Unicode in JSONL") from exc
    elif type(payload) is bytes:
        supplied = payload
    else:
        raise CandidateError("JSONL must be supplied in memory as bytes or text")
    if not supplied or len(supplied) > MAX_JSONL_BYTES:
        raise CandidateError("JSONL batch is empty or exceeds byte limit")
    try:
        source_text = supplied.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CandidateError("JSONL must be UTF-8") from exc
    if source_text.startswith("\ufeff") or "\x00" in source_text:
        raise CandidateError("JSONL BOM or NUL not supported")
    lines = source_text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if not 1 <= len(lines) <= MAX_RECORDS:
        raise CandidateError("JSONL record count exceeds bounded review")

    sources: dict[tuple[str, str], dict] = {}
    by_name: dict[tuple[str, str], dict] = {}
    first_supplied_name: dict[tuple[str, str], str] = {}
    evidence_owners: dict[tuple[str, str, str, int], tuple[str, tuple[str, str]]] = {}
    seen_records: set[str] = set()
    candidates = []
    conflicts = []

    def add_conflict(kind: str, **fields: object) -> None:
        if len(conflicts) >= MAX_CONFLICTS:
            raise CandidateError("Too many conflicts for bounded review")
        conflicts.append({"kind": kind, **fields})

    for line in lines:
        if not line or len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise CandidateError("Empty or oversized JSONL physical record")
        try:
            record = json.loads(
                line, object_pairs_hook=_unique_object,
                parse_constant=_invalid_constant,
            )
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise CandidateError("Invalid provenance JSONL record") from exc
        _fields(record, {"schema", "source", "candidate", "evidence", "conflicts"})
        if record["schema"] != CHUNK_SCHEMA:
            raise CandidateError("Unsupported provenance chunk schema")
        fingerprint = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if fingerprint in seen_records:
            raise CandidateError("Duplicate provenance record")
        seen_records.add(fingerprint)

        source = _fields(record["source"], {
            "id", "revision", "raw_sha256", "raw_bytes", "custody_status",
        }, optional={"parent_revision"})
        source_id = _identifier(source["id"])
        revision = _identifier(source["revision"])
        parent_revision = source.get("parent_revision")
        if parent_revision is not None:
            _identifier(parent_revision)
            if parent_revision == revision:
                raise CandidateError("Source revision cannot be its own parent")
        raw_sha = _digest(source["raw_sha256"])
        raw_bytes = _count(source["raw_bytes"], maximum=2**31 - 1)
        custody = source["custody_status"]
        if type(custody) is not str or custody not in _CUSTODY_STATUSES:
            raise CandidateError("Unsupported source custody claim")
        binding_key = (source_id, revision)
        binding = {
            "id": source_id, "revision": revision, "raw_sha256": raw_sha,
            "raw_bytes": raw_bytes, "reported_custody_status": custody,
            "reported_parent_revision": parent_revision,
            "originals_verified_by_bridge": False,
        }
        if binding_key in sources and sources[binding_key] != binding:
            raise CandidateError("Conflicting hash or custody for same source revision")
        if binding_key not in sources:
            if len(sources) >= MAX_SOURCE_REVISIONS:
                raise CandidateError("Too many source revisions")
            for prior in sources.values():
                if prior["id"] == source_id and prior["raw_sha256"] != raw_sha:
                    add_conflict("REVISION_VARIANT_REVIEW", source_id=source_id,
                                 revisions=[prior["revision"], revision])
            sources[binding_key] = binding

        candidate = _fields(record["candidate"], {"category", "name", "status"})
        category = candidate["category"]
        if type(category) is not str or category not in _CATEGORIES:
            raise CandidateError("Unsupported candidate category")
        category = CATEGORIES.get(category, category)
        supplied_name = _safe_text(candidate["name"], 100)
        name = unicodedata.normalize("NFC", supplied_name).strip()
        if not name:
            raise CandidateError("Empty candidate name")
        if candidate["status"] != "PROPOSED":
            raise CandidateError("Only PROPOSED semantic candidates may enter review")
        name_key = (category, name.casefold())
        item = by_name.get(name_key)
        if item is None:
            if len(candidates) >= MAX_CANDIDATES:
                raise CandidateError("Too many candidates")
            item = {
                "id": f"candidate-{len(candidates) + 1:04d}",
                "category": category, "name": name,
                "status": "PROPOSED", "review_status": "UNREVIEWED",
                "evidence": [],
            }
            by_name[name_key] = item
            first_supplied_name[name_key] = supplied_name
            candidates.append(item)
        elif first_supplied_name[name_key] != supplied_name:
            add_conflict("NAME_ALIAS_COLLISION_REVIEW", category=category,
                         names=[first_supplied_name[name_key], supplied_name])

        evidence = _fields(record["evidence"], {
            "unit", "ordinal", "text", "text_sha256", "source_status",
        })
        unit = evidence["unit"]
        if unit not in ("line", "paragraph"):
            raise CandidateError("Unsupported source evidence unit")
        ordinal = _count(evidence["ordinal"], maximum=2**31 - 1)
        excerpt = _safe_text(evidence["text"], 1024)
        excerpt_sha = _digest(evidence["text_sha256"])
        if hashlib.sha256(excerpt.encode("utf-8")).hexdigest() != excerpt_sha:
            raise CandidateError("Evidence text SHA-256 mismatch")
        reported_status = evidence["source_status"]
        if type(reported_status) is not str or reported_status not in _SOURCE_STATUSES:
            raise CandidateError("Unsupported source status claim")
        evidence_key = (source_id, revision, unit, ordinal)
        prior_evidence = evidence_owners.get(evidence_key)
        if prior_evidence is not None:
            if prior_evidence[0] != excerpt_sha:
                raise CandidateError("Conflicting text at same source revision and position")
            if prior_evidence[1] == name_key:
                raise CandidateError("Duplicate source evidence position")
            add_conflict("EVIDENCE_ASSIGNMENT_CONFLICT", source_id=source_id,
                         revision=revision, unit=unit, ordinal=ordinal)
        else:
            evidence_owners[evidence_key] = (excerpt_sha, name_key)
        if len(item["evidence"]) >= MAX_EVIDENCE_PER_CANDIDATE:
            raise CandidateError("Too many evidence positions for candidate")
        item["evidence"].append({
            "unit": unit, "ordinal": ordinal, "text": excerpt,
            "text_sha256": excerpt_sha, "line_sha256": excerpt_sha,
            "source_status": "SUPPLIED_NOT_REVERIFIED",
            "reported_source_status": reported_status,
            "source_id": source_id, "revision": revision,
            "raw_sha256": raw_sha,
        })

        reported_conflicts = record["conflicts"]
        if type(reported_conflicts) is not list or len(reported_conflicts) > 4:
            raise CandidateError("Invalid reported conflicts list")
        for reported in reported_conflicts:
            _fields(reported, {"kind", "detail"})
            kind, detail = reported["kind"], _safe_text(reported["detail"], 200)
            if type(kind) is not str or _KIND.fullmatch(kind) is None:
                raise CandidateError("Invalid reported conflict kind")
            add_conflict(kind, source_id=source_id, revision=revision,
                         candidate_id=item["id"], detail=detail,
                         status="SUPPLIED_UNRESOLVED")

    # A reported revision parent is merely a claim, not authenticated lineage.
    # Still, any cycle *within this supplied batch* is an observable internal
    # contradiction and must remain explicit in the review artifact. A parent
    # outside the batch is unresolved, not proof of a broken or valid chain.
    reported_cycles: set[frozenset[tuple[str, str]]] = set()
    for initial_key in sources:
        path: list[tuple[str, str]] = []
        positions: dict[tuple[str, str], int] = {}
        current = initial_key
        while current in sources:
            if current in positions:
                cycle = path[positions[current]:]
                identity = frozenset(cycle)
                if identity not in reported_cycles:
                    reported_cycles.add(identity)
                    add_conflict(
                        "REVISION_LINEAGE_CYCLE_REVIEW", source_id=current[0],
                        revisions=[revision for _, revision in cycle] + [cycle[0][1]],
                        status="SUPPLIED_UNRESOLVED",
                    )
                break
            positions[current] = len(path)
            path.append(current)
            parent = sources[current]["reported_parent_revision"]
            if parent is None:
                break
            current = (current[0], parent)

    return {
        "schema": SCHEMA,
        "source": {
            "format": "jsonl-provenance",
            "sha256": hashlib.sha256(supplied).hexdigest(),
            "bytes": len(supplied),
            "custody": "SUPPLIED_IN_MEMORY_REVIEW",
            "originals_verified_by_bridge": False,
        },
        "source_bindings": list(sources.values()),
        "candidates": candidates,
        "conflicts": conflicts,
        "boundaries": {
            "navigation": "UNKNOWN", "pressure_hatches": "UNKNOWN",
            "rights": "UNVERIFIED", "canon": "UNAPPROVED",
            "worldpack_release": "HELD",
        },
        "effect": "REVIEW_ONLY_NO_IMPORT",
    }
