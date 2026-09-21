from __future__ import annotations

import math
from dataclasses import replace
import random
import unittest

from data_collection_gate import (
    CompletedCollection,
    PoseSample,
    StraightGate,
    StraightGateConfig,
    TurnGate,
    TurnGateConfig,
)


def _gate_without_extra_context(config=None) -> TurnGate:
    """Retain the old timing contract in legacy tests; default context is tested below."""
    return TurnGate(replace(config or TurnGateConfig(), pre_context_s=0.0, post_context_s=0.0))


def _straight(duration_s: float = 12.0, speed_mps: float = 0.5) -> list[PoseSample]:
    return [
        PoseSample(index / 10.0, speed_mps * index / 10.0, 0.0, 0.0)
        for index in range(int(duration_s * 10) + 1)
    ]


def _arc(
    curvature_rad_per_m: float,
    duration_s: float = 12.0,
    *,
    speed_mps: float = 0.5,
    sample_rate_hz: float = 10.0,
) -> list[PoseSample]:
    samples = []
    for index in range(int(duration_s * sample_rate_hz) + 1):
        timestamp_s = index / sample_rate_hz
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
        gate = _gate_without_extra_context()
        samples = _straight()

        evidence = gate.evaluate(samples, 3.0)

        self.assertTrue(evidence.sufficient_data)
        self.assertFalse(evidence.is_turn)
        self.assertEqual(gate.start_collection(samples, 3.0), (False, 3.0))
        self.assertEqual(gate.end_collection(samples, 5.0), (False, 5.0))

    def test_future_one_metre_curve_starts_collection(self) -> None:
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
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

    def test_total_turn_threshold_is_inclusive(self) -> None:
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
        for degrees in (-8.1, -8.0, -7.9, 7.9, 8.0, 8.1):
            with self.subTest(degrees=degrees):
                evidence = gate.evaluate(_arc(math.radians(degrees), duration_s=4.0), 2.0)
                self.assertEqual(evidence.is_curve, abs(degrees) >= 8.0)

    def test_spin_uses_unwrapped_yaw_and_low_translation(self) -> None:
        samples = []
        for index in range(101):
            timestamp_s = index / 10.0
            unwrapped = math.radians(170.0) + math.radians(20.0) * timestamp_s
            wrapped = math.atan2(math.sin(unwrapped), math.cos(unwrapped))
            samples.append(PoseSample(timestamp_s, 0.01, -0.01, wrapped))
        gate = _gate_without_extra_context()

        evidence = gate.evaluate(samples, 2.0)

        self.assertTrue(evidence.is_spin)
        self.assertFalse(evidence.is_curve)
        self.assertEqual(evidence.direction, "left")
        self.assertGreater(
            evidence.spin_yaw_change_rad or 0.0,
            math.radians(30.0),
        )
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))
        # Crossing +pi/-pi is normal wrapped-yaw motion, not a pose jump.
        self.assertEqual(gate.end_collection(samples, 4.0), (False, 4.0))

    def test_translation_prevents_curve_from_being_labeled_spin(self) -> None:
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
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
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))

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
        gate = _gate_without_extra_context()
        samples = _straight(duration_s=3.0)

        evidence = gate.evaluate(samples, 2.0)

        self.assertFalse(evidence.sufficient_data)
        self.assertEqual(
            gate.start_collection(_arc(math.radians(12.0)), 2.0), (True, 2.0)
        )
        self.assertEqual(gate.end_collection(samples, 2.2), (False, 2.2))
        self.assertEqual(gate.active_start_timestamp_s, 2.0)

    def test_stable_stop_after_turn_ends_collection(self) -> None:
        samples = _motion_profile(
            [
                (5.0, 0.5, math.radians(12.0)),
                (4.0, 0.0, 0.0),
                (8.0, 0.5, 0.0),
            ]
        )
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))

        stop_result = (False, 0.0)
        for index in range(21, 91):
            timestamp_s = index / 10.0
            stop_result = gate.end_collection(samples, timestamp_s)
            if stop_result[0]:
                break

        self.assertTrue(stop_result[0])
        self.assertAlmostEqual(stop_result[1], 8.0)

    def test_short_stationary_pause_does_not_end_collection(self) -> None:
        samples = _motion_profile(
            [
                (5.0, 0.5, math.radians(12.0)),
                (2.0, 0.0, 0.0),
                (4.0, 0.5, math.radians(12.0)),
            ]
        )
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))

        for index in range(21, 81):
            timestamp_s = index / 10.0
            self.assertEqual(
                gate.end_collection(samples, timestamp_s), (False, timestamp_s)
            )
        self.assertEqual(gate.active_start_timestamp_s, 2.0)

    def test_turn_collection_has_exact_default_maximum_interval(self) -> None:
        initial_cache = _arc(math.radians(12.0), duration_s=12.0)
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
        self.assertTrue(hasattr(TurnGate.end_collection, "__wrapped__"))
        self.assertEqual(gate.start_collection(initial_cache, 2.0), (True, 2.0))

        # The late call contains no history around the deadline.  Tmax still
        # returns the exact start + 45 s action node.
        recent_cache = [PoseSample(60.0, 0.0, 0.0, 0.0)]
        self.assertEqual(gate.end_collection(recent_cache, 60.0), (True, 47.0))
        self.assertIsNone(gate.active_start_timestamp_s)

    def test_pose_jump_is_a_hard_stop_for_turn_collection(self) -> None:
        samples = _arc(math.radians(12.0), duration_s=5.0)
        last = samples[-1]
        samples.extend(
            PoseSample(6.0 + index / 10.0, 20.0 + index * 0.05, 10.0, last.yaw_rad)
            for index in range(30)
        )
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))

        self.assertEqual(gate.end_collection(samples, 7.0), (True, 5.0))

    def test_renewed_turn_resets_pending_straight_recovery(self) -> None:
        samples = _motion_profile(
            [
                (5.0, 0.5, math.radians(12.0)),
                (2.0, 0.5, 0.0),
                (2.0, 0.5, math.radians(-12.0)),
                (8.0, 0.5, 0.0),
            ]
        )
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
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
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
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
        gate = _gate_without_extra_context()

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
        gate = _gate_without_extra_context()
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
        self.assertEqual(gate.config.maximum_collection_interval_s, 45.0)
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

    def test_maximum_interval_caps_custom_straight_duration(self) -> None:
        samples = _straight(duration_s=60.0)
        self.assertTrue(hasattr(StraightGate.end_collection, "__wrapped__"))
        gate = StraightGate(
            StraightGateConfig(
                start_probability=1.0,
                minimum_collection_duration_s=6.0,
                maximum_collection_duration_s=60.0,
                maximum_collection_interval_s=45.0,
            ),
            rng=_StubRandom(probability_draw=0.0, duration_s=60.0),
        )

        self.assertEqual(gate.start_collection(samples, 3.0), (True, 3.0))
        self.assertEqual(gate.active_collection.target_duration_s, 45.0)  # type: ignore[union-attr]
        self.assertEqual(gate.end_collection(samples, 60.0), (True, 48.0))

    def test_stable_stop_is_a_hard_stop_for_straight_collection(self) -> None:
        samples = _motion_profile([(5.0, 0.5, 0.0), (8.0, 0.0, 0.0)])
        samples = [
            PoseSample(
                sample.timestamp_s,
                sample.x_m
                + (
                    0.01 * math.sin(sample.timestamp_s * 7.0)
                    if sample.timestamp_s > 5.0
                    else 0.0
                ),
                sample.y_m,
                sample.yaw_rad
                + (
                    math.radians(0.5) * math.sin(sample.timestamp_s * 5.0)
                    if sample.timestamp_s > 5.0
                    else 0.0
                ),
            )
            for sample in samples
        ]
        gate = StraightGate(
            StraightGateConfig(
                start_probability=1.0,
                minimum_collection_duration_s=12.0,
                maximum_collection_duration_s=12.0,
            ),
            rng=random.Random(7),
        )
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))

        self.assertEqual(gate.end_collection(samples, 10.0), (True, 8.0))
        self.assertEqual(gate.last_collection.end_timestamp_s, 8.0)  # type: ignore[union-attr]
        self.assertEqual(gate.last_collection.target_duration_s, 6.0)  # type: ignore[union-attr]

    def test_pose_jump_is_a_hard_stop_for_straight_collection(self) -> None:
        samples = _straight(duration_s=5.0)
        samples.extend(
            PoseSample(5.1 + index / 10.0, 20.0 + index * 0.05, 10.0, 0.0)
            for index in range(30)
        )
        gate = StraightGate(
            StraightGateConfig(
                start_probability=1.0,
                minimum_collection_duration_s=12.0,
                maximum_collection_duration_s=12.0,
            ),
            rng=random.Random(7),
        )
        self.assertEqual(gate.start_collection(samples, 2.0), (True, 2.0))

        self.assertEqual(gate.end_collection(samples, 7.0), (True, 5.0))
        self.assertEqual(gate.last_collection.end_timestamp_s, 5.0)  # type: ignore[union-attr]


