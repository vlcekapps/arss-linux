from __future__ import annotations

import unittest

from arss.audio_session import (
    AudioInterruption,
    InterruptionMode,
    NoopAudioSession,
    RoleInterruptionPolicy,
    normalize_media_role,
)


class RoleInterruptionPolicyTest(unittest.TestCase):
    def test_accessibility_ducks_and_communication_takes_priority(self) -> None:
        events: list[AudioInterruption] = []
        policy = RoleInterruptionPolicy(events.append)

        policy.update("screen-reader", "Accessibility", True)
        policy.update("call", "Communication", True)
        policy.remove("call")
        policy.remove("screen-reader")

        self.assertEqual(
            [
                AudioInterruption.DUCK,
                AudioInterruption.TRANSIENT_LOSS,
                AudioInterruption.DUCK,
                AudioInterruption.GAIN,
            ],
            events,
        )
        self.assertEqual(InterruptionMode.NONE, policy.mode)

    def test_idle_nodes_and_unrelated_roles_do_not_interrupt(self) -> None:
        events: list[AudioInterruption] = []
        policy = RoleInterruptionPolicy(events.append)

        policy.update("idle-a11y", "a11y", False)
        policy.update("music", "Music", True)
        policy.update("unknown", "Orca", True)

        self.assertEqual([], events)
        self.assertTrue(policy.can_acquire())
        self.assertIsNone(policy.activation_event())

    def test_running_phone_role_blocks_new_acquisition(self) -> None:
        policy = RoleInterruptionPolicy()
        policy.update(7, "phone", True)

        self.assertEqual(InterruptionMode.PAUSE, policy.mode)
        self.assertFalse(policy.can_acquire())

    def test_normalizer_accepts_standard_roles_only(self) -> None:
        self.assertEqual("accessibility", normalize_media_role(" Accessibility "))
        self.assertEqual("a11y", normalize_media_role("A11Y"))
        self.assertEqual("communication", normalize_media_role("Communication"))
        self.assertIsNone(normalize_media_role("screen-reader"))
        self.assertIsNone(normalize_media_role(None))

    def test_noop_session_is_safe_and_always_grants_playback(self) -> None:
        session = NoopAudioSession()
        session.set_interruption_callback(lambda _event: None)

        self.assertTrue(session.acquire())
        session.release()
        session.close()


if __name__ == "__main__":
    unittest.main()
