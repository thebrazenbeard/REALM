"""Deterministic, review-only location proposals sourced from Creator intake.

This module neither approves content nor imports, opens or mutates game worlds.
A location heading is an *unreviewed assertion in an uploaded document*.
Proposed exits and scene descriptions are synthetic gameplay suggestions.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping

from experiments.realm_creator import CreatorDraftError, prepare_draft

REGION_SCHEMA = "realm.creator.region_proposal.v1"
MAX_REGION_ROOMS = 16
_LOCATION = re.compile(r"^Location:[ \t]+([A-Za-z][A-Za-z0-9 _'-]{0,63})$", re.IGNORECASE | re.ASCII)
_SLUG_SPACES = re.compile(r"[^a-z0-9]+")
_SOURCE_MARKDOWN_HEADING = re.compile(r"^#{1,3}[ \t]+(.+?)\s*#*\s*$")
_FENCE_OPEN = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})(?P<info>.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})[ \t]*$")


class RegionProposalError(ValueError):
    """A source-bound proposal cannot be generated safely."""


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _fenced_lines(source: bytes) -> set[int]:
    """Identify source line numbers inside Markdown backtick/tilde fences."""
    hidden: set[int] = set()
    active: tuple[str, int] | None = None
    for line_number, line in enumerate(source.decode("utf-8").splitlines(), 1):
        if active is not None:
            hidden.add(line_number)
            closing = _FENCE_CLOSE.fullmatch(line)
            if closing is not None:
                run = closing.group("run")
                if run[0] == active[0] and len(run) >= active[1]:
                    active = None
            continue
        opening = _FENCE_OPEN.fullmatch(line)
        if opening is not None:
            run = opening.group("run")
            if run[0] == "~" or "`" not in opening.group("info"):
                active = (run[0], len(run))
                hidden.add(line_number)
    return hidden


def _verified_draft(draft: object, sources: Mapping[str, bytes]) -> dict:
    if not isinstance(draft, dict):
        raise RegionProposalError("Expected an original Creator draft object")
    try:
        original = prepare_draft(draft.get("world_id"), draft.get("name"), sources)
    except (CreatorDraftError, TypeError, ValueError) as exc:
        raise RegionProposalError("Source bundle is not a valid bounded Creator intake") from exc
    # A rehashed but altered persisted draft is not authority to reinterpret
    # source bytes. Reconstruct from exact bytes and compare *all* fields.
    if original != draft:
        raise RegionProposalError("Draft/source digest or content mismatch")
    return original


def propose_region(draft: dict, sources: Mapping[str, bytes]) -> dict:
    """Propose a bounded, explorable region from explicit Location headings.

    Every output carries its source references and REVIEW_REQUIRED status.
    This is not a worldpack, approval, database write or canon promotion.
    """
    original = _verified_draft(draft, sources)
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
           for char in original["name"]):
        raise RegionProposalError("Unsafe display name contains control/format characters")
    # SHA-bound raw lines, not the chunk labels, determine which headings
    # actually occur outside Markdown code fences in the uploaded source.
    lines_by_sha = {
        hashlib.sha256(contents).hexdigest(): contents.decode("utf-8").splitlines()
        for contents in sources.values()
    }
    fenced_by_sha = {
        hashlib.sha256(contents).hexdigest(): _fenced_lines(contents)
        for contents in sources.values()
    }
    rooms = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()

    for section in original["proposals"]:
        provenance = section["provenance"]
        source_digest = provenance["source_sha256"]
        if source_digest not in fenced_by_sha:
            raise RegionProposalError("Source provenance is not in the verified bundle")
        line_number = provenance["start_line"]
        if line_number in fenced_by_sha[source_digest]:
            continue
        source_lines = lines_by_sha[source_digest]
        if not 1 <= line_number <= len(source_lines):
            raise RegionProposalError("Source heading line is out of range")
        raw_heading = _SOURCE_MARKDOWN_HEADING.fullmatch(source_lines[line_number - 1])
        if raw_heading is None:
            continue  # A later segment starts at body text, not a new location.
        heading = raw_heading.group(1).strip()
        if not heading.casefold().startswith("location:"):
            continue
        match = _LOCATION.fullmatch(heading)
        if match is None:
            raise RegionProposalError("Unsafe or ambiguous explicit Location heading")
        name = match.group(1).strip()
        room_id = _SLUG_SPACES.sub("_", name.casefold()).strip("_")
        canonical_name = name.casefold()
        if canonical_name in seen_names or room_id in seen_ids:
            raise RegionProposalError("duplicate or confusable Location heading")
        seen_names.add(canonical_name)
        seen_ids.add(room_id)
        if len(rooms) >= MAX_REGION_ROOMS:
            raise RegionProposalError(f"Region proposal limit is {MAX_REGION_ROOMS} rooms")

        source = section["provenance"]
        rooms.append({
            "id": room_id,
            "name": name,
            "name_evidence": {
                "kind": "UNREVIEWED_SOURCE_HEADING",
                "proposal_id": section["proposal_id"],
                "source_sha256": source["source_sha256"],
                "start_line": source["start_line"],
                "end_line": source["end_line"],
            },
            "source_excerpt": {
                "kind": "UNREVIEWED_SOURCE_EXCERPT",
                "text": section["excerpt"],
                "truncated": section["excerpt_truncated"],
            },
            "description": {
                "kind": "GAMEPLAY_PROPOSAL",
                "text": f"A proposed explorable scene at {name}; details require creator review.",
            },
            "exits": {},
        })

    if not rooms:
        raise RegionProposalError("No explicit Location headings found in the source bundle")

    # A convenient linear *gameplay* draft, not a geographical claim.
    # Do not encode as canonical worldpack exits before human editing.
    for index, room in enumerate(rooms):
        if index > 0:
            room["exits"]["west"] = {
                "to": rooms[index - 1]["id"], "kind": "GAMEPLAY_PROPOSAL",
            }
        if index + 1 < len(rooms):
            room["exits"]["east"] = {
                "to": rooms[index + 1]["id"], "kind": "GAMEPLAY_PROPOSAL",
            }

    result = {
        "schema": REGION_SCHEMA,
        "source_draft_sha256": original["sha256"],
        "world_id": original["world_id"],
        "name": original["name"],
        "status": "REVIEW_REQUIRED",
        "canon_status": "UNREVIEWED",
        "topology_evidence": "NOT_SOURCE_ESTABLISHED",
        "start_room_candidate": {
            "id": rooms[0]["id"], "kind": "GAMEPLAY_PROPOSAL",
        },
        "rooms": rooms,
        "worldpack": None,
        "approval_authenticated": False,
        "world_imported": False,
        "live_world_modified": False,
    }
    result["sha256"] = _fingerprint(result)
    return result
