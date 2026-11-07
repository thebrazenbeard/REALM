"""Private, incremental source-corpus staging for REALM Creator.

Pure data transformation only: no database, host, model, canon promotion, or
publication effects. Returned excerpts are private drafts and MUST NOT be
published just because a public code repository contains this module.
The digest is an accidental-change check, NOT an authenticated signature.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
import unicodedata
from typing import Mapping

from realm.worldpack import WorldpackError, validate_worldpack

from .draft import (_digest, _safe_source_text, _sections, MAX_EXCERPT_CHARS,
                    MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES)


CORPUS_SCHEMA = "realm.creator.corpus.v1"
MAX_BATCH_CANDIDATES = 2000
MAX_CSV_COLUMNS = 64
MAX_PATH_CHARS = 512
MAX_SOURCE_LINES = 20000
MAX_CANDIDATES_PER_SOURCE = 10000
MAX_TOTAL_CANDIDATES = 100000
MAX_TOTAL_SOURCES = 2048
MAX_TOTAL_BATCHES = 8192
MAX_TOTAL_CONFLICTS = 100000
_SHA = re.compile(r"^[0-9a-f]{64}$")
_SUFFIXES = (".txt", ".md", ".markdown", ".csv")
_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_WIN_DEVICES = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10)}


class CorpusError(ValueError):
    """Batch rejected without modifying the previous private corpus state."""


def _hash_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _identity(prefix: str, *parts: object) -> str:
    return prefix + "-" + _digest(parts)[:24]


def _path(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PATH_CHARS:
        raise CorpusError("Source path must be a bounded relative POSIX path")
    if (unicodedata.normalize("NFC", value) != value or
            not _safe_source_text(value) or
            any(unicodedata.category(c) in {"Cc", "Zl", "Zp"} or
                (unicodedata.category(c) == "Zs" and c != " ") for c in value)):
        raise CorpusError("Source path has ambiguous Unicode")
    parts = value.split("/")
    if (value.startswith("/") or any(c in value for c in '\\:<>|?*"') or
            any(p in {"", ".", ".."} or p.startswith((".", " ")) or
                p.endswith((" ", ".")) or
                unicodedata.normalize("NFKC", p.split(".", 1)[0]).casefold() in _WIN_DEVICES
                for p in parts) or
            not value.casefold().endswith(_SUFFIXES)):
        raise CorpusError("Source path is not a safe relative .txt/.md/.markdown/.csv path")
    return value


def _shape(record: object, keys: set[str], context: str) -> dict:
    if not isinstance(record, dict) or set(record) != keys:
        raise CorpusError(f"Invalid {context} shape")
    return record


def _name_and_id(world_id: str, name: str) -> tuple[str, str]:
    try:
        validate_worldpack({"schema": "realm.worldpack.v1", "world_id": world_id,
                            "name": name, "start_room": "unpublished",
                            "rooms": [{"id": "unpublished", "name": "Unpublished",
                                       "description": "Unpublished draft", "exits": {}}]})
    except (WorldpackError, TypeError, AttributeError) as exc:
        raise CorpusError("Invalid world ID/name") from exc
    return world_id, name


def _with_hash(state: dict) -> dict:
    state["sha256"] = _digest({k: v for k, v in state.items() if k != "sha256"})
    return state


def new_corpus(world_id: str, name: str) -> dict:
    """Start a private, unreviewed staging ledger; no world is created."""
    _name_and_id(world_id, name)
    return _with_hash({"schema": CORPUS_SCHEMA, "world_id": world_id,
                       "name": name, "status": "PRIVATE_REVIEW_REQUIRED",
                       "sources": [], "candidates": [], "conflicts": [], "batches": [],
                       "world_imported": False, "published": False,
                       "semantic_relation": "NOT_ENTAILMENT"})


def _validated(state: object, expected_sha256: str) -> dict:
    if not isinstance(state, dict) or set(state) != {
            "schema", "world_id", "name", "status", "sources", "candidates",
            "conflicts", "batches", "world_imported", "published",
            "semantic_relation", "sha256"}:
        raise CorpusError("Invalid corpus state shape")
    if (state["schema"] != CORPUS_SCHEMA or state["status"] != "PRIVATE_REVIEW_REQUIRED" or
            state["world_imported"] is not False or state["published"] is not False or
            state["semantic_relation"] != "NOT_ENTAILMENT"):
        raise CorpusError("Corpus must remain an unpublished, unreviewed private draft")
    _name_and_id(state["world_id"], state["name"])
    if any(not isinstance(state[k], list) for k in ("sources", "candidates", "conflicts", "batches")):
        raise CorpusError("Corpus ledgers must be arrays")
    if (len(state["sources"]) > MAX_TOTAL_SOURCES or
            len(state["candidates"]) > MAX_TOTAL_CANDIDATES or
            len(state["conflicts"]) > MAX_TOTAL_CONFLICTS or
            len(state["batches"]) > MAX_TOTAL_BATCHES):
        raise CorpusError("Corpus ledger size exceeds bounded limits")
    try:
        actual_digest = _digest({k: v for k, v in state.items() if k != "sha256"})
    except (TypeError, ValueError, RecursionError, OverflowError, UnicodeError) as exc:
        raise CorpusError("Corpus is not a JSON-compatible document") from exc
    if (not isinstance(state["sha256"], str) or not _SHA.fullmatch(state["sha256"]) or
            state["sha256"] != actual_digest):
        raise CorpusError("Corpus digest mismatch")
    if expected_sha256 != state["sha256"]:
        raise CorpusError("Stale corpus revision; reload before retry")
    try:
        source_keys = {"source_id", "revision_id", "path", "sha256", "bytes",
                       "format", "status", "candidates_total", "candidates_staged", "complete"}
        candidate_keys = {"candidate_id", "source_id", "source_revision_id", "format",
                          "heading", "excerpt", "excerpt_truncated", "record_sha256",
                          "provenance", "status", "room_candidate"}
        conflict_keys = {"conflict_id", "kind", "candidate_ids", "source_revision_ids",
                         "note", "resolution_status"}
        batch_keys = {"batch_id", "manifest_sha256", "source_count", "candidates_added"}
        for i, r in enumerate(state["sources"]):
            _shape(r, source_keys, f"sources[{i}]")
        for i, r in enumerate(state["candidates"]):
            _shape(r, candidate_keys, f"candidates[{i}]")
        for i, r in enumerate(state["conflicts"]):
            _shape(r, conflict_keys, f"conflicts[{i}]")
        for i, r in enumerate(state["batches"]):
            _shape(r, batch_keys, f"batches[{i}]")
        source_ids = {r["source_id"] for r in state["sources"]}
        revision_ids = {r["revision_id"] for r in state["sources"]}
        source_revisions = {r["revision_id"]: r for r in state["sources"]}
        candidate_ids = {r["candidate_id"] for r in state["candidates"]}
        batch_ids = [r["batch_id"] for r in state["batches"]]
        if (len(revision_ids) != len(state["sources"]) or
                len(candidate_ids) != len(state["candidates"]) or
                len(batch_ids) != len(set(batch_ids))):
            raise CorpusError("Duplicate ledger identities")
        staged_counts = {r: 0 for r in revision_ids}
        for r in state["candidates"]:
            staged_counts[r["source_revision_id"]] += 1
        seen_paths: dict[str, str] = {}
        for r in state["sources"]:
            if (_path(r["path"]) != r["path"] or
                    not isinstance(r["source_id"], str) or
                    not isinstance(r["revision_id"], str) or
                    not isinstance(r["sha256"], str) or not _SHA.fullmatch(r["sha256"]) or
                    r["source_id"] != _identity("source", state["world_id"], r["path"].casefold()) or
                    r["revision_id"] != _identity("revision", r["source_id"], r["sha256"]) or
                    type(r["bytes"]) is not int or not 1 <= r["bytes"] <= MAX_FILE_BYTES or
                    r["format"] not in {"CSV", "TEXT"} or
                    r["format"] != ("CSV" if r["path"].casefold().endswith(".csv") else "TEXT") or
                    type(r["candidates_total"]) is not int or
                    type(r["candidates_staged"]) is not int or
                    not 1 <= r["candidates_total"] <= 1000000 or
                    not 0 <= r["candidates_staged"] <= r["candidates_total"] or
                    r["candidates_staged"] != staged_counts[r["revision_id"]] or
                    type(r["complete"]) is not bool or
                    r["complete"] is not (r["candidates_staged"] == r["candidates_total"]) or
                    r["status"] != "UNREVIEWED"):
                raise CorpusError("Invalid source ledger record")
            path_key = r["path"].casefold()
            if path_key in seen_paths and seen_paths[path_key] != r["path"]:
                raise CorpusError("Colliding source path identities")
            seen_paths[path_key] = r["path"]
        for r in state["candidates"]:
            p = _shape(r["provenance"], {"source_sha256", "start_line", "end_line"},
                       "candidate provenance")
            if (r["source_id"] not in source_ids or
                    r["source_revision_id"] not in revision_ids or
                    source_revisions[r["source_revision_id"]]["source_id"] != r["source_id"] or
                    source_revisions[r["source_revision_id"]]["sha256"] != p["source_sha256"] or
                    r["status"] != "UNREVIEWED" or r["room_candidate"] is not False or
                    r["format"] != source_revisions[r["source_revision_id"]]["format"] or
                    type(p["start_line"]) is not int or type(p["end_line"]) is not int or
                    not (1 <= p["start_line"] <= p["end_line"] <= 1000000) or
                    not isinstance(r["heading"], str) or
                    not 1 <= len(r["heading"]) <= 100 or
                    "\n" in r["heading"] or "\r" in r["heading"] or
                    not _safe_source_text(r["heading"]) or
                    not isinstance(r["excerpt"], str) or
                    not _safe_source_text(r["excerpt"]) or
                    len(r["excerpt"]) > MAX_EXCERPT_CHARS or
                    type(r["excerpt_truncated"]) is not bool or
                    not isinstance(r["record_sha256"], str) or
                    not _SHA.fullmatch(r["record_sha256"]) or
                    not isinstance(r["candidate_id"], str) or
                    r["candidate_id"] != _identity("candidate", r["source_revision_id"],
                                                   p["start_line"], p["end_line"],
                                                   r["heading"], r["record_sha256"])):
                raise CorpusError("Invalid candidate ledger record")
        # One index for the whole ledger: rebuilding it per conflict would be
        # O(candidate_count * conflict_count), a quota-respecting denial of service.
        candidate_index = {c["candidate_id"]: c for c in state["candidates"]}
        for r in state["conflicts"]:
            if (r["resolution_status"] != "UNRESOLVED" or
                    r["kind"] not in {"SOURCE_REVISION", "POTENTIAL_CLAIM"} or
                    not isinstance(r["note"], str) or
                    not 1 <= len(r["note"]) <= 500 or not _safe_source_text(r["note"]) or
                    any(c in r["note"] for c in "\r\n\u2028\u2029") or
                    not isinstance(r["candidate_ids"], list) or
                    not isinstance(r["source_revision_ids"], list) or
                    any(not isinstance(v, str) for v in r["candidate_ids"] + r["source_revision_ids"]) or
                    not set(r["candidate_ids"]).issubset(candidate_ids) or
                    not set(r["source_revision_ids"]).issubset(revision_ids)):
                raise CorpusError("Invalid conflict ledger record")
            if r["kind"] == "SOURCE_REVISION":
                versions = r["source_revision_ids"]
                if (r["candidate_ids"] or len(versions) != 2 or versions[0] == versions[1] or
                        source_revisions[versions[0]]["source_id"] !=
                        source_revisions[versions[1]]["source_id"] or
                        r["conflict_id"] != _identity("conflict", "SOURCE_REVISION", versions)):
                    raise CorpusError("Invalid source revision conflict")
            else:
                ids = r["candidate_ids"]
                if (r["source_revision_ids"] or len(ids) != 2 or ids != sorted(set(ids)) or
                        candidate_index[ids[0]]["source_id"] == candidate_index[ids[1]]["source_id"] or
                        r["conflict_id"] != _identity("conflict", "POTENTIAL_CLAIM", ids, r["note"])):
                    raise CorpusError("Invalid potential cross-document conflict")
        if len({c["conflict_id"] for c in state["conflicts"]}) != len(state["conflicts"]):
            raise CorpusError("Duplicate conflict identities")
        total_added = 0
        for r in state["batches"]:
            if (not isinstance(r["batch_id"], str) or not _BATCH_ID.fullmatch(r["batch_id"]) or
                    not isinstance(r["manifest_sha256"], str) or
                    not _SHA.fullmatch(r["manifest_sha256"]) or
                    type(r["source_count"]) is not int or not 1 <= r["source_count"] <= MAX_FILES or
                    type(r["candidates_added"]) is not int or
                    not 0 <= r["candidates_added"] <= MAX_BATCH_CANDIDATES):
                raise CorpusError("Invalid batch ledger record")
            total_added += r["candidates_added"]
        if total_added != len(state["candidates"]):
            raise CorpusError("Batch/candidate count ledger mismatch")
    except (TypeError, KeyError, ValueError, OverflowError) as exc:
        raise CorpusError("Malformed corpus ledger record") from exc
    return state


def _decode(path: str, contents: bytes) -> str:
    if not isinstance(contents, bytes) or not 1 <= len(contents) <= MAX_FILE_BYTES:
        raise CorpusError(f"{path!r} source must contain 1-{MAX_FILE_BYTES} bytes")
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CorpusError(f"{path!r} source is not UTF-8") from exc
    if not text.strip() or not _safe_source_text(text):
        raise CorpusError(f"{path!r} source is empty or contains unsafe format characters")
    # Count physical lines, including CR-only legacy documents. Counting only
    # LF allows an attacker to bypass this quota with arbitrarily many CRs.
    if len(text.splitlines()) > MAX_SOURCE_LINES:
        raise CorpusError(f"{path!r} has too many physical lines; provide provenance-bound segments")
    return text


def _csv_rows(text: str) -> list[tuple[str, str, str, int, int]]:
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader)
        if (not 1 <= len(header) <= MAX_CSV_COLUMNS or
                any(not h.strip() or h != h.strip() or len(h) > 120 for h in header) or
                len({unicodedata.normalize("NFC", h).strip().casefold() for h in header}) != len(header)):
            raise CorpusError("CSV requires unique, bounded, nonblank column headings")
        previous_line = reader.line_num
        rows = []
        for index, row in enumerate(reader, start=1):
            if index > MAX_CANDIDATES_PER_SOURCE:
                raise CorpusError("CSV exceeds per-source candidate bound; split with line provenance")
            line = reader.line_num
            if len(row) != len(header):
                raise CorpusError("CSV has a row with a mismatched column count")
            # Serialize field boundaries; never let delimiter-like source text
            # masquerade as another column in a human review excerpt.
            row_content = json.dumps(dict(zip(header, row)), ensure_ascii=False,
                                     separators=(",", ":"))
            rows.append((f"CSV row {index}", row_content,
                         _hash_bytes(repr(row).encode("utf-8")), previous_line + 1, line))
            previous_line = line
        if not rows:
            raise CorpusError("CSV must include data rows")
        return rows
    except (csv.Error, StopIteration) as exc:
        raise CorpusError("Invalid CSV source") from exc


def stage_batch(state: dict, batch_id: str, sources: Mapping[str, bytes],
                *, expected_state_sha256: str) -> dict:
    """Atomically append a bounded batch, preserving every candidate or failing.

    Repeating exactly the same batch ID + payload is an idempotent no-op.
    A large CSV may stage in several batches: sources expose staged/total counts
    and complete=false until the same source bytes are supplied in a new batch.
    Different source revisions remain separate, never silently picked as canon.
    """
    _validated(state, expected_state_sha256)
    if not isinstance(batch_id, str) or not _BATCH_ID.fullmatch(batch_id):
        raise CorpusError("Invalid batch ID")
    if not isinstance(sources, Mapping) or not 1 <= len(sources) <= MAX_FILES:
        raise CorpusError(f"Each batch requires 1-{MAX_FILES} files")
    entries = []
    seen = set()
    total = 0
    for path, contents in sources.items():
        canonical = _path(path)
        if canonical.casefold() in seen:
            raise CorpusError("Case/Unicode-colliding source paths in batch")
        seen.add(canonical.casefold())
        decoded = _decode(canonical, contents)
        total += len(contents)
        if total > MAX_TOTAL_BYTES:
            raise CorpusError("Combined batch bytes exceed bounded intake cap")
        entries.append((canonical, contents, decoded))
    entries.sort(key=lambda entry: entry[0].casefold())
    manifest = [{"path": p, "sha256": _hash_bytes(raw), "bytes": len(raw)}
                for p, raw, _ in entries]
    fingerprint = _digest(manifest)
    for previous in state["batches"]:
        if previous["batch_id"] == batch_id:
            if previous["manifest_sha256"] != fingerprint:
                raise CorpusError("Batch ID replay has different payload")
            return copy.deepcopy(state)
    result = copy.deepcopy(state)
    if len(result["batches"]) >= MAX_TOTAL_BATCHES:
        raise CorpusError("Corpus batch ledger is full; start a new governed corpus")
    candidates_added = 0
    for path, raw, text in entries:
        path_id = _identity("source", result["world_id"], path.casefold())
        original = [r for r in result["sources"] if r["source_id"] == path_id]
        if original and original[0]["path"] != path:
            raise CorpusError("Source path collides with a previously staged path")
        revision_sha = _hash_bytes(raw)
        revision_id = _identity("revision", path_id, revision_sha)
        staged_revision = next((r for r in original if r["revision_id"] == revision_id), None)
        if staged_revision is not None and staged_revision["complete"]:
            continue  # New batch references already-staged immutable revision.
        is_csv = path.casefold().endswith(".csv")
        if is_csv:
            section_rows = _csv_rows(text)
        else:
            section_rows = [(heading, content, _hash_bytes(content.encode("utf-8")), first, last)
                            for heading, content, first, last in _sections(text)]
        if len(section_rows) > MAX_CANDIDATES_PER_SOURCE:
            raise CorpusError("Source exceeds per-source candidate bound; segment with provenance")
        if not section_rows:
            raise CorpusError("A source must have at least one reviewable candidate")
        if staged_revision is not None:
            if staged_revision["candidates_total"] != len(section_rows):
                raise CorpusError("Staged candidate count differs for the immutable source revision")
            record = next(r for r in result["sources"] if r["revision_id"] == revision_id)
        else:
            if len(result["sources"]) >= MAX_TOTAL_SOURCES:
                raise CorpusError("Corpus source ledger is full")
            record = {"source_id": path_id, "revision_id": revision_id,
                      "path": path, "sha256": revision_sha, "bytes": len(raw),
                      "format": "CSV" if is_csv else "TEXT", "status": "UNREVIEWED",
                      "candidates_total": len(section_rows), "candidates_staged": 0,
                      "complete": False}
            result["sources"].append(record)
        if original and staged_revision is None:
            if len(result["conflicts"]) >= MAX_TOTAL_CONFLICTS:
                raise CorpusError("Corpus conflict ledger is full")
            previous = original[-1]["revision_id"]
            source_revisions = [previous, revision_id]
            result["conflicts"].append({
                "conflict_id": _identity("conflict", "SOURCE_REVISION", source_revisions),
                "kind": "SOURCE_REVISION", "candidate_ids": [],
                "source_revision_ids": source_revisions,
                "note": "Source path has a different immutable content revision; review both.",
                "resolution_status": "UNRESOLVED"})
        for heading, content, record_sha, start_line, end_line in section_rows[
                record["candidates_staged"]:]:
            if candidates_added >= MAX_BATCH_CANDIDATES:
                break  # Explicit incomplete cursor; next batch reuses identical bytes.
            if len(result["candidates"]) >= MAX_TOTAL_CANDIDATES:
                raise CorpusError("Corpus candidate ledger is full")
            result["candidates"].append({
                "candidate_id": _identity("candidate", revision_id, start_line,
                                          end_line, heading, record_sha),
                "source_id": path_id, "source_revision_id": revision_id,
                "format": record["format"], "heading": heading,
                "excerpt": content[:MAX_EXCERPT_CHARS],
                "excerpt_truncated": len(content) > MAX_EXCERPT_CHARS,
                "record_sha256": record_sha,
                "provenance": {"source_sha256": revision_sha,
                               "start_line": start_line, "end_line": end_line},
                "status": "UNREVIEWED", "room_candidate": False})
            candidates_added += 1
            record["candidates_staged"] += 1
        record["complete"] = record["candidates_staged"] == record["candidates_total"]
    result["batches"].append({"batch_id": batch_id,
                              "manifest_sha256": fingerprint,
                              "source_count": len(entries),
                              "candidates_added": candidates_added})
    return _with_hash(result)


def flag_cross_document_conflict(state: dict, candidate_ids: tuple[str, str],
                                 note: str, *, expected_state_sha256: str) -> dict:
    """Record a *potential*, human-identified contradiction, never decide canon."""
    _validated(state, expected_state_sha256)
    if (not isinstance(candidate_ids, tuple) or len(candidate_ids) != 2 or
            any(not isinstance(x, str) for x in candidate_ids) or
            candidate_ids[0] == candidate_ids[1]):
        raise CorpusError("Conflict needs two distinct candidate IDs")
    if (not isinstance(note, str) or not 1 <= len(note) <= 500 or
            not _safe_source_text(note) or any(c in note for c in "\r\n\u2028\u2029")):
        raise CorpusError("Conflict note must be bounded and safe")
    candidates = {r["candidate_id"]: r for r in state["candidates"]}
    if any(x not in candidates for x in candidate_ids):
        raise CorpusError("Conflict references unknown candidate")
    if candidates[candidate_ids[0]]["source_id"] == candidates[candidate_ids[1]]["source_id"]:
        raise CorpusError("Cross-document conflict must reference distinct sources")
    ids = sorted(candidate_ids)
    conflict_id = _identity("conflict", "POTENTIAL_CLAIM", ids, note)
    if any(r["conflict_id"] == conflict_id for r in state["conflicts"]):
        return copy.deepcopy(state)
    if len(state["conflicts"]) >= MAX_TOTAL_CONFLICTS:
        raise CorpusError("Corpus conflict ledger is full")
    result = copy.deepcopy(state)
    result["conflicts"].append({"conflict_id": conflict_id,
                                "kind": "POTENTIAL_CLAIM", "candidate_ids": ids,
                                "source_revision_ids": [], "note": note,
                                "resolution_status": "UNRESOLVED"})
    return _with_hash(result)