class GateTimingTest(unittest.TestCase):
    def test_straight_requires_low_curvature_even_when_turn_motion_is_rejected(self) -> None:
        samples = _arc(math.radians(8.1), speed_mps=0.18)
        for threshold in (8.0, 9.0):
            with self.subTest(threshold=threshold):
                detector = _gate_without_extra_context(TurnGateConfig(
                    turn_minimum_yaw_change_rad=math.radians(90.0),
                    curvature_threshold_rad_per_m=math.radians(threshold),
                ))
                self.assertFalse(detector.evaluate(samples, 2.0).is_turn)
                straight = StraightGate(
                    StraightGateConfig(start_probability=1.0), turn_gate=detector,
                )
                self.assertFalse(straight.start_collection(samples, 2.0)[0])  # Uncertain turn is not confirmed straight.

        # At the default 0.10 m motion boundary, a jitter loop is not straight.
        jitter = [
            PoseSample(index / 10.0, 0.05 * math.cos(index * math.pi / 10.0),
                       0.05 * math.sin(index * math.pi / 10.0), 0.0)
            for index in range(121)
        ]
        straight = StraightGate(
            StraightGateConfig(start_probability=1.0),
            turn_gate=_gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0)),
        )
        self.assertFalse(straight.turn_gate.evaluate(jitter, 2.0).is_turn)
        self.assertEqual(straight.start_collection(jitter, 2.0), (False, 2.0))

    def test_curve_classification_is_stable_with_sparse_poses_and_probe_phase(self) -> None:
        gate = _gate_without_extra_context()
        for frequency, speed in ((4.0, 0.39), (10.0, 0.21), (4.0, 1.2)):
            for degrees in (-12.0, -8.1, -7.9, -5.0, 5.0, 7.9, 8.1, 12.0):
                samples = _arc(math.radians(degrees), speed_mps=speed, sample_rate_hz=frequency)
                for timestamp in (2.0, 2.2, 2.4, 2.6, 2.8, 3.0):
                    with self.subTest(frequency=frequency, speed=speed, degrees=degrees, timestamp=timestamp):
                        evidence = gate.evaluate(samples, timestamp)
                        self.assertTrue(evidence.is_curve)
                gate.reset()
                self.assertTrue(gate.start_collection(samples, 2.0)[0])
                gate.reset()

    def test_parked_anchor_does_not_borrow_a_distant_turn(self) -> None:
        samples = _motion_profile([(6.0, 0.0, 0.0), (6.0, 0.5, 0.6)])
        for horizon in (2.0, 15.0):
            with self.subTest(horizon=horizon):
                gate = _gate_without_extra_context(TurnGateConfig(curve_maximum_lookahead_s=horizon))
                self.assertEqual(gate.start_collection(samples, 2.0), (False, 2.0))
                self.assertFalse(gate.evaluate(samples, 2.0).is_curve)

    def test_curve_window_is_bounded_in_time_and_retains_low_speed_turns(self) -> None:
        samples = _motion_profile([(12.0, 0.2, math.radians(12.0))])
        gate = _gate_without_extra_context(TurnGateConfig(curve_maximum_lookahead_s=2.0))
        # Data beyond the observation window cannot extend its route length.
        evidence = gate.evaluate(samples, 2.0)
        # A 4.8-degree unfinished prefix is uncertain, not a confirmed turn.
        self.assertFalse(evidence.is_curve)
        # Median smoothing and the 2 cm noise filter may shorten the path.
        self.assertGreaterEqual(evidence.future_path_m, 0.3)
        self.assertLessEqual(evidence.future_path_m, 0.401)
        self.assertIsNone(evidence.observation_end_timestamp_s)
        self.assertEqual(gate.start_collection(samples, 2.0), (False, 2.0))
        # The same low-speed arc is confirmed when actual angular support arrives.
        self.assertTrue(_gate_without_extra_context().evaluate(samples, 2.0).is_curve)

    def test_small_xy_loops_without_yaw_are_not_turns(self) -> None:
        samples = [
            PoseSample(index / 20.0, 0.04 * math.cos(index * 0.3),
                       0.04 * math.sin(index * 0.3), 0.0)
            for index in range(241)
        ]
        gate = _gate_without_extra_context(TurnGateConfig(pose_smoothing_window_s=0.0))
        self.assertFalse(gate.evaluate(samples, 2.0).is_turn)
        self.assertEqual(gate.start_collection(samples, 2.0), (False, 2.0))

    def test_streaming_clips_include_the_turn_that_triggered_them(self) -> None:
        for approach_s, speed in ((6.0, 0.0), (4.5, 0.2)):
            with self.subTest(approach_s=approach_s, speed=speed):
                samples = _motion_profile([
                    (approach_s, speed, 0.0), (6.0, 0.5, 0.6), (10.0, 0.5, 0.0),
                ])
                gate = _gate_without_extra_context()
                completed = []
                for index in range(int((samples[-1].timestamp_s - 6.0) / 0.2) + 1):
                    timestamp = index * 0.2
                    cache = [p for p in samples if timestamp - 54.0 <= p.timestamp_s <= timestamp + 6.0 + 1e-8]
                    if gate.active_start_timestamp_s is None:
                        gate.start_collection(cache, timestamp)
                    elif gate.end_collection(cache, timestamp)[0]:
                        completed.append(gate.last_completed_collection)
                self.assertEqual(len(completed), 1)
                clip = completed[0]
                self.assertTrue(clip.should_save)
                if speed == 0.0:
                    self.assertGreaterEqual(clip.start_timestamp_s, approach_s - 3.0)
                self.assertLess(clip.start_timestamp_s, approach_s + 1.0)
                self.assertGreater(clip.end_timestamp_s, approach_s + 6.0)

    def test_natural_recovery_waits_for_the_start_observation(self) -> None:
        samples = _motion_profile([(4.5, 0.2, 0.0), (8.0, 0.5, 0.6)])
        gate = _gate_without_extra_context()
        self.assertEqual(gate.start_collection(samples, 3.0), (True, 3.0))
        # Previously the pre-turn straight history ended the clip at 4.2 s.
        self.assertEqual(gate.end_collection(samples, 4.2), (False, 4.2))
        self.assertEqual(gate.end_collection(samples, 6.0), (False, 6.0))

    def test_slow_turns_accumulate_distance_without_a_two_second_speed_cutoff(self) -> None:
        for speed, degrees in ((0.06, 30.0), (0.10, 30.0), (0.14, 30.0), (0.08, 170.0)):
            with self.subTest(speed=speed, degrees=degrees):
                samples = _arc(math.radians(degrees), 20.0, speed_mps=speed)
                cache = [p for p in samples if p.timestamp_s <= 8.0]
                gate = _gate_without_extra_context()
                self.assertTrue(gate.evaluate(cache, 2.0).is_curve)
                self.assertEqual(gate.start_collection(cache, 2.0), (True, 2.0))

    def test_sparse_pose_phase_does_not_prevent_slow_curve_start(self) -> None:
        samples = _arc(math.radians(30.0), 20.0, speed_mps=0.2, sample_rate_hz=2.0)
        for timestamp in (2.0, 2.01, 2.11, 2.31, 2.49):
            with self.subTest(timestamp=timestamp):
                gate = _gate_without_extra_context()
                cache = [p for p in samples if p.timestamp_s <= timestamp + 6.0]
                self.assertEqual(gate.start_collection(cache, timestamp), (True, timestamp))

    def test_stop_go_turn_and_finite_slow_turn_are_retained(self) -> None:
        profiles = [
            [(0.5, 0.4, math.radians(30.0)), (1.5, 0.0, 0.0)] * 10,
            [(3.0, 0.0, 0.0), (4.0, 0.1, math.radians(30.0))],
        ]
        for profile, turn_start, turn_end in zip(profiles, (0.0, 3.0), (18.5, 7.0)):
            with self.subTest(profile=profile):
                samples = _motion_profile(profile + [(12.0, 0.0, 0.0)])
                gate = _gate_without_extra_context()
                completed = []
                for index in range(int((samples[-1].timestamp_s - 6.0) / 0.2) + 1):
                    timestamp = index * 0.2
                    cache = [p for p in samples if p.timestamp_s <= timestamp + 6.0 + 1e-8]
                    if gate.active_start_timestamp_s is None:
                        gate.start_collection(cache, timestamp)
                    elif gate.end_collection(cache, timestamp)[0]:
                        clip = gate.last_completed_collection
                        if clip.should_save:
                            completed.append(clip)
                self.assertEqual(len(completed), 1)
                self.assertLessEqual(completed[0].start_timestamp_s, turn_start)
                self.assertGreaterEqual(completed[0].end_timestamp_s, turn_end)

    def test_curve_observation_excludes_the_stationary_cache_tail(self) -> None:
        gate = _gate_without_extra_context()
        moving = _motion_profile([(4.0, 0.5, math.radians(30.0)), (12.0, 0.0, 0.0)])
        evidence = gate.evaluate(moving, 1.0)
        self.assertTrue(evidence.is_curve)
        self.assertLess(evidence.observation_end_timestamp_s, 4.6)
        stopped = _motion_profile([(1.0, 0.5, math.radians(30.0)), (12.0, 0.0, 0.0)])
        evidence = gate.evaluate(stopped, 0.0)
        self.assertTrue(evidence.is_curve)
        self.assertLessEqual(evidence.observation_end_timestamp_s, 1.6)

    def test_finite_sparse_turn_retains_interpolated_anchor_distance(self) -> None:
        for duration, speed in ((2.0, 0.19), (2.0, 0.20), (3.0, 0.115), (3.0, 0.12)):
            with self.subTest(duration=duration, speed=speed):
                samples = _arc(math.radians(30.0), duration, speed_mps=speed, sample_rate_hz=2.0)
                last = samples[-1]
                samples.extend(PoseSample(duration + i * 0.5, last.x_m, last.y_m, last.yaw_rad)
                               for i in range(1, 25))
                gate = _gate_without_extra_context()
                cache = [p for p in samples if p.timestamp_s <= 6.01]
                self.assertEqual(gate.start_collection(cache, 0.01), (True, 0.01))
                self.assertTrue(gate.end_collection(samples, duration + 4.0)[0])
                clip = gate.last_completed_collection
                self.assertTrue(clip.should_save)
                self.assertGreaterEqual(clip.end_timestamp_s, duration)

    def test_hard_stop_keeps_only_turns_present_in_the_final_window(self) -> None:
        for first_speed, first_curvature in ((0.04, 0.0), (0.08, 0.6), (0.10, 0.6), (0.2, 0.6)):
            with self.subTest(first_speed=first_speed):
                samples = _motion_profile([
                    (2.0, first_speed, first_curvature), (3.3, 0.0, 0.0),
                    (4.0, 0.5, 0.6), (12.0, 0.0, 0.0),
                ])
                gate = _gate_without_extra_context()
                cache = [p for p in samples if p.timestamp_s <= 6.2 + 1e-8]
                started = gate.start_collection(cache, 0.2)[0]
                if first_curvature == 0.0:
                    self.assertFalse(started)  # Do not borrow a turn beyond a long stop.
                    continue
                self.assertTrue(started)
                self.assertTrue(gate.end_collection(samples, 5.2)[0])
                clip = gate.last_completed_collection
                self.assertGreaterEqual(clip.duration_s, 4.0)
                self.assertLess(clip.end_timestamp_s, 5.3)
                self.assertEqual(clip.target_confirmed, first_curvature != 0.0)
                self.assertEqual(clip.should_save, first_curvature != 0.0)

    def test_future_time_boundary_tolerates_float_roundoff(self) -> None:
        samples = _motion_profile([(2.0, 0.10, 0.6), (2.8, 0.0, 0.0)])
        self.assertLess(samples[-1].timestamp_s, 4.8)
        evidence = _gate_without_extra_context().evaluate(samples, 2.8000000000000003)
        self.assertTrue(evidence.sufficient_data)
        self.assertFalse(evidence.is_turn)

    def test_hard_stop_retains_spin_when_started_by_a_future_curve(self) -> None:
        profile = _motion_profile([
            (2.0, 0.04, 0.0), (0.4, 0.0, 0.0), (3.3, 0.0, 0.0),
            (4.0, 1.0, 0.6), (12.0, 0.0, 0.0),
        ])
        samples = []
        for p in profile:
            spin = math.radians(20.0) * min(1.0, max(0.0, (p.timestamp_s - 2.0) / 0.4))
            dx = p.x_m - 0.08
            samples.append(PoseSample(
                p.timestamp_s, 0.08 + dx * math.cos(spin) - p.y_m * math.sin(spin),
                dx * math.sin(spin) + p.y_m * math.cos(spin), p.yaw_rad + spin,
            ))
        for delay in (6.0, 7.0):
            with self.subTest(delay=delay):
                gate = _gate_without_extra_context()
                completed = []
                for index in range(int((samples[-1].timestamp_s - delay) / 0.2) + 1):
                    timestamp = index * 0.2
                    cache = [p for p in samples if p.timestamp_s <= timestamp + delay + 1e-8]
                    if gate.active_start_timestamp_s is None:
                        gate.start_collection(cache, timestamp)
                    elif gate.end_collection(cache, timestamp)[0]:
                        completed.append(gate.last_completed_collection)
                self.assertGreaterEqual(len(completed), 2)
                self.assertTrue(completed[0].should_save)
                self.assertLessEqual(completed[0].start_timestamp_s, 2.0)
                self.assertGreaterEqual(completed[0].end_timestamp_s, 2.4)
                self.assertTrue(all(clip.target_confirmed for clip in completed))

    def test_save_threshold_uses_exact_action_times(self) -> None:
        for start in (2.0, 1789639379.0706234):
            for duration in (0.0, 1.806082, 2.07845, 3.0, 3.999, 4.0, 4.001):
                with self.subTest(start=start, duration=duration):
                    clip = CompletedCollection(start, start + duration)
                    self.assertEqual(clip.should_save, duration >= 4.0)

    def test_short_hard_stop_ends_both_gates_and_publishes_discard(self) -> None:
        for kind in ('turn', 'straight'):
            for duration in (3.999, 4.0):
                with self.subTest(kind=kind, duration=duration):
                    start = 2.0
                    end = start + duration
                    samples = _arc(math.radians(12.0)) if kind == 'turn' else _straight()
                    samples = [p for p in samples if p.timestamp_s < end]
                    last = samples[-1]
                    samples.append(PoseSample(end, last.x_m, last.y_m, last.yaw_rad))
                    # Absolute jump beyond a gap still ends at the pre-jump pose.
                    samples.append(PoseSample(end + 1.0, 20.0, 20.0, last.yaw_rad))
                    gate = _gate_without_extra_context() if kind == 'turn' else StraightGate(
                        StraightGateConfig(start_probability=1.0),
                    )
                    self.assertIsNone(gate.last_completed_collection)
                    self.assertEqual(gate.start_collection(samples, start), (True, start))
                    self.assertEqual(gate.end_collection(samples, end + 1.0), (True, end))
                    clip = gate.last_completed_collection
                    self.assertEqual(clip, CompletedCollection(start, end))
                    self.assertEqual(clip.should_save, duration >= 4.0)
                    self.assertIsNone(gate.active_start_timestamp_s)
                    self.assertEqual(gate.end_collection(samples, end + 1.2), (False, end + 1.2))
                    self.assertEqual(gate.last_completed_collection, clip)
                    gate.reset()
                    self.assertEqual(gate.last_completed_collection, clip)

    def test_natural_and_timeout_ends_publish_final_window(self) -> None:
        samples = _straight(duration_s=20.0)
        straight = StraightGate(StraightGateConfig(
            start_probability=1.0, minimum_collection_duration_s=6.0,
            maximum_collection_duration_s=6.0,
        ))
        self.assertEqual(straight.start_collection(samples, 2.0), (True, 2.0))
        self.assertEqual(straight.end_collection(samples, 10.0), (True, 8.0))
        self.assertEqual(straight.last_completed_collection, CompletedCollection(2.0, 8.0))
        turn = _gate_without_extra_context()
        self.assertTrue(turn.start_collection(_arc(math.radians(12.0)), 2.0)[0])
        self.assertEqual(turn.end_collection([], 60.0), (True, 47.0))
        self.assertEqual(turn.last_completed_collection, CompletedCollection(2.0, 47.0))

    def test_short_stable_stop_is_discarded_even_before_observation_end(self) -> None:
        for kind in ('turn', 'straight'):
            with self.subTest(kind=kind):
                curvature = 0.6 if kind == 'turn' else 0.0
                samples = _motion_profile([
                    (1.0, 0.5, curvature), (5.0, 0.0, 0.0), (3.0, 0.5, curvature),
                ])
                detector = _gate_without_extra_context(TurnGateConfig(curve_maximum_lookahead_s=15.0))
                gate = detector if kind == 'turn' else StraightGate(
                    StraightGateConfig(start_probability=1.0), turn_gate=detector,
                )
                self.assertTrue(gate.start_collection(samples, 0.2)[0])
                should_end, end = gate.end_collection(samples, 4.5)
                self.assertTrue(should_end)
                self.assertAlmostEqual(end, 4.0)
                self.assertFalse(gate.last_completed_collection.should_save)
                self.assertIsNone(gate.active_start_timestamp_s)

    def test_turn_time_limits_must_be_finite(self) -> None:
        for value in (math.inf, math.nan, 0.0, -1.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    TurnGateConfig(curve_maximum_lookahead_s=value)

class TurnGateRevisionTest(unittest.TestCase):
    def collect(self, samples, *, phase=0.0, config=None):
        gate = TurnGate(config)
        completed = []
        last = samples[-1].timestamp_s
        count = int(last / 0.2) + 1
        for timestamp in [phase + i * 0.2 for i in range(count) if phase + i * 0.2 <= last] + [last]:
            cache = [p for p in samples if timestamp - 54.0 <= p.timestamp_s <= timestamp + 6.0 + 1e-8]
            if len(cache) < 2:
                continue
            if gate.active_start_timestamp_s is None:
                gate.start_collection(cache, timestamp)
            elif gate.end_collection(cache, timestamp)[0]:
                completed.append(gate.last_completed_collection)
        if gate.finish_collection(samples, last)[0]:
            completed.append(gate.last_completed_collection)
        return [clip for clip in completed if clip.should_save]

    def test_real_near_straight_examples_never_start(self):
        import json
        from pathlib import Path
        cases = json.loads((Path(__file__).parent / 'tests/fixtures/turn_gate_near_straight.json').read_text())
        for row in cases:
            with self.subTest(source=row['source']):
                samples = [PoseSample(*p) for p in row['poses']]
                self.assertEqual(self.collect(samples), [])

    def test_default_context_includes_approach_and_departure(self):
        samples = _motion_profile([(8, .5, 0), (5, .5, .6), (10, .5, 0)])
        clips = self.collect(samples)
        self.assertEqual(len(clips), 1)
        self.assertLessEqual(clips[0].start_timestamp_s, 4.01)
        self.assertGreaterEqual(clips[0].end_timestamp_s, 17.0)
        self.assertLess(clips[0].end_timestamp_s, 20.0)

    def test_parked_approach_context_does_not_stop_before_core(self):
        samples = _motion_profile([(10, 0, 0), (3, .4, .6), (10, .5, 0)])
        clips = self.collect(samples)
        self.assertEqual(len(clips), 1)
        self.assertAlmostEqual(clips[0].start_timestamp_s, 6.0, delta=.2)
        self.assertGreaterEqual(clips[0].end_timestamp_s, 17.0)

    def test_weak_short_turn_needs_closure_and_keeps_historical_slow_arc(self):
        moving = _motion_profile([(2, .08, .6)])
        gate = TurnGate()
        self.assertFalse(gate.evaluate(moving, 0).is_turn)
        full = _motion_profile([(2, .08, .6), (5, 0, 0)])
        self.assertTrue(gate.evaluate(full, 0).is_turn)
        clips = self.collect(full)
        self.assertEqual(len(clips), 1)
        self.assertLessEqual(clips[0].start_timestamp_s, .01)
        self.assertGreaterEqual(clips[0].end_timestamp_s, 2.0)

    def test_brief_correction_cannot_use_stationary_tail_as_support(self):
        samples = _motion_profile([(8, .5, 0), (.5, .6, math.radians(6)/.3), (6, 0, 0)])
        self.assertEqual(self.collect(samples), [])

    def test_unfinished_shallow_drift_is_not_a_short_turn(self):
        samples = _motion_profile([(15, .15, math.radians(5)/2.25), (5, 0, 0)])
        self.assertEqual(self.collect(samples), [])

    def test_small_reversals_do_not_accumulate_absolute_yaw(self):
        samples = [PoseSample(i*.1, i*.05, .01*math.sin(i*.2), math.radians(1.5)*math.sin(i*.2)) for i in range(201)]
        self.assertEqual(self.collect(samples), [])

    def test_s_turn_with_zero_net_yaw_is_retained(self):
        samples = _motion_profile([(8,.5,0),(3,.5,.3),(3,.5,-.3),(10,.5,0)])
        clips = self.collect(samples)
        self.assertEqual(len(clips), 1)
        self.assertLessEqual(clips[0].start_timestamp_s, 8.0)
        self.assertGreaterEqual(clips[0].end_timestamp_s, 18.0)

    def test_slow_turn_uses_available_history_with_six_second_delay(self):
        samples = _motion_profile([(10,0,0),(20,.04,math.radians(30)),(10,.5,0)])
        clips = self.collect(samples)
        self.assertEqual(len(clips), 1)
        self.assertLessEqual(clips[0].start_timestamp_s, 10.0)
        self.assertGreaterEqual(clips[0].end_timestamp_s, 30.0)

    def test_eof_tail_is_drained_and_finish_is_idempotent(self):
        samples = _motion_profile([(8,.5,0),(2,.5,.6)])
        clips = self.collect(samples)
        self.assertEqual(len(clips), 1)
        self.assertAlmostEqual(clips[0].end_timestamp_s, 10.0)
        gate = TurnGate()
        self.assertTrue(gate.start_collection(samples, 7.0)[0])
        self.assertTrue(gate.finish_collection(samples, 10.0)[0])
        previous = gate.last_completed_collection
        self.assertFalse(gate.finish_collection(samples, 10.0)[0])
        self.assertEqual(gate.last_completed_collection, previous)

    def test_long_turn_continues_with_short_tail_at_tmax(self):
        # Core starts at 8, capture starts at 4, Tmax at 49. Only one second
        # of slow turning remains; a fresh eight-degree requirement would miss it.
        samples = _motion_profile([(8,.5,0),(42,.1,math.radians(30)),(10,.5,0)])
        clips = self.collect(samples)
        self.assertEqual(len(clips), 2)
        self.assertTrue(all(clip.duration_s <= 45.0 + 1e-8 for clip in clips))
        self.assertGreaterEqual(clips[0].end_timestamp_s, clips[1].start_timestamp_s)
        self.assertLessEqual(clips[0].start_timestamp_s, 8.0)
        self.assertGreaterEqual(clips[-1].end_timestamp_s, 50.0)
        self.assertLess(clips[1].start_timestamp_s, 49.0)

    def test_confirmation_does_not_use_poses_beyond_bounded_window(self):
        samples = _motion_profile([(15,.5,0),(5,.5,.6)])
        gate = TurnGate(TurnGateConfig(curve_maximum_lookahead_s=4.0))
        self.assertFalse(gate.evaluate(samples, 10).is_turn)
        # Altering an observation outside the evidence window cannot alter it.
        prefix = [p for p in samples if p.timestamp_s <= 14.0 + 1e-8]
        self.assertEqual(gate.evaluate(samples, 10), gate.evaluate(prefix, 10))

    def test_final_window_cannot_borrow_turn_after_external_truncation(self):
        samples = _motion_profile([(8,.5,0),(3,.5,.6),(10,.5,0)])
        gate = TurnGate()
        self.assertTrue(gate.start_collection(samples, 7.0)[0])
        gate.finish_collection(samples, 8.0)
        self.assertFalse(gate.last_completed_collection.target_confirmed)
        self.assertFalse(gate.last_completed_collection.should_save)

    def test_small_xy_wobble_during_departure_does_not_wait_for_tmax(self):
        samples = _motion_profile([(8,.5,0),(3,.5,.6),(45,.5,0)])
        heading = samples[110].yaw_rad
        noisy = []
        for p in samples:
            noise = .02 * math.sin(2*math.pi*.3*(p.timestamp_s-11)) if p.timestamp_s > 11 else 0.0
            noisy.append(PoseSample(p.timestamp_s,p.x_m-noise*math.sin(heading),p.y_m+noise*math.cos(heading),p.yaw_rad))
        clips = self.collect(noisy)
        self.assertEqual(len(clips),1)
        self.assertGreaterEqual(clips[0].end_timestamp_s,15.0)
        self.assertLess(clips[0].end_timestamp_s,18.0)

    def test_sustained_shallow_arc_is_not_mistaken_for_straight_recovery(self):
        for speed, degrees in ((.2,5.0),(.2,3.0)):
            with self.subTest(speed=speed,degrees=degrees):
                samples = _motion_profile([(8,.5,0),(32,speed,math.radians(degrees)),(15,.5,0)])
                clips = self.collect(samples)
                self.assertTrue(clips)
                self.assertLessEqual(clips[0].start_timestamp_s,8.0)
                self.assertGreaterEqual(clips[-1].end_timestamp_s,40.0)
                for a,b in zip(clips,clips[1:]):
                    self.assertGreaterEqual(a.end_timestamp_s,b.start_timestamp_s)

    def test_tmax_continuation_stops_at_pose_gap(self):
        samples = _motion_profile([(8,.5,0),(42,.1,math.radians(30)),(14,.5,0)])
        samples = [p for p in samples if not 49.5 < p.timestamp_s < 51.0]
        clips = self.collect(samples)
        self.assertTrue(clips)
        before = max(p.timestamp_s for p in samples if p.timestamp_s < 51.0)
        after = min(p.timestamp_s for p in samples if p.timestamp_s >= 51.0)
        self.assertTrue(all(c.end_timestamp_s <= before + 1e-8 or c.start_timestamp_s >= after for c in clips))

    def test_spin_filter_support_after_eof_cannot_confirm_saved_window(self):
        times = [i*.5 for i in range(13)] + [6+i*.1 for i in range(1,41)]
        samples = []
        for t in times:
            degrees = 0.0 if t <= 4 else 2*(t-4) if t <= 6 else 4+30*min(t-6,1.0)
            samples.append(PoseSample(t,0,0,math.radians(degrees)))
        gate = TurnGate()
        self.assertTrue(gate.start_collection(samples,4.0)[0])
        gate.finish_collection(samples,6.0)
        self.assertFalse(gate.last_completed_collection.target_confirmed)
        self.assertFalse(gate.last_completed_collection.should_save)

    def test_new_context_and_evidence_parameters_are_validated(self):
        for key in ('pre_context_s','post_context_s','weak_turn_minimum_support_s','turn_minimum_yaw_change_rad'):
            for value in (-1.0, math.inf, math.nan):
                with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                    TurnGateConfig(**{key:value})
        with self.assertRaises(ValueError):
            TurnGateConfig(pre_context_s=45.0)



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
