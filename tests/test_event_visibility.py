"""Pure proposed per-world event audience policy, NOT a Gateway privacy pass."""

from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.event_visibility import (
    EventAudience, ReadPurpose, ViewerGrant, Visibility,
    VisibilityPolicyError, WorldVisibilityPolicy, can_read_event,
    classify_event, revoke_event_access,
)


class EventVisibilityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = WorldVisibilityPolicy("woodland")

    def viewer(self, actor, *, purpose=ReadPurpose.FEED, world="woodland",
               member=True, capabilities=frozenset()):
        return ViewerGrant(world_id=world, principal=actor, purpose=purpose,
                           world_member=member, capabilities=capabilities)

    def say(self, policy=None, *, witnessed=frozenset({"Early"}), memory=False):
        return classify_event(policy or self.policy, event_id=3, kind="say",
                              actor="Early", room_id="square",
                              witnessed_ids=witnessed, memory_consent=memory)

    def test_default_speech_is_witnessed_at_event_time_not_current_room(self):
        record = self.say()
        self.assertIs(record.visibility, Visibility.WITNESSED_ROOM)
        self.assertTrue(can_read_event(self.policy, record, self.viewer("Early")))
        # Late is standing in `square` *now*, but wasn't a witness when the
        # event committed. Current room cannot authorize past content.
        self.assertFalse(can_read_event(self.policy, record, self.viewer("Late")))
        self.assertFalse(can_read_event(self.policy, record, self.viewer("Early", member=False)))
        self.assertFalse(can_read_event(self.policy, record, self.viewer("Early", world="other")))
        self.assertTrue(can_read_event(self.policy, record, self.viewer("early", purpose=ReadPurpose.HISTORY)))

    def test_public_chronicle_is_legitimate_when_world_and_event_explicitly_opt_in(self):
        public = replace(self.policy, allow_public_chronicle=True)
        with self.assertRaises(VisibilityPolicyError):
            classify_event(self.policy, event_id=4, kind="say", actor="Early",
                           visibility=Visibility.PUBLIC_CHRONICLE)
        with self.assertRaises(VisibilityPolicyError):
            classify_event(public, event_id=4, kind="say", actor="Early",
                           visibility=Visibility.PUBLIC_CHRONICLE,
                           explicit_public_consent=False)
        with self.assertRaises(VisibilityPolicyError):
            classify_event(public, event_id=4, kind="say", actor="Early",
                           visibility=Visibility.PUBLIC_CHRONICLE,
                           explicit_public_consent=True,
                           recipient_ids=frozenset({"PrivateRecipient"}))
        record = classify_event(public, event_id=4, kind="say", actor="Early",
                                visibility=Visibility.PUBLIC_CHRONICLE,
                                explicit_public_consent=True)
        self.assertTrue(can_read_event(public, record, self.viewer("LaterReader")))
        self.assertTrue(can_read_event(public, record, self.viewer("LaterReader", purpose=ReadPurpose.HISTORY)))
        self.assertFalse(can_read_event(self.policy, record, self.viewer("LaterReader")))
        self.assertFalse(can_read_event(public, record, self.viewer("Anonymous", member=False)))
        # Enabling a public policy later does not retroactively promote a
        # previously witnessed-only event.
        self.assertFalse(can_read_event(public, self.say(public), self.viewer("LaterReader")))

    def test_private_message_is_sender_and_recipient_only_not_room_witness(self):
        private = classify_event(
            self.policy, event_id=5, kind="private_message", actor="Early",
            room_id="square", witnessed_ids=frozenset({"Early", "Bystander"}),
            recipient_ids=frozenset({"Friend"}),
        )
        self.assertIs(private.visibility, Visibility.PRIVATE_MESSAGE)
        for actor in ("Early", "Friend"):
            self.assertTrue(can_read_event(self.policy, private, self.viewer(actor)))
        self.assertFalse(can_read_event(self.policy, private, self.viewer("Bystander")))
        self.assertFalse(can_read_event(self.policy, private, self.viewer("Moderator", purpose=ReadPurpose.MODERATION,
                                                                        capabilities=frozenset({"moderation_read"}))))
        with self.assertRaises(VisibilityPolicyError):
            classify_event(self.policy, event_id=6, kind="private_message", actor="Early")
        with self.assertRaises(VisibilityPolicyError):
            classify_event(replace(self.policy, allow_public_chronicle=True),
                           event_id=6, kind="private_message", actor="Early",
                           visibility=Visibility.PUBLIC_CHRONICLE,
                           explicit_public_consent=True)

    def test_moderation_ledger_is_separate_from_public_history(self):
        record = classify_event(self.policy, event_id=7, kind="moderation_note", actor="Host")
        self.assertIs(record.visibility, Visibility.MODERATION_LEDGER)
        moderator = self.viewer("Moderator", purpose=ReadPurpose.MODERATION,
                                capabilities=frozenset({"moderation_read"}))
        self.assertTrue(can_read_event(self.policy, record, moderator))
        self.assertFalse(can_read_event(self.policy, record, self.viewer("Moderator")))
        self.assertFalse(can_read_event(self.policy, record, self.viewer("Moderator", purpose=ReadPurpose.MODERATION)))
        self.assertFalse(can_read_event(self.policy, record, self.viewer("Player")))
        with self.assertRaises(VisibilityPolicyError):
            classify_event(replace(self.policy, allow_public_chronicle=True),
                           event_id=7, kind="moderation_note", actor="Host",
                           visibility=Visibility.PUBLIC_CHRONICLE,
                           explicit_public_consent=True)

    def test_npc_memory_needs_separate_world_and_event_consent(self):
        memory_viewer = self.viewer("Early", purpose=ReadPurpose.NPC_MEMORY)
        record = self.say(memory=True)
        self.assertFalse(can_read_event(self.policy, record, memory_viewer))
        memory_world = replace(self.policy, allow_npc_memory=True)
        self.assertTrue(can_read_event(memory_world, record, memory_viewer))
        self.assertFalse(can_read_event(memory_world, self.say(memory=False), memory_viewer))
        self.assertFalse(can_read_event(memory_world, record, self.viewer("OtherNPC", purpose=ReadPurpose.NPC_MEMORY)))

    def test_revocation_hides_content_without_rewriting_canonical_event(self):
        record = self.say(witnessed=frozenset({"Early", "Friend"}))
        revoked = revoke_event_access(record, principal="Friend")
        self.assertEqual(record.event_id, revoked.event_id)
        self.assertTrue(can_read_event(self.policy, revoked, self.viewer("Early")))
        self.assertFalse(can_read_event(self.policy, revoked, self.viewer("Friend")))
        fully = revoke_event_access(revoked)
        self.assertFalse(can_read_event(self.policy, fully, self.viewer("Early")))
        public_world = replace(self.policy, allow_public_chronicle=True)
        chronicle = classify_event(public_world, event_id=8, kind="say", actor="Host",
                                   visibility=Visibility.PUBLIC_CHRONICLE,
                                   explicit_public_consent=True)
        self.assertFalse(can_read_event(public_world, revoke_event_access(chronicle), self.viewer("Late")))

    def test_legacy_unsupported_or_forged_record_fails_closed(self):
        early = self.viewer("Early")
        self.assertFalse(can_read_event(self.policy, None, early))
        record = self.say()
        self.assertFalse(can_read_event(self.policy, replace(record, visibility="public_chronicle"), early))
        self.assertFalse(can_read_event(self.policy, replace(record, policy_revision="unknown"), early))
        self.assertFalse(can_read_event(self.policy, replace(record, world_id="other"), early))
        self.assertFalse(can_read_event(self.policy, replace(record, witnessed_ids="Early"), early))
        self.assertFalse(can_read_event(self.policy, replace(record, event_id=True), early))
        self.assertFalse(can_read_event(self.policy, record, replace(early, world_member="yes")))
        self.assertFalse(can_read_event(self.policy, record, replace(early, purpose="feed")))
        with self.assertRaises(VisibilityPolicyError):
            classify_event(self.policy, event_id=9, kind="say", actor="Early",
                           room_id="square", witnessed_ids=frozenset(),
                           visibility=Visibility.WITNESSED_ROOM)
        with self.assertRaises(VisibilityPolicyError):
            classify_event(self.policy, event_id=10, kind="say", actor="Early",
                           visibility="public_chronicle")

    def test_same_event_numbers_across_worlds_are_never_shared_grants(self):
        a = self.say()
        other_policy = WorldVisibilityPolicy("different_world")
        b = classify_event(other_policy, event_id=3, kind="say", actor="Early",
                           room_id="square", witnessed_ids=frozenset({"Early"}))
        self.assertFalse(can_read_event(self.policy, b, self.viewer("Early")))
        self.assertFalse(can_read_event(other_policy, a, self.viewer("Early", world="different_world")))
        self.assertTrue(can_read_event(other_policy, b, self.viewer("Early", world="different_world")))


if __name__ == "__main__":
    unittest.main()
