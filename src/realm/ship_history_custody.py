"""Full in-transaction ship ledger postconditions for opt-in UTC host ticks.

This is a bounded *WorldClock.tick* guard, not global SQLite tamper-proofing.
Other legitimate writers (authenticated gateway departures and explicit host
geography mutations) have different transaction boundaries. A privileged
SQLite trigger is modeled here as a hostile pre-installed host-side hook, not
as a remotely reachable player exploit.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

_TABLES = ("geo_ships", "geo_ship_events", "geo_ship_receipts")
_SHIP_COLS = ("id", "name", "owner", "planet_id", "destination", "arrival_at")
_EVENT_COLS = ("id", "occurred_at", "ship_id", "actor", "kind", "payload_json")
_RECEIPT_COLS = ("ship_id", "actor", "action_id", "command", "response_json")


def _rows(db: sqlite3.Connection, table: str, columns: tuple[str, ...],
          key_columns: tuple[str, ...]) -> dict:
    return {
        tuple(row[c] for c in key_columns): tuple(row[c] for c in columns)
        for row in db.execute(f"SELECT {','.join(columns)} FROM main.{table}")
    }


class ShipHistoryCustody:
    """Compare complete ship-state and history effects against selected UTC work.

    Snapshot before *any* callback, verify each permitted ship transition right
    after its callback, then recheck all three complete tables AFTER scheduler
    receipt and clock-state writes. Even a later audit-only or idle tick must
    preserve prior ship history, including rivals' events and owners.
    """

    def __init__(self, db: sqlite3.Connection):
        present = {
            row["name"] for row in db.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' "
                "AND name IN ('geo_ships','geo_ship_events','geo_ship_receipts')"
            )
        }
        if not present:
            self.enabled = False
            return
        if present != set(_TABLES):
            raise ValueError("Ship UTC custody requires a complete geography ledger")
        self.enabled = True
        self.ships, self.events, self.receipts = self._snapshot(db)
        self.max_event_id = max((key[0] for key in self.events), default=0)

    @staticmethod
    def _snapshot(db: sqlite3.Connection) -> tuple[dict, dict, dict]:
        return (
            _rows(db, "geo_ships", _SHIP_COLS, ("id",)),
            _rows(db, "geo_ship_events", _EVENT_COLS, ("id",)),
            _rows(db, "geo_ship_receipts", _RECEIPT_COLS,
                  ("ship_id", "actor", "action_id")),
        )

    def _verify(self, db: sqlite3.Connection) -> None:
        if self._snapshot(db) != (self.ships, self.events, self.receipts):
            raise ValueError("Ship UTC ledger changed outside authorized clock effects")

    @staticmethod
    def _required_ship_id(event) -> str:
        # Only the typed, exact ship_id is delegated by the scheduled event.
        ship_id = event.payload.get("ship_id")
        if (type(ship_id) is not str or not ship_id
                or any(k not in ("ship_id", "destination") for k in event.payload)):
            raise ValueError("Ship UTC event lacks exact ship binding")
        return ship_id

    def after_handler(self, db: sqlite3.Connection, event) -> None:
        """Authorize exactly one transition by a selected host ship event."""
        if not self.enabled:
            if event.kind in ("ship_arrival", "ship_departure"):
                raise ValueError("Ship UTC event requires enabled geography ledger")
            return
        if event.kind not in ("ship_arrival", "ship_departure"):
            self._verify(db)
            return

        ship_id = self._required_ship_id(event)
        ship = self.ships.get((ship_id,))
        if ship is None:
            raise ValueError("Ship UTC event references unknown bound ship")
        at = int(event.due_at.timestamp())
        if datetime.fromtimestamp(at, tz=timezone.utc) != event.due_at:
            raise ValueError("Ship UTC event requires an exact integral UTC instant")
        # Validate the documented Geography transition from the *prior* bound
        # state, not the post-trigger values or a callback's untrusted response.
        old_planet, destination, arrival_at = ship[3], ship[4], ship[5]
        if event.kind == "ship_arrival":
            if (set(event.payload) != {"ship_id"} or destination is None
                    or arrival_at is None or at < arrival_at):
                raise ValueError("Ship UTC arrival lacks a due in-transit ship")
            next_ship = (ship[0], ship[1], ship[2], destination, None, None)
            actor, kind, command = "system", "arrive", "arrive"
            payload = {"from": old_planet, "to": destination}
            response = {"ship": ship_id, "state": "docked", "planet": destination}
        else:
            destination = event.payload.get("destination")
            if (set(event.payload) != {"ship_id", "destination"}
                    or type(destination) is not str or not destination
                    or ship[4] is not None or ship[5] is not None):
                raise ValueError("Ship UTC departure lacks exact docked route")
            route = db.execute(
                "SELECT seconds FROM main.geo_links WHERE origin=? AND destination=? "
                "AND mode='ship'", (old_planet, destination),
            ).fetchone()
            if route is None:
                raise ValueError("Ship UTC departure requires declared direct route")
            arrival = at + route["seconds"]
            if not 0 <= arrival <= (1 << 63) - 1:
                raise ValueError("Ship UTC arrival epoch overflow")
            next_ship = (ship[0], ship[1], ship[2], old_planet, destination, arrival)
            actor, kind, command = ship[2], "depart", "depart:" + destination
            payload = {"from": old_planet, "to": destination, "arrival_at": arrival}
            response = {"ship": ship_id, "state": "in_transit", **payload}
            # The opted-in host departure must also enqueue its exact future
            # arrival; otherwise a 'successful' clock ACK can strand a ship.
            event_key = json.dumps(
                [ship_id, actor, event.event_id], ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            arrival_id = "ship_arrival_" + hashlib.sha256(event_key).hexdigest()[:32]
            arrival_row = db.execute(
                "SELECT due_utc,kind,payload_json,processed_utc "
                "FROM main.clock_events WHERE event_id=?", (arrival_id,),
            ).fetchone()
            due = datetime.fromtimestamp(arrival, tz=timezone.utc).isoformat(
                timespec="microseconds")
            if (arrival_row is None
                    or tuple(arrival_row) != (
                        due, "ship_arrival",
                        json.dumps({"ship_id": ship_id}, sort_keys=True,
                                   separators=(",", ":")), None)):
                raise ValueError("Ship UTC departure lacks bound scheduled arrival")

        receipt_key = (ship_id, actor, event.event_id)
        if receipt_key in self.receipts:
            raise ValueError("Ship UTC event already has a prior receipt")
        expected_receipts = dict(self.receipts)
        expected_receipts[receipt_key] = (
            ship_id, actor, event.event_id, command,
            json.dumps(response, sort_keys=True),
        )
        expected_ships = dict(self.ships)
        expected_ships[(ship_id,)] = next_ship
        current_ships, current_events, current_receipts = self._snapshot(db)
        if current_ships != expected_ships or current_receipts != expected_receipts:
            raise ValueError("Ship UTC state/receipt effects diverged from scheduled grant")
        added = set(current_events) - set(self.events)
        if (len(added) != 1 or any(key not in current_events for key in self.events)
                or any(current_events[key] != original for key, original in self.events.items())):
            raise ValueError("Ship UTC history contains ungranted or rewritten effects")
        new_id = next(iter(added))[0]
        if type(new_id) is not int or new_id <= self.max_event_id:
            raise ValueError("Ship UTC history contains out-of-sequence inserted event")
        expected_new_event = (
            new_id, at, ship_id, actor, kind, json.dumps(payload, sort_keys=True),
        )
        if current_events[(new_id,)] != expected_new_event:
            raise ValueError("Ship UTC history event mismatches authorized transition")
        self.ships = expected_ships
        self.receipts = expected_receipts
        self.events = current_events
        self.max_event_id = new_id

    def before_commit(self, db: sqlite3.Connection) -> None:
        """Final, read-only guard after *all* UTC batch DML, including idle ticks."""
        if self.enabled:
            self._verify(db)
