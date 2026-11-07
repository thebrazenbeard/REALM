"""Bounded, host-driven REALM NPC turn scheduler.

This synchronous experiment performs no background execution or authority
discovery. The trusted host owns the ResidentTurnHost, observer, actor/world
binding, receipt allocator, tick timing and policy instance. A single policy
instance must never be shared between different residents or worlds.

The observer's world_id is *asserted by the host*, not independently attested
against Engine.db_path; the host must bind those identities itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

try:  # Source-checkout and direct experiments/ invocation.
    from .npc_policy import _observation
    from .realm_adapter import TurnResult
except ImportError:  # Unit-test import using experiment directory on sys.path.
    from npc_policy import _observation
    from realm_adapter import TurnResult


class TurnHostLike(Protocol):
    def step(self, *, action_id: str) -> TurnResult: ...


@dataclass(frozen=True)
class ScheduleResult:
    """Observed stopping condition; no inference about semantic success."""

    actor_id: str
    world_id: str
    reason: str
    turns: tuple[TurnResult, ...]


def _goal_complete(observation: Mapping[str, object]) -> bool:
    goal = observation["goal"]
    kind, target = goal["kind"], goal["target"]
    return ((kind == "reach" and target == observation["room"])
            or (kind == "take" and target in observation["inventory"]))


class ResidentTurnScheduler:
    """Execute up to ``max_turns`` permitted actions, stopping on completion.

    A fresh host-generated action ID is required for each new logical turn.
    Idempotency across process restarts depends on the host's persisted receipt
    allocator and the ResidentTurnHost receipt checks, not this in-memory set.
    """

    def __init__(
        self,
        host: TurnHostLike,
        observer: Callable[[], Mapping[str, object]],
        receipt_id_factory: Callable[[], str],
        *,
        actor_id: str,
        world_id: str,
        max_turns: int = 20,
        stall_limit: int = 3,
    ) -> None:
        if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 24 or not actor_id.isprintable():
            raise ValueError("actor_id must be a bounded host-provisioned identity")
        if not isinstance(world_id, str) or not 1 <= len(world_id) <= 128 or not world_id.isprintable():
            raise ValueError("world_id must be a bounded host-provisioned identity")
        if type(max_turns) is not int or not 1 <= max_turns <= 256:
            raise ValueError("max_turns must be 1-256")
        if type(stall_limit) is not int or not 1 <= stall_limit <= 10:
            raise ValueError("stall_limit must be 1-10")
        if not callable(observer) or not callable(receipt_id_factory) or not callable(getattr(host, "step", None)):
            raise ValueError("host, observer and receipt_id_factory must be callable")
        self._host = host
        self._observer = observer
        self._receipt_id_factory = receipt_id_factory
        self.actor_id = actor_id
        self.world_id = world_id
        self.max_turns = max_turns
        self.stall_limit = stall_limit
        self._used_ids: set[str] = set()

    def _observe(self) -> dict:
        observation = _observation(self._observer())
        if observation["world_id"] != self.world_id:
            raise ValueError("resident observation changed host-bound world_id")
        return observation

    def _allocate_action(self) -> str:
        action_id = self._receipt_id_factory()
        if (not isinstance(action_id, str) or not 1 <= len(action_id) <= 128
                or not action_id.isprintable() or action_id in self._used_ids):
            raise ValueError("host returned malformed or duplicate action receipt ID")
        if len(self._used_ids) >= 4096:
            raise ValueError("scheduler receipt budget exhausted")
        self._used_ids.add(action_id)
        return action_id

    def run(self) -> ScheduleResult:
        """Run a bounded batch at a host-chosen time, without sleeps/threads."""
        initial = self._observe()
        goal = initial["goal"]
        if _goal_complete(initial):
            return ScheduleResult(self.actor_id, self.world_id, "goal_complete", ())

        outcomes: list[TurnResult] = []
        stalls = 0
        current = initial
        for _ in range(self.max_turns):
            if current["goal"] != goal:
                return ScheduleResult(self.actor_id, self.world_id, "objective_changed", tuple(outcomes))
            if _goal_complete(current):
                return ScheduleResult(self.actor_id, self.world_id, "goal_complete", tuple(outcomes))
            action_id = self._allocate_action()
            outcome = self._host.step(action_id=action_id)
            if outcome.action_id != action_id:
                raise ValueError("resident host returned a mismatched action receipt ID")
            outcomes.append(outcome)
            later = self._observe()
            if later["goal"] != goal:
                return ScheduleResult(self.actor_id, self.world_id, "objective_changed", tuple(outcomes))
            if _goal_complete(later):
                return ScheduleResult(self.actor_id, self.world_id, "goal_complete", tuple(outcomes))
            if (current["room"], current["inventory"]) == (later["room"], later["inventory"]):
                stalls += 1
                if stalls >= self.stall_limit:
                    return ScheduleResult(self.actor_id, self.world_id, "stalled", tuple(outcomes))
            else:
                stalls = 0
            current = later
        return ScheduleResult(self.actor_id, self.world_id, "budget_exhausted", tuple(outcomes))
