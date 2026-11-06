"""Model-neutral bridge: two optional dialogue providers -> fenced NPC ticks.

This trusted, in-process host adapter accepts PR23 DialogueProvider callables,
including separately configured local GGUF adapters. It never starts a model,
downloads weights, creates a listener, grants memory, or hands a provider any
SQLite/Engine authority. Each turn is planned as a durable *single-actor* tick
and committed through PR33's lease-fenced atomic event/memory/receipt path.
The next speaker sees only explicitly granted ActorMemory, not room history or
Gateway.feed, which are not per-player privacy-qualified.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .actor_memory import ActorMemory
from .conversations import DialogueContext, DialogueProvider
from .engine import _PLAYER_RE
from .simulation import HostProposedAction, SimulationClock, SimulationIntegrityError


_SESSION = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MAX_TURNS = 20
_MODEL_CONTROL = re.compile(
    r"<\|[^\r\n<>]{1,80}\|>|</?(?:think|analysis|final|system|assistant|user|tool)>|"
    r"\[/?(?:INST|SYS|Start thinking|end of text)\]|"
    r"(?:^|\s)(?:system|developer|assistant|tool|model|TONE|FACT|OUT|PROMPT)\s*:",
    re.IGNORECASE,
)
_STAGE_VERBS = (
    "nods", "nodded", "smiles", "smiled", "sighs", "sighed", "walks",
    "walked", "turns", "turned", "looks", "looked", "shrugs", "shrugged",
)
_BRIDGE_SCHEMA = """CREATE TABLE IF NOT EXISTS npc_bridge_sessions (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    actor_a TEXT NOT NULL REFERENCES npc_actor_registry(actor),
    actor_b TEXT NOT NULL REFERENCES npc_actor_registry(actor),
    created_at TEXT NOT NULL,
    CHECK(actor_a<>actor_b)
)"""


@dataclass(frozen=True)
class BridgeTurn:
    session_id: str
    turn_index: int
    speaker: str
    listener: str
    tick_seq: int
    action_id: str
    status: str
    response: str
    replayed: bool


class ModelNeutralDuetBridge:
    """Host-gated sequential turns; models suggest text, never command actors."""

    def __init__(
        self, clock: SimulationClock, *, actor_a: str, actor_b: str,
        providers: Mapping[str, DialogueProvider], memory: ActorMemory,
    ) -> None:
        if not isinstance(clock, SimulationClock) or not isinstance(memory, ActorMemory):
            raise TypeError("An installed SimulationClock and ActorMemory are required")
        if memory.world_id != clock.expected_world_id or (
            Path(memory._engine.db_path).resolve() != Path(clock.engine.db_path).resolve()
        ):
            raise ValueError("Memory and simulation must belong to the same world database")
        if (not isinstance(actor_a, str) or not isinstance(actor_b, str)
                or not _PLAYER_RE.fullmatch(actor_a) or not _PLAYER_RE.fullmatch(actor_b)
                or actor_a.casefold() == actor_b.casefold()):
            raise ValueError("Two distinct exact host NPC actors are required")
        if not isinstance(providers, Mapping) or set(providers) != {actor_a, actor_b}:
            raise ValueError("Provide exactly one provider per host-selected actor")
        if any(not callable(provider) for provider in providers.values()):
            raise ValueError("Each provider must be a trusted host callable")
        self.clock = clock
        self.actor_a = actor_a
        self.actor_b = actor_b
        self.providers = dict(providers)
        self.memory = memory

    def _bind_session(self, session_id: str) -> str:
        with closing(self.clock.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            control = self.clock._control(db)
            # Session binding is host-owned. DDL lives inside this transaction,
            # never in an `executescript` that silently commits early.
            db.execute(_BRIDGE_SCHEMA)
            pair = (self.actor_a, self.actor_b)
            for actor in pair:
                row = db.execute(
                    "SELECT p.id FROM players p JOIN simulation_drives d ON d.actor=p.id "
                    "JOIN npc_actor_registry n ON n.actor=p.id WHERE p.id=? COLLATE NOCASE",
                    (actor,),
                ).fetchone()
                if row is None or row["id"] != actor:
                    raise ValueError("The duet references an unregistered simulation NPC")
            prior = db.execute(
                "SELECT instance_id,actor_a,actor_b FROM npc_bridge_sessions WHERE id=?", (session_id,),
            ).fetchone()
            binding = (control["instance_id"], *pair)
            if prior is not None:
                if tuple(prior) != binding:
                    raise ValueError("Session ID is already bound to another NPC pair or world")
            else:
                db.execute(
                    "INSERT INTO npc_bridge_sessions VALUES (?,?,?,?,?)",
                    (session_id, *binding, self.clock.engine._now()),
                )
            return control["instance_id"]

    @staticmethod
    def _action_id(instance_id: str, session_id: str, turn_index: int, actor: str) -> str:
        material = f"{instance_id}\0{session_id}\0{turn_index}\0{actor}".encode("utf-8")
        return "bridge:" + hashlib.sha256(material).hexdigest()

    def _turn_receipt(self, actor: str, action_id: str) -> sqlite3.Row | None:
        with closing(self.clock.engine._connect()) as db:
            return db.execute(
                "SELECT tick_seq,actor,action_id,status,response FROM simulation_intents "
                "WHERE actor=? AND action_id=?", (actor, action_id),
            ).fetchone()

    def _previous_observed_speech(self, actor: str, *, listener: str) -> str | None:
        # The memory class independently filters revocations, provenance,
        # exact source hashes, and per-observer grants. Do NOT use event SQL,
        # Engine.history, or Gateway.feed as a fallback if recall is empty.
        for memory in self.memory.recall(actor, limit=20):
            if memory["kind"] == "say" and memory["source_actor"] == listener:
                return memory["source_text"]  # Attributed utterance, not fact.
        return None

    @staticmethod
    def _guard_model_proposal(proposal: object, *, speaker: str, listener: str) -> Mapping[str, object]:
        """Reject common transcript/template leakage before any durable plan.

        This is intentionally a lexical safeguard, not a truthfulness,
        privacy, or speech-vs-narration semantic classifier. Providers may
        also impose narrower model-specific structured-stop requirements.
        """
        if not isinstance(proposal, Mapping) or set(proposal) != {"command"}:
            raise ValueError("Model may propose only one say command")
        command = proposal["command"]
        if not isinstance(command, str) or not command.startswith("say "):
            raise ValueError("Model may propose only printable say speech")
        speech = command[4:]
        if not speech or len(speech) > 280 or not all(c.isprintable() for c in speech):
            raise ValueError("Model speech must be bounded printable single-line text")
        if _MODEL_CONTROL.search(speech) or speech.lstrip().startswith(("*", "[", "```")):
            raise ValueError("Model speech resembles a role transcript or stage direction")
        actor_labels = "|".join(re.escape(n) for n in (speaker, listener, "the listener", "the speaker"))
        stage_verbs = "|".join(_STAGE_VERBS)
        if re.match(rf"^(?:{actor_labels})\s+(?:{stage_verbs})\b", speech, re.IGNORECASE):
            raise ValueError("Model output is actor narration rather than direct speech")
        return proposal

    def run(self, *, session_id: str, turns: int = 2) -> list[BridgeTurn]:
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            raise ValueError("Session ID must be a plain 1–64 character identifier")
        if type(turns) is not int or not 1 <= turns <= _MAX_TURNS:
            raise ValueError(f"Dialogue is bounded to 1–{_MAX_TURNS} turns")
        instance_id = self._bind_session(session_id)
        roster = (self.actor_a, self.actor_b)
        results: list[BridgeTurn] = []
        for index in range(turns):
            actor, listener = roster[index % 2], roster[(index + 1) % 2]
            action_id = self._action_id(instance_id, session_id, index, actor)
            stored = self._turn_receipt(actor, action_id)
            if stored is not None and stored["status"] == "proposed":
                # A crash after a durable plan must resume its exact stored
                # command without calling a nondeterministic provider again.
                self.clock.apply_pending_tick()
                stored = self._turn_receipt(actor, action_id)
            if stored is not None:
                if stored["status"] not in {"committed", "rejected"} or stored["response"] is None:
                    raise SimulationIntegrityError("Bridge turn has an unresolved or invalid receipt")
                result = BridgeTurn(
                    session_id, index, actor, listener, stored["tick_seq"],
                    action_id, stored["status"], stored["response"], True,
                )
            else:
                if self.clock.pending_tick() is not None:
                    raise ValueError("An unrelated pending tick must be settled first")
                speaker = self.clock.snapshot_npc_actor(actor)
                peer = self.clock.snapshot_npc_actor(listener)
                if speaker.room_id != peer.room_id:
                    raise ValueError("Conversation partners must share a room")
                context = DialogueContext(
                    speaker=actor, listener=listener, persona=speaker.persona,
                    room_id=speaker.room_id, room_name=speaker.room_name,
                    room_description=speaker.room_description,
                    previous_speech=self._previous_observed_speech(actor, listener=listener),
                    turn_index=index, session_id=session_id,
                )
                # Provider inference is fully outside any SQLite write lock.
                # Only a trusted host constructs the typed wrapper below;
                # model output may contain exactly one printable `say` command.
                proposal = self._guard_model_proposal(
                    self.providers[actor](context), speaker=actor, listener=listener,
                )
                self.clock.plan_next_tick(host_actions=(HostProposedAction(
                    snapshot=speaker, proposal=proposal,
                    action_id=action_id, required_listener=listener,
                ),))
                self.clock.apply_pending_tick()
                stored = self._turn_receipt(actor, action_id)
                if stored is None or stored["status"] not in {"committed", "rejected"}:
                    raise SimulationIntegrityError("Host tick failed to persist its terminal receipt")
                result = BridgeTurn(
                    session_id, index, actor, listener, stored["tick_seq"],
                    action_id, stored["status"], stored["response"], False,
                )
            results.append(result)
            if result.status != "committed":
                break  # A rejected turn is terminal; never invent next dialogue.
        return results
