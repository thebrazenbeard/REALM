"""Optional setting-neutral atmosphere kernel. No service, DB, or medical authority.

Units: volume in liters, pressure in pascals, temperature in millikelvin,
oxygen fraction in parts-per-million. gas_pa_l and oxygen_pa_l are integer
pressure-volume inventories for an explicitly *isothermal* synthetic model,
not a physical CFD or thermodynamic solver. One host tick transfers at most a
quarter of the ideal pressure difference per hatch, deterministically.

Exposure flags currently use synthetic default thresholds: 30,000 Pa ambient
and 10,000 Pa oxygen partial pressure. These are not clinical prognoses,
species physiology, or canonical world rules. The sealed-suit flag is only a
nominal shielding assumption; air supply and suit failure are not modeled.
"""
from copy import deepcopy
import re

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_VOLUME = 10_000_000
_MAX_PRESSURE = 10_000_000
_MAX_GAS = _MAX_VOLUME * _MAX_PRESSURE
_MAX_RATE = _MAX_GAS


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid atmosphere identifier")
    return value


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"Invalid {name}")
    return value


class AtmosphereModel:
    """Host-invoked pure in-memory domain layer, not an authentication boundary."""

    def __init__(self, *, compartments, hatches, occupants):
        if not isinstance(compartments, dict) or not 1 <= len(compartments) <= 512:
            raise ValueError("Invalid compartments")
        if not isinstance(hatches, dict) or len(hatches) > 1024:
            raise ValueError("Invalid hatches")
        if not isinstance(occupants, dict) or len(occupants) > 4096:
            raise ValueError("Invalid occupants")
        self.compartments = {}
        for name, spec in compartments.items():
            _id(name)
            if not isinstance(spec, dict):
                raise ValueError("Invalid compartment")
            v = _integer(spec.get("volume_l"), "volume_l", 1, _MAX_VOLUME)
            p = _integer(spec.get("pressure_pa"), "pressure_pa", 0, _MAX_PRESSURE)
            o = _integer(spec.get("oxygen_ppm"), "oxygen_ppm", 0, 1_000_000)
            t = _integer(spec.get("temperature_mk"), "temperature_mk", 1, 2_000_000)
            gas = p * v
            self.compartments[name] = {
                "volume_l": v, "gas_pa_l": gas,
                "oxygen_pa_l": gas * o // 1_000_000,
                "temperature_mk": t,
            }
        self.hatches = {}
        for name, spec in hatches.items():
            _id(name)
            if not isinstance(spec, dict):
                raise ValueError("Invalid hatch")
            a, b = _id(spec.get("a")), _id(spec.get("b"))
            if a == b or a not in self.compartments or b not in self.compartments:
                raise ValueError("Unknown hatch compartment")
            operators = spec.get("operators")
            if not isinstance(operators, (tuple, list)) or not operators:
                raise ValueError("Hatch operators required")
            operators = tuple(_id(op) for op in operators)
            is_open, operational = spec.get("open"), spec.get("operational")
            if not isinstance(is_open, bool) or not isinstance(operational, bool):
                raise ValueError("Invalid hatch state")
            self.hatches[name] = {
                "a": a, "b": b, "operators": operators,
                "open": is_open, "operational": operational,
            }
        self.occupants = {}
        for actor, spec in occupants.items():
            _id(actor)
            if not isinstance(spec, dict) or spec.get("compartment") not in self.compartments:
                raise ValueError("Unknown occupant compartment")
            if not isinstance(spec.get("sealed_suit"), bool):
                raise ValueError("Explicit sealed_suit required")
            self.occupants[actor] = dict(spec)
        self.breaches = {}
        self._receipts = {}

    def snapshot(self):
        rooms = {}
        for name, c in sorted(self.compartments.items()):
            v, gas = c["volume_l"], c["gas_pa_l"]
            rooms[name] = {
                **c, "pressure_pa": gas // v,
                "oxygen_ppm": (c["oxygen_pa_l"] * 1_000_000 // gas) if gas else 0,
            }
        return {
            "compartments": rooms,
            "hatches": deepcopy(self.hatches),
            "breaches": dict(self.breaches),
            "occupants": deepcopy(self.occupants),
        }

    def _replay(self, action_id, command):
        _id(action_id)
        previous = self._receipts.get(action_id)
        if previous is None:
            return None
        if previous[0] != command:
            raise ValueError("Action ID already used for different command")
        return deepcopy(previous[1])

    def _store(self, action_id, command, result):
        self._receipts[action_id] = (command, deepcopy(result))
        return deepcopy(result)

    def hatch(self, hatch_id, *, open, actor, action_id):
        command = ("hatch", hatch_id, open, actor)
        replay = self._replay(action_id, command)
        if replay is not None:
            return replay
        if hatch_id not in self.hatches or not isinstance(open, bool):
            raise ValueError("Invalid hatch operation")
        _id(actor)
        hatch = self.hatches[hatch_id]
        if actor not in hatch["operators"]:
            raise PermissionError("Actor lacks hatch operating permission")
        if not hatch["operational"]:
            raise ValueError("Hatch is not operational")
        if open and (self.compartments[hatch["a"]]["temperature_mk"]
                     != self.compartments[hatch["b"]]["temperature_mk"]):
            raise ValueError("Isothermal kernel rejects mismatched temperatures")
        hatch["open"] = open
        return self._store(action_id, command, self.snapshot())

    def breach(self, chamber_id, *, loss_pa_l_per_tick, action_id):
        command = ("breach", chamber_id, loss_pa_l_per_tick)
        replay = self._replay(action_id, command)
        if replay is not None:
            return replay
        if chamber_id not in self.compartments:
            raise ValueError("Unknown compartment")
        _integer(loss_pa_l_per_tick, "loss_pa_l_per_tick", 1, _MAX_RATE)
        self.breaches[chamber_id] = loss_pa_l_per_tick
        return self._store(action_id, command, self.snapshot())

    def tick(self, *, action_id):
        command = ("tick",)
        replay = self._replay(action_id, command)
        if replay is not None:
            return replay
        # Validate every open edge before any inventory transfer: one broken
        # compartment cannot cause a partially applied host tick.
        for hatch in self.hatches.values():
            if hatch["open"] and (self.compartments[hatch["a"]]["temperature_mk"]
                                  != self.compartments[hatch["b"]]["temperature_mk"]):
                raise ValueError("Isothermal kernel rejects mismatched temperatures")
        candidate = deepcopy(self.compartments)
        events = []
        for name, hatch in sorted(self.hatches.items()):
            if not hatch["open"]:
                continue
            a, b = candidate[hatch["a"]], candidate[hatch["b"]]
            if a["temperature_mk"] != b["temperature_mk"]:
                raise ValueError("Isothermal kernel rejects mismatched temperatures")
            av, bv = a["volume_l"], b["volume_l"]
            pa, pb = a["gas_pa_l"] // av, b["gas_pa_l"] // bv
            if pa == pb:
                continue
            source, target = (a, b) if pa > pb else (b, a)
            transfer_to_equalize = abs(pa - pb) * av * bv // (av + bv)
            # Integer gas inventory cannot express a sub-unit transfer. Forcing
            # one unit here makes a 1 Pa / 0 Pa pair oscillate forever.
            transfer = min(source["gas_pa_l"], transfer_to_equalize // 4)
            if transfer == 0:
                continue
            oxygen = source["oxygen_pa_l"] * transfer // source["gas_pa_l"]
            source["gas_pa_l"] -= transfer
            source["oxygen_pa_l"] -= oxygen
            target["gas_pa_l"] += transfer
            target["oxygen_pa_l"] += oxygen
            events.append({"kind": "equalization", "hatch": name, "gas_pa_l": transfer})
        for room, rate in sorted(self.breaches.items()):
            c = candidate[room]
            if c["gas_pa_l"] <= 0:
                continue
            vented = min(c["gas_pa_l"], rate)
            oxygen = c["oxygen_pa_l"] * vented // c["gas_pa_l"]
            c["gas_pa_l"] -= vented
            c["oxygen_pa_l"] -= oxygen
            events.append({"kind": "vent", "compartment": room, "gas_pa_l": vented})
        # Do not publish partial pressure changes if any breach or occupant
        # observation raises. This is a pure in-memory staged commit only.
        observed = self._exposures_from(candidate)
        self.compartments = candidate
        return self._store(action_id, command, {"events": events, "exposures": observed})

    def exposures(self):
        return self._exposures_from(self.compartments)

    def _exposures_from(self, compartments):
        results = []
        for actor, spec in sorted(self.occupants.items()):
            c = compartments[spec["compartment"]]
            pressure = c["gas_pa_l"] // c["volume_l"]
            partial_oxygen = c["oxygen_pa_l"] // c["volume_l"]
            low_pressure = pressure < 30_000
            low_oxygen = partial_oxygen < 10_000
            protected = spec["sealed_suit"]
            results.append({
                "actor": actor, "compartment": spec["compartment"],
                "low_pressure": low_pressure,
                "low_oxygen": low_oxygen,
                "effective_low_pressure": low_pressure and not protected,
                "effective_low_oxygen": low_oxygen and not protected,
                "temperature_mk": c["temperature_mk"],
                "protected": protected,
            })
        return results
