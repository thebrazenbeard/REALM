"""Strict read-only preview of author-supplied realm.worldpack.v1 bytes.

No text-to-world generation, canonical review, source authentication, SQLite
import, network request, or world publication occurs in this module.
"""
from __future__ import annotations

import hashlib
import json
import re

from realm.worldpack import MAX_SOURCE_BYTES, WorldpackError, validate_worldpack

_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    """Reject JSON aliases that json.loads would ordinarily discard."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorldpackError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def preview_existing_worldpack(raw: bytes, provenance: dict) -> dict:
    """Validate existing authored topology without replacing or rewriting it."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise WorldpackError(f"Existing worldpack must be 1-{MAX_SOURCE_BYTES} raw bytes")
    if not isinstance(provenance, dict) or set(provenance) - {"source_id", "source_sha256"}:
        raise WorldpackError("Untrusted provenance has invalid fields")
    source_id = provenance.get("source_id")
    if not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None:
        raise WorldpackError("source_id must be a bounded lowercase identifier")
    source_sha = hashlib.sha256(raw).hexdigest()
    claimed_sha = provenance.get("source_sha256", source_sha)
    if (not isinstance(claimed_sha, str) or _SHA256.fullmatch(claimed_sha) is None
            or claimed_sha != source_sha):
        raise WorldpackError("Provenance source_sha256 does not match supplied bytes")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
    except WorldpackError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise WorldpackError("Existing worldpack is not valid bounded UTF-8 JSON") from exc
    pack = validate_worldpack(parsed)
    semantic = json.dumps(pack, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    return {
        "worldpack": pack,
        "semantic_sha256": hashlib.sha256(semantic).hexdigest(),
        "provenance": {
            "source_id": source_id,
            "source_sha256": source_sha,
            "source_status": "UNVERIFIED",
            "authenticated": False,
        },
        "review_status": "REVIEW_REQUIRED",
        "canon_status": "UNREVIEWED",
        "approval_authenticated": False,
        "world_imported": False,
        "worldpack_generated": False,
    }
