"""Host-triggered, single-resident policy checkpoint custody (stdlib only).

State storage does not make the model authoritative for REALM events. The host
chooses an already trusted private directory, explicitly saves/restores, and
must serialize writers for the same actor. The SHA-256 detects accidental or
unsophisticated corruption; an attacker who can rewrite files can rehash them.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from npc_policy import OriginalNPCPolicy

_FILE_SCHEMA = "realm-npc-policy-file-v1"
_MAX_FILE_BYTES = 8 * 1024 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _trusted_file_path(raw: str | Path) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError("checkpoint requires host-selected absolute file path")
    if not path.parent.is_dir():
        raise ValueError("checkpoint parent directory must already exist")
    # A private host-owned directory is a prerequisite. Reject symlinked path
    # components at the time of checking; this is not a hostile-FS race proof.
    if any(parent.is_symlink() for parent in (path.parent, *path.parents)):
        raise ValueError("checkpoint path must not traverse symlinks")
    if path.is_symlink():
        raise ValueError("checkpoint cannot be a symlink")
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("checkpoint must be a regular file")
    return path


def load_policy_state(path: str | Path, *, world_id: str,
                      actor_id: str) -> OriginalNPCPolicy:
    """Read a complete checkpoint; reject wrong actors/worlds and corruption."""
    destination = _trusted_file_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(destination, flags)
    except FileNotFoundError:
        raise
    try:
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("checkpoint opened a non-regular file")
            data = stream.read(_MAX_FILE_BYTES + 1)
    except Exception:
        # os.fdopen closes the handle on normal/exceptional context exit.
        raise
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("checkpoint exceeds size budget")
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid checkpoint JSON") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"schema", "payload", "sha256"}:
        raise ValueError("invalid checkpoint envelope")
    if envelope["schema"] != _FILE_SCHEMA:
        raise ValueError("invalid checkpoint file schema")
    digest = envelope["sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("invalid checkpoint digest")
    try:
        actual = hashlib.sha256(_canonical(envelope["payload"])).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid checkpoint payload") from exc
    if digest != actual:
        raise ValueError("checkpoint digest mismatch")
    return OriginalNPCPolicy.from_checkpoint(envelope["payload"],
                                             world_id=world_id, actor_id=actor_id)


def save_policy_state(path: str | Path, policy: OriginalNPCPolicy, *,
                      world_id: str, actor_id: str) -> None:
    """Atomically replace one host-owned checkpoint, only on explicit call.

    Existing checkpoints must match the same actor/world and pass full
    validation before overwriting, to prevent accidental cross-character
    custody changes. A failure before os.replace leaves the old file intact.
    """
    destination = _trusted_file_path(path)
    if not isinstance(policy, OriginalNPCPolicy):
        raise TypeError("expected OriginalNPCPolicy")
    payload = policy.export_checkpoint()
    if payload["world_id"] != world_id or payload["actor_id"] != actor_id:
        raise ValueError("checkpoint world/actor binding mismatch")
    if destination.exists():
        load_policy_state(destination, world_id=world_id, actor_id=actor_id)
    envelope = {
        "schema": _FILE_SCHEMA,
        "payload": payload,
        "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
    }
    encoded = _canonical(envelope)
    if len(encoded) > _MAX_FILE_BYTES:
        raise ValueError("checkpoint exceeds size budget")

    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=destination.name + ".",
                                       suffix=".tmp", dir=destination.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # A hostile writer could still swap the parent between inspection and
        # replace. Live shared-host deployment needs private, controlled ACLs.
        if destination.is_symlink():
            raise ValueError("checkpoint became a symlink")
        os.replace(tmp_name, destination)
        tmp_name = None
        if os.name != "nt":
            try:
                dir_fd = os.open(destination.parent, os.O_RDONLY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
