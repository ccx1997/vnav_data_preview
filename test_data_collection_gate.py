from __future__ import annotations

import math
import random
import unittest

from data_collection_gate import (
    PoseSample,
    StraightGate,
    StraightGateConfig,
    TurnGate,
    TurnGateConfig,
)


def _straight(duration_s: float = 12.0, speed_mps: float = 0.5) -> list[PoseSample]:
    return [
        PoseSample(index / 10.0, speed_mps * index / 10.0, 0.0, 0.0)
        for index in range(int(duration_s * 10) + 1)
    ]


def _arc(curvature_rad_per_m: float, duration_s: float = 12.0) -> list[PoseSample]:
    speed_mps = 0.5
    samples = []
    for index in range(int(duration_s * 10) + 1):
        timestamp_s = index / 10.0
        distance_m = speed_mps * timestamp_s
        angle = curvature_rad_per_m * distance_m
        radius_m = 1.0 / curvature_rad_per_m
        samples.append(
            PoseSample(
                timestamp_s,
                radius_m * math.sin(angle),
                radius_m * (1.0 - math.cos(angle)),
                angle,
            )
        )
    return samples


def _motion_profile(
    segments: list[tuple[float, float, float]], dt_s: float = 0.1
) -> list[PoseSample]:
    """Build ``(duration_s, speed_mps, curvature_rad_per_m)`` segments."""

    timestamp_s = 0.0
    x_m = 0.0
    y_m = 0.0
    yaw_rad = 0.0
    samples = [PoseSample(timestamp_s, x_m, y_m, yaw_rad)]
    for duration_s, speed_mps, curvature in segments:
        for _ in range(int(round(duration_s / dt_s))):
            distance_m = speed_mps * dt_s
            yaw_delta = curvature * distance_m
            midpoint_yaw = yaw_rad + 0.5 * yaw_delta
            x_m += distance_m * math.cos(midpoint_yaw)
            y_m += distance_m * math.sin(midpoint_yaw)
            yaw_rad += yaw_delta
            timestamp_s += dt_s
            samples.append(PoseSample(timestamp_s, x_m, y_m, yaw_rad))
    return samples


