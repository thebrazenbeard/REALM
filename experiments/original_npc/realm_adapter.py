"""Host-owned REALM NPC turn adapter (no database/model authority).

Compatible with REALM ``ResidentController.validate`` and ``commit`` on
``thebrazenbeard/REALM`` main 09fc7cf6fcd8f7826c315d69c045467f27a7c8c7.
This file intentionally does not import Engine, SQLite, or a model runtime.

A trusted host creates/provisions the controller, supplies an authorized
per-character observation and an action receipt ID. The policy sees only a
JSON snapshot and returns only a command proposal. It never receives the
controller, filesystem paths, player credentials, or a database connection.
This is *not* a sandbox for malicious Python policies running in-process.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol


class ResidentControllerLike(Protocol):
    def validate(self, proposal: Mapping[str, object]) -> object: ...

    def commit(self, proposal: Mapping[str, object], *, action_id: str) -> str: ...


class ResidentPolicyLike(Protocol):
    def propose(self, observation: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class TurnResult:
    """A commit response, not a declaration that a gameplay action succeeded."""

    action_id: str
    command: str
    response: str
    learning_status: str


OutcomeEvaluator = Callable[[Mapping[str, object], str, Mapping[str, object], str], float]
_OBSERVATION_FIELDS = frozenset({
    "world_id", "room", "exits", "items", "inventory", "goal", "allowed_commands",
})


class ResidentTurnHost:
    """Orchestrate one host-authorized NPC turn through REALM's controller.

    ``observer`` must enforce world/actor/event visibility and produce a JSON
    compatible snapshot. ``allowed_commands`` is an exact host-generated
    candidate list, not a suggestion the model may extend. ``action_id`` is
    chosen by the host and must be stable for a retry of the same logical turn.
    """

    def __init__(
        self,
        controller: ResidentControllerLike,
        observer: Callable[[], Mapping[str, object]],
        policy: ResidentPolicyLike,
        *,
        outcome_evaluator: OutcomeEvaluator | None = None,
        receipt_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._controller = controller
        self._observer = observer
        self._policy = policy
        self._evaluator = outcome_evaluator
        # Host-owned actor/world-scoped persisted receipt lookup. It must be
        # coordinated with same-actor writers; a read by itself is not an
        # atomic cross-process claim of first execution.
        self._receipt_exists = receipt_exists
        self._pending: dict[str, tuple[str, dict[str, object]]] = {}
        self._completed: dict[str, TurnResult] = {}
        self._bound_world_id: str | None = None

    def _snapshot(self) -> dict[str, object]:
        source = self._observer()
        if not isinstance(source, Mapping):
            raise ValueError("Host observer must return a mapping")
        # Project an explicit least-information view. The observer may hold
        # credentials, actor identity, private events, or non-JSON handles;
        # none may reach the policy or reward feedback through an extra key.
        if any(field not in source for field in _OBSERVATION_FIELDS):
            raise ValueError("Host observation is missing required public fields")
        raw_goal = source["goal"]
        if not isinstance(raw_goal, Mapping):
            raise ValueError("Host observation requires an approved goal mapping")
        projected = {field: source[field] for field in _OBSERVATION_FIELDS if field != "goal"}
        projected["goal"] = {key: raw_goal[key] for key in ("kind", "target") if key in raw_goal}
        # JSON round-trip refuses executable handles, objects, and cyclic data.
        encoded = json.dumps(projected, allow_nan=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("NPC observation exceeds 64 KiB")
        snapshot = json.loads(encoded)
        if not isinstance(snapshot.get("world_id"), str) or not snapshot["world_id"]:
            raise ValueError("Host observation requires a world_id")
        if self._bound_world_id is None:
            self._bound_world_id = snapshot["world_id"]
        elif snapshot["world_id"] != self._bound_world_id:
            raise ValueError("Resident observation changed world_id")
        commands = snapshot.get("allowed_commands")
        if not isinstance(commands, list) or not 1 <= len(commands) <= 32:
            raise ValueError("Host observation requires 1-32 allowed commands")
        if any(not isinstance(c, str) or not c or len(c) > 512 for c in commands):
            raise ValueError("Host observation contains malformed commands")
        return snapshot

    def step(self, *, action_id: str) -> TurnResult:
        """Propose -> host validate -> commit -> optional verified-outcome learn.

        Reuse of the same action ID within this process reuses its original
        proposal; the underlying Engine receipt provides durable deduplication.
        A cross-process retry still needs the host to recover that same command
        or the Engine will correctly reject a conflicting action ID.
        """
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
            raise ValueError("Host must supply a receipt ID of 1-128 characters")
        if action_id in self._completed:
            return self._completed[action_id]

        if action_id in self._pending:
            command, before = self._pending[action_id]
            # A previously chosen, uncommitted decision is *not* an enduring
            # authorization. Re-check the host's current visibility/policy.
            fresh = self._snapshot()
            if command not in fresh["allowed_commands"]:
                raise ValueError("Pending NPC command is no longer authorized")
            if fresh != before:
                # Room, goal, visible facts, or candidate ordering may have
                # changed while the logical action was pending. Do not apply
                # a decision from the previous situation to the new one.
                # A possibly committed earlier attempt needs separate durable
                # receipt reconciliation; fail closed here.
                raise ValueError("Pending NPC observation changed; reconcile receipt")
        else:
            before = self._snapshot()
            # The policy receives a detached copy, never the host's own state.
            proposal = self._policy.propose(copy.deepcopy(before))
            if not isinstance(proposal, Mapping) or set(proposal) != {"command"}:
                raise ValueError("NPC may propose only {'command': <str>}")
            command = proposal["command"]
            if not isinstance(command, str) or command not in before["allowed_commands"]:
                raise ValueError("NPC command is not in the host's allowed actions")
            self._pending[action_id] = (command, before)

        envelope = {"command": command}
        # A REALM ResidentController applies its separate verb and syntax gate.
        self._controller.validate(envelope)
        preexisting: bool | None = None
        if self._receipt_exists is not None:
            try:
                preexisting = self._receipt_exists(action_id)
            except Exception as exc:
                raise ValueError("Trusted receipt witness failed before commit") from exc
            if type(preexisting) is not bool:
                raise ValueError("Trusted receipt witness must return bool")
        response = self._controller.commit(envelope, action_id=action_id)
        status = (
            "replayed_receipt" if preexisting is True else
            "disabled" if self._evaluator is None else
            "receipt_unverified" if preexisting is None else "disabled"
        )
        if (self._evaluator is not None and preexisting is False
                and callable(getattr(self._policy, "learn", None))):
            try:
                after = self._snapshot()
                reward = self._evaluator(copy.deepcopy(before), command, copy.deepcopy(after), response)
                if isinstance(reward, bool) or not isinstance(reward, (float, int)) or not math.isfinite(reward):
                    status = "invalid_reward"
                else:
                    accepted = self._policy.learn(  # type: ignore[attr-defined]
                        copy.deepcopy(before), command, copy.deepcopy(after),
                        reward=float(reward), receipt_id=action_id,
                    )
                    status = "learned_with_host_witness" if accepted else "not_learned"
            except Exception:
                # The world action already committed; never replay it because
                # an optional learning/evaluation step failed.
                status = "learning_error"
        result = TurnResult(action_id, command, response, status)
        self._completed[action_id] = result
        self._pending.pop(action_id, None)
        return result
