"""Opt-in world-bound SQLite persistence for the synthetic atmosphere kernel.

All mutation is host-invoked, not an authenticated player command. A host may
reuse its own BEGIN IMMEDIATE connection (e.g. WorldClock.tick) through
apply_in_transaction; side effects and receipts then share its rollback.
SQLite files are rollbackable, not independent safety/death witnesses.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .atmosphere import AtmosphereModel, _MAX_PRESSURE, _MAX_RATE, _id, _integer

_ACTIONS = {"hatch", "breach", "tick"}
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_COMMAND_BYTES = 4096
_RECEIPT_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_WORLD_AUTHORITY_KEYS = ("world_id", "schema", "name", "start_room", "worldpack_sha256")


def _world_authority(db):
    """Preserve engine identity and imported worldpack provenance, not audit keys."""
    return {row["key"]: row["value"] for row in db.execute(
        "SELECT key,value FROM main.world_meta WHERE key IN (?,?,?,?,?)",
        _WORLD_AUTHORITY_KEYS,
    )}


def _json(value, limit):
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
        if len(text.encode("utf-8")) > limit:
            raise ValueError("Atmosphere JSON exceeds size bound")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("Invalid bounded atmosphere JSON") from exc
    return text


def _read(raw, limit):
    if type(raw) is not str or len(raw.encode("utf-8")) > limit:
        raise ValueError("Invalid stored atmosphere JSON size")
    def unique(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError("Duplicate atmosphere JSON key")
            obj[k] = v
        return obj
    def invalid_constant(_):
        raise ValueError("Nonfinite atmosphere JSON")
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Malformed stored atmosphere JSON") from exc


def _rehydrate(snapshot):
    if type(snapshot) is not dict or set(snapshot) != {"compartments", "hatches", "breaches", "occupants"}:
        raise ValueError("Invalid atmosphere state shape")
    rooms = snapshot["compartments"]
    if type(rooms) is not dict:
        raise ValueError("Invalid atmosphere compartments")
    # Rebuild the host-validated model, then restore exact integer inventories.
    specs = {}
    for key, c in rooms.items():
        if type(c) is not dict or set(c) != {"volume_l", "gas_pa_l", "oxygen_pa_l", "temperature_mk", "pressure_pa", "oxygen_ppm"}:
            raise ValueError("Invalid compartment snapshot")
        v = _integer(c["volume_l"], "volume_l", 1, 10_000_000)
        # Checking only floor(gas / volume) lets a supermaximum fractional
        # inventory pass at the declared max pressure. Bound the exact gas.
        gas = _integer(c["gas_pa_l"], "gas_pa_l", 0, v * _MAX_PRESSURE)
        oxygen = _integer(c["oxygen_pa_l"], "oxygen_pa_l", 0, gas)
        _integer(c["temperature_mk"], "temperature_mk", 1, 2_000_000)
        pressure = _integer(c["pressure_pa"], "pressure_pa", 0, _MAX_PRESSURE)
        oxygen_ppm = _integer(c["oxygen_ppm"], "oxygen_ppm", 0, 1_000_000)
        if pressure != gas // v or oxygen_ppm != (oxygen * 1_000_000 // gas if gas else 0):
            raise ValueError("Inconsistent atmospheric inventory")
        specs[key] = {"volume_l": v, "pressure_pa": 0, "oxygen_ppm": 0,
                      "temperature_mk": c["temperature_mk"]}
    model = AtmosphereModel(compartments=specs,
                            hatches=snapshot["hatches"], occupants=snapshot["occupants"])
    for key, c in rooms.items():
        model.compartments[key]["gas_pa_l"] = c["gas_pa_l"]
        model.compartments[key]["oxygen_pa_l"] = c["oxygen_pa_l"]
    breaches = snapshot["breaches"]
    if type(breaches) is not dict:
        raise ValueError("Invalid atmosphere breaches")
    for chamber, rate in breaches.items():
        if chamber not in model.compartments:
            raise ValueError("Unknown restored breach compartment")
        model.breaches[chamber] = _integer(rate, "breach rate", 1, _MAX_RATE)
    # JSON tuples (operator memberships) are lists on disk; compare canonical
    # JSON shape while preserving exact gas/oxygen integer inventories.
    if _json(model.snapshot(), _MAX_STATE_BYTES) != _json(snapshot, _MAX_STATE_BYTES):
        raise ValueError("Atmosphere snapshot failed exact restoration")
    model._receipts.clear()
    return model


class AtmosphereLedger:
    """One initialized world per DB; no actor authentication or autonomous tick."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)

    def _bound(self, db, world_id, *, require_transaction=False):
        _id(world_id)
        files = db.execute("PRAGMA database_list").fetchall()
        main = next((row[2] for row in files if row[1] == "main"), None)
        if not main or Path(main).resolve() != self.db_path.resolve():
            raise RuntimeError("Atmosphere connection is not the bound world DB")
        if db.row_factory is not sqlite3.Row or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Atmosphere requires row_factory and foreign keys")
        if require_transaction and not db.in_transaction:
            raise RuntimeError("Atmosphere host transaction required")
        main_meta = db.execute("SELECT type FROM main.sqlite_master WHERE name='world_meta'").fetchone()
        if not main_meta or main_meta[0] != "table":
            raise RuntimeError("World identity table unavailable")
        actual = db.execute("SELECT value FROM main.world_meta WHERE key='world_id'").fetchone()
        if actual is None or actual[0] != world_id:
            raise ValueError("Atmosphere world binding mismatch")

    def enable(self, *, world_id, model: AtmosphereModel):
        """Explicit host-only install into an already-bound, pristine world DB."""
        if type(model) is not AtmosphereModel or not self.db_path.is_file():
            raise ValueError("Initialized world and valid atmosphere model required")
        state = _json(model.snapshot(), _MAX_STATE_BYTES)
        _rehydrate(_read(state, _MAX_STATE_BYTES))
        with closing(sqlite3.connect(str(self.db_path), timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                db.execute("BEGIN IMMEDIATE")
                self._bound(db, world_id, require_transaction=True)
                if db.execute("SELECT name FROM main.sqlite_master WHERE name IN ('atm_state','atm_receipts')").fetchone():
                    raise RuntimeError("Atmosphere already initialized or foreign schema occupied")
                db.execute("CREATE TABLE atm_state (id INTEGER PRIMARY KEY CHECK(id=1), world_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>=0), state_json TEXT NOT NULL)")
                db.execute("CREATE TABLE atm_receipts (action_id TEXT PRIMARY KEY, world_id TEXT NOT NULL, command_json TEXT NOT NULL, result_json TEXT NOT NULL)")
                db.execute("INSERT INTO atm_state(id,world_id,version,state_json) VALUES(1,?,?,?)", (world_id, 0, state))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _connect(self):
        if not self.db_path.is_file():
            raise RuntimeError("Atmosphere world DB is unavailable")
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def status(self, *, world_id):
        with closing(self._connect()) as db:
            self._bound(db, world_id)
            row = db.execute("SELECT world_id,version,state_json FROM main.atm_state WHERE id=1").fetchone()
            if row is None or row["world_id"] != world_id:
                raise RuntimeError("Atmosphere state is not enabled for this world")
            snapshot = _read(row["state_json"], _MAX_STATE_BYTES)
            _rehydrate(snapshot)  # Fail closed on tampering or inconsistent inventory.
            return {"version": row["version"], "snapshot": snapshot}

    def apply(self, *, world_id, action_id, operation, params, expected_version):
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                result = self.apply_in_transaction(db, world_id=world_id, action_id=action_id,
                    operation=operation, params=params, expected_version=expected_version)
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def apply_in_transaction(self, db, *, world_id, action_id, operation, params, expected_version):
        """Host-owned BEGIN IMMEDIATE SQLite connection; nested savepoint on failure."""
        if not isinstance(db, sqlite3.Connection):
            raise ValueError("SQLite connection required")
        self._bound(db, world_id, require_transaction=True)
        if type(action_id) is not str or not _RECEIPT_ID.fullmatch(action_id):
            raise ValueError("Invalid atmosphere action ID")
        if type(operation) is not str or operation not in _ACTIONS or type(params) is not dict:
            raise ValueError("Invalid atmosphere operation")
        if type(expected_version) is not int or not 0 <= expected_version < 2**63 - 1:
            raise ValueError("Invalid expected atmosphere revision")
        required = {"hatch": {"hatch_id", "open", "actor"},
                    "breach": {"chamber_id", "loss_pa_l_per_tick"}, "tick": set()}
        if set(params) != required[operation]:
            raise ValueError("Unexpected atmosphere operation parameters")
        command = _json({"world_id": world_id, "operation": operation,
                         "params": params, "expected_version": expected_version}, _MAX_COMMAND_BYTES)
        db.execute("SAVEPOINT atm_action")
        try:
            prior = db.execute("SELECT world_id,command_json,result_json FROM main.atm_receipts WHERE action_id=?", (action_id,)).fetchone()
            if prior is not None:
                if prior["world_id"] != world_id or prior["command_json"] != command:
                    raise ValueError("Atmosphere action replay conflict")
                result = _read(prior["result_json"], _MAX_STATE_BYTES)
            else:
                # Keep unrelated receipts intact. MAIN triggers on our own
                # INSERT may add a forged future replay or alter a prior
                # action without touching the just-inserted receipt.
                before_receipts = {
                    row["action_id"]: (row["world_id"], row["command_json"], row["result_json"])
                    for row in db.execute(
                        "SELECT action_id,world_id,command_json,result_json FROM main.atm_receipts"
                    )
                }
                before_world_authority = _world_authority(db)
                state = db.execute("SELECT world_id,version,state_json FROM main.atm_state WHERE id=1").fetchone()
                if state is None or state["world_id"] != world_id:
                    raise RuntimeError("Atmosphere world state missing")
                if state["version"] != expected_version:
                    raise ValueError("Stale atmosphere revision")
                model = _rehydrate(_read(state["state_json"], _MAX_STATE_BYTES))
                if operation == "hatch":
                    outcome = model.hatch(params["hatch_id"], open=params["open"],
                                          actor=params["actor"], action_id=action_id)
                elif operation == "breach":
                    outcome = model.breach(params["chamber_id"],
                        loss_pa_l_per_tick=params["loss_pa_l_per_tick"], action_id=action_id)
                else:
                    outcome = model.tick(action_id=action_id)
                next_state_json = _json(model.snapshot(), _MAX_STATE_BYTES)
                if db.execute("UPDATE main.atm_state SET version=?, state_json=? WHERE id=1 AND world_id=? AND version=?",
                              (expected_version + 1, next_state_json, world_id, expected_version)).rowcount != 1:
                    raise RuntimeError("Concurrent atmosphere revision change")
                result = {"version": expected_version + 1, "result": outcome}
                response_json = _json(result, _MAX_STATE_BYTES)
                db.execute("INSERT INTO main.atm_receipts(action_id,world_id,command_json,result_json) VALUES(?,?,?,?)",
                           (action_id, world_id, command, response_json))
                # MAIN triggers can modify/delete either row after its DML
                # succeeds. A returned ACK must match BOTH durable effects at
                # the end of this nested action, not just rowcount/INSERT.
                persisted_state = db.execute(
                    "SELECT world_id,version,state_json FROM main.atm_state WHERE id=1"
                ).fetchone()
                persisted_receipt = db.execute(
                    "SELECT world_id,command_json,result_json FROM main.atm_receipts WHERE action_id=?",
                    (action_id,)
                ).fetchone()
                persisted_receipts = {
                    row["action_id"]: (row["world_id"], row["command_json"], row["result_json"])
                    for row in db.execute(
                        "SELECT action_id,world_id,command_json,result_json FROM main.atm_receipts"
                    )
                }
                expected_receipts = dict(before_receipts)
                expected_receipts[action_id] = (world_id, command, response_json)
                if (persisted_state is None or
                    tuple(persisted_state) != (world_id, expected_version + 1, next_state_json) or
                    persisted_receipt is None or
                    tuple(persisted_receipt) != (world_id, command, response_json) or
                    persisted_receipts != expected_receipts):
                    raise ValueError("Atmosphere state/receipt diverged before acknowledgment")
                # Receipt triggers may change/delete main.world_meta after the
                # initial binding check while preserving every atmosphere row.
                self._bound(db, world_id, require_transaction=True)
                if _world_authority(db) != before_world_authority:
                    raise ValueError("Atmosphere world authority metadata diverged")
                result = _read(response_json, _MAX_STATE_BYTES)
            db.execute("RELEASE SAVEPOINT atm_action")
            return result
        except BaseException:
            db.execute("ROLLBACK TO SAVEPOINT atm_action")
            db.execute("RELEASE SAVEPOINT atm_action")
            raise