class TurnGateTest(unittest.TestCase):
    def test_straight_does_not_start_inactive_turn_gate(self) -> None:
        gate = TurnGate()
        samples = _straight()

        evidence = gate.evaluate(samples, 3.0)

        self.assertTrue(evidence.sufficient_data)
        self.assertFalse(evidence.is_turn)
        self.assertEqual(gate.start_collection(samples, 3.0), (False, 3.0))
        self.assertEqual(gate.end_collection(samples, 5.0), (False, 5.0))

    def test_future_one_metre_curve_starts_collection(self) -> None:
        gate = TurnGate(TurnGateConfig(pose_smoothing_window_s=0.0))
        samples = _arc(math.radians(12.0))

        evidence = gate.evaluate(samples, 2.0)

        self.assertTrue(evidence.is_curve)
        self.assertFalse(evidence.is_spin)
        self.assertEqual(evidence.direction, "left")
        self.assertAlmostEqual(
            evidence.reference_curvature_rad_per_m or 0.0,
            math.radians(12.0),
            delta=math.radians(0.25),
        )
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))
        self.assertEqual(gate.active_start_timestamp_s, 2.0)
        self.assertEqual(gate.start_collection(samples, 2.2), (False, 2.2))

    def test_curve_threshold_is_inclusive(self) -> None:
        gate = TurnGate(TurnGateConfig(pose_smoothing_window_s=0.0))
        samples = _arc(math.radians(8.0))

        evidence = gate.evaluate(samples, 2.0)

        self.assertTrue(evidence.is_curve)

    def test_spin_uses_unwrapped_yaw_and_low_translation(self) -> None:
        samples = []
        for index in range(101):
            timestamp_s = index / 10.0
            unwrapped = math.radians(170.0) + math.radians(20.0) * timestamp_s
            wrapped = math.atan2(math.sin(unwrapped), math.cos(unwrapped))
            samples.append(PoseSample(timestamp_s, 0.01, -0.01, wrapped))
        gate = TurnGate()

        evidence = gate.evaluate(samples, 2.0)

        self.assertTrue(evidence.is_spin)
        self.assertFalse(evidence.is_curve)
        self.assertEqual(evidence.direction, "left")
        self.assertGreater(
            evidence.spin_yaw_change_rad or 0.0,
            math.radians(30.0),
        )

    def test_translation_prevents_curve_from_being_labeled_spin(self) -> None:
        gate = TurnGate(TurnGateConfig(pose_smoothing_window_s=0.0))
        evidence = gate.evaluate(_arc(math.radians(-12.0)), 2.0)

        self.assertTrue(evidence.is_curve)
        self.assertFalse(evidence.is_spin)
        self.assertEqual(evidence.direction, "right")

    def test_end_waits_for_sustained_normal_straight_recovery(self) -> None:
        samples = _arc(math.radians(12.0), duration_s=5.0)
        last = samples[-1]
        for index in range(1, 101):
            timestamp_s = last.timestamp_s + index / 10.0
            distance_m = 0.5 * index / 10.0
            samples.append(
                PoseSample(
                    timestamp_s,
                    last.x_m + distance_m * math.cos(last.yaw_rad),
                    last.y_m + distance_m * math.sin(last.yaw_rad),
                    last.yaw_rad,
                )
            )
        gate = TurnGate(TurnGateConfig(pose_smoothing_window_s=0.0))

        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))
        stop_result = (False, 0.0)
        for index in range(21, 101):
            timestamp_s = index / 10.0
            stop_result = gate.end_collection(samples, timestamp_s)
            if stop_result[0]:
                break

        self.assertTrue(stop_result[0])
        # The geometric arc ends at 5 s. Collection continues until the
        # trailing two-second window is classified as normal straight motion.
        self.assertGreater(stop_result[1], 5.0)
        self.assertIsNone(gate.active_start_timestamp_s)

    def test_insufficient_future_never_ends_collection(self) -> None:
        gate = TurnGate()
        samples = _straight(duration_s=3.0)

        evidence = gate.evaluate(samples, 2.0)

        self.assertFalse(evidence.sufficient_data)
        self.assertEqual(
            gate.start_collection(_arc(math.radians(12.0)), 2.0), (True, 2.0)
        )
        self.assertEqual(gate.end_collection(samples, 2.2), (False, 2.2))
        self.assertEqual(gate.active_start_timestamp_s, 2.0)

    def test_stationary_after_turn_does_not_count_as_straight_recovery(self) -> None:
        samples = _motion_profile(
            [
                (5.0, 0.5, math.radians(12.0)),
                (4.0, 0.0, 0.0),
                (8.0, 0.5, 0.0),
            ]
        )
        gate = TurnGate(TurnGateConfig(pose_smoothing_window_s=0.0))
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))

        for index in range(21, 91):
            timestamp_s = index / 10.0
            self.assertEqual(
                gate.end_collection(samples, timestamp_s), (False, timestamp_s)
            )

        stop_result = (False, 0.0)
        for index in range(91, 141):
            stop_result = gate.end_collection(samples, index / 10.0)
            if stop_result[0]:
                break
        self.assertTrue(stop_result[0])
        self.assertGreater(stop_result[1], 9.0)

    def test_renewed_turn_resets_pending_straight_recovery(self) -> None:
        samples = _motion_profile(
            [
                (5.0, 0.5, math.radians(12.0)),
                (2.0, 0.5, 0.0),
                (2.0, 0.5, math.radians(-12.0)),
                (8.0, 0.5, 0.0),
            ]
        )
        gate = TurnGate(TurnGateConfig(pose_smoothing_window_s=0.0))
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))

        saw_pending_recovery = False
        saw_reset = False
        stop_result = (False, 0.0)
        for index in range(21, 151):
            stop_result = gate.end_collection(samples, index / 10.0)
            pending = gate.straight_recovery_start_timestamp_s is not None
            if pending:
                saw_pending_recovery = True
            elif saw_pending_recovery:
                saw_reset = True
            if stop_result[0]:
                break

        self.assertTrue(saw_pending_recovery)
        self.assertTrue(saw_reset)
        self.assertTrue(stop_result[0])
        self.assertGreater(stop_result[1], 9.0)

    def test_sparse_stream_call_returns_historical_action_node(self) -> None:
        samples = _motion_profile(
            [
                (5.0, 0.5, math.radians(12.0)),
                (8.0, 0.5, 0.0),
            ]
        )
        gate = TurnGate(TurnGateConfig(pose_smoothing_window_s=0.0))
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))

        should_end, action_timestamp_s = gate.end_collection(samples, 10.0)

        self.assertTrue(should_end)
        self.assertIsInstance(action_timestamp_s, float)
        self.assertGreater(action_timestamp_s, 5.0)
        self.assertLess(action_timestamp_s, 10.0)

    def test_pose_jump_is_not_turn_evidence(self) -> None:
        samples = _straight(duration_s=4.0)
        samples.extend(
            PoseSample(4.1 + index / 10.0, 20.0 + index * 0.05, 10.0, math.pi / 2.0)
            for index in range(60)
        )
        gate = TurnGate()

        evidence = gate.evaluate(samples, 4.0)

        self.assertFalse(evidence.is_turn)
        self.assertFalse(evidence.sufficient_data)
        self.assertEqual(gate.end_collection(samples, 4.0), (False, 4.0))

    def test_explicit_degree_mapping_is_supported_but_ambiguous_yaw_is_not(
        self,
    ) -> None:
        samples = [
            {
                "ts": item.timestamp_s,
                "pose": {"x": item.x_m, "y": item.y_m, "yaw_deg": 0.0},
            }
            for item in _straight()
        ]
        gate = TurnGate()
        self.assertEqual(gate.start_collection(samples, 3.0), (False, 3.0))

        bad = list(samples)
        bad[0] = {"ts": 0.0, "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0}}
        with self.assertRaisesRegex(ValueError, "yaw_rad or yaw_deg"):
            gate.evaluate(bad, 3.0)


class StraightGateTest(unittest.TestCase):
    def test_confirmed_straight_can_start_and_stops_at_fixed_duration(self) -> None:
        gate = StraightGate(
            StraightGateConfig(
                start_probability=1.0,
                minimum_collection_duration_s=6.0,
                maximum_collection_duration_s=6.0,
            ),
            rng=random.Random(7),
        )
        samples = _straight(duration_s=20.0)

        self.assertEqual(gate.start_collection(samples, 3.0), (True, 3.0))
        self.assertIsNotNone(gate.active_collection)
        self.assertEqual(gate.active_collection.target_duration_s, 6.0)  # type: ignore[union-attr]
        self.assertEqual(gate.start_collection(samples, 4.0), (False, 4.0))
        self.assertEqual(gate.end_collection(samples, 8.999), (False, 8.999))
        # A late streaming check still returns the exact locked deadline.
        self.assertEqual(gate.end_collection(samples, 9.2), (True, 9.0))
        self.assertIsNone(gate.active_collection)
        self.assertEqual(gate.last_collection.target_duration_s, 6.0)  # type: ignore[union-attr]

    def test_default_probability_is_one_percent_per_eligible_opportunity(self) -> None:
        samples = _straight(duration_s=20.0)
        hit = StraightGate(rng=_StubRandom(probability_draw=0.009, duration_s=12.0))
        miss = StraightGate(rng=_StubRandom(probability_draw=0.010, duration_s=12.0))

        self.assertEqual(hit.start_collection(samples, 3.0), (True, 3.0))
        self.assertEqual(hit.active_collection.target_duration_s, 12.0)  # type: ignore[union-attr]
        self.assertEqual(miss.start_collection(samples, 3.0), (False, 3.0))
        self.assertIsNone(miss.active_collection)

    def test_stationary_and_turning_motion_are_not_straight_opportunities(self) -> None:
        static = [PoseSample(index / 10.0, 1.0, 2.0, 0.0) for index in range(201)]
        always_start = StraightGate(StraightGateConfig(start_probability=1.0))

        self.assertEqual(always_start.start_collection(static, 3.0), (False, 3.0))
        self.assertEqual(
            always_start.start_collection(_arc(math.radians(12.0), 20.0), 3.0),
            (False, 3.0),
        )

    def test_duration_is_uniformly_bounded_between_six_and_twelve_seconds(
        self,
    ) -> None:
        samples = _straight(duration_s=30.0)
        durations = []
        gate = StraightGate(
            StraightGateConfig(start_probability=1.0), rng=random.Random(20260908)
        )
        for index in range(50):
            start_s = 3.0 + index * 0.01
            self.assertEqual(gate.start_collection(samples, start_s), (True, start_s))
            collection = gate.active_collection
            self.assertIsNotNone(collection)
            durations.append(collection.target_duration_s)  # type: ignore[union-attr]
            self.assertEqual(
                gate.end_collection(samples, collection.end_timestamp_s),  # type: ignore[union-attr]
                (True, collection.end_timestamp_s),  # type: ignore[union-attr]
            )

        self.assertGreaterEqual(min(durations), 6.0)
        self.assertLessEqual(max(durations), 12.0)
        self.assertGreater(max(durations) - min(durations), 2.5)


class _StubRandom:
    def __init__(self, *, probability_draw: float, duration_s: float) -> None:
        self.probability_draw = probability_draw
        self.duration_s = duration_s

    def random(self) -> float:
        return self.probability_draw

    def uniform(self, _minimum: float, _maximum: float) -> float:
        return self.duration_s


if __name__ == "__main__":
    unittest.main()
