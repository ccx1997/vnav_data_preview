"""Collection gate for retaining turn and in-place rotation data.

The gate is intentionally independent from the recorder.  A recorder passes a
pose cache and a timestamp in that cache to :meth:`TurnGate.start_collection`
or :meth:`TurnGate.end_collection`; each method returns ``(decision, action
timestamp)``.  This lets the caller delay decisions, recover an exact node from
history, and use future observations that are still available in its rolling
cache.

All angles in this module are radians.  Ambiguous ``yaw`` mapping keys are
rejected; use ``yaw_rad`` or ``yaw_deg`` explicitly.
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence, Union


TURN_CURVATURE_THRESHOLD_RAD_PER_M = math.radians(8.0)
NORMAL_STRAIGHT_MINIMUM_TRANSLATION_M = 0.10
NORMAL_STRAIGHT_MAXIMUM_YAW_CHANGE_RAD = math.radians(3.0)

GateResult = tuple[bool, float]


@dataclass(frozen=True)
class PoseSample:
    """One world-frame pose observation used by :class:`TurnGate`."""

    timestamp_s: float
    x_m: float
    y_m: float
    yaw_rad: float


PoseSampleLike = Union[PoseSample, Sequence[float], Mapping[str, object]]


@dataclass(frozen=True)
class TurnGateConfig:
    """Thresholds matching the existing turn-oriented post-processing rules."""

    # Route turns: future-one-metre route curvature >= 8 deg/m.
    curve_lookahead_m: float = 1.0
    curve_minimum_path_m: float = 0.30
    curve_maximum_lookahead_s: float = 15.0
    curve_resample_m: float = 0.05
    curvature_threshold_rad_per_m: float = TURN_CURVATURE_THRESHOLD_RAD_PER_M

    # Observable replacement for the offline teacher's SPIN_LEFT/SPIN_RIGHT.
    spin_measurement_window_s: float = 2.0
    spin_maximum_translation_m: float = 0.10
    spin_minimum_yaw_change_rad: float = math.radians(8.0)

    # Context and streaming recovery after positive turn evidence.
    start_lookahead_s: float = 1.0
    straight_recovery_window_s: float = 2.0
    straight_recovery_persistence_s: float = 1.0
    straight_recovery_minimum_translation_m: float = (
        NORMAL_STRAIGHT_MINIMUM_TRANSLATION_M
    )
    straight_recovery_maximum_yaw_change_rad: float = (
        NORMAL_STRAIGHT_MAXIMUM_YAW_CHANGE_RAD
    )
    probe_interval_s: float = 0.20

    # Pose denoising and the same jump limits used by the training pipeline.
    pose_smoothing_window_s: float = 1.0
    minimum_route_step_m: float = 0.02
    maximum_pose_gap_s: float = 0.50
    maximum_pose_jump_m: float = 2.0
    maximum_linear_speed_mps: float = 3.0
    maximum_yaw_step_rad: float = math.radians(45.0)
    maximum_yaw_rate_rps: float = 2.0

    def __post_init__(self) -> None:
        positive = {
            "curve_lookahead_m": self.curve_lookahead_m,
            "curve_minimum_path_m": self.curve_minimum_path_m,
            "curve_maximum_lookahead_s": self.curve_maximum_lookahead_s,
            "curve_resample_m": self.curve_resample_m,
            "curvature_threshold_rad_per_m": self.curvature_threshold_rad_per_m,
            "spin_measurement_window_s": self.spin_measurement_window_s,
            "spin_maximum_translation_m": self.spin_maximum_translation_m,
            "spin_minimum_yaw_change_rad": self.spin_minimum_yaw_change_rad,
            "straight_recovery_window_s": self.straight_recovery_window_s,
            "straight_recovery_persistence_s": self.straight_recovery_persistence_s,
            "straight_recovery_minimum_translation_m": self.straight_recovery_minimum_translation_m,
            "straight_recovery_maximum_yaw_change_rad": self.straight_recovery_maximum_yaw_change_rad,
            "probe_interval_s": self.probe_interval_s,
            "maximum_pose_gap_s": self.maximum_pose_gap_s,
            "maximum_pose_jump_m": self.maximum_pose_jump_m,
            "maximum_linear_speed_mps": self.maximum_linear_speed_mps,
            "maximum_yaw_step_rad": self.maximum_yaw_step_rad,
            "maximum_yaw_rate_rps": self.maximum_yaw_rate_rps,
        }
        invalid = [name for name, value in positive.items() if value <= 0.0]
        if invalid:
            raise ValueError(
                f"configuration values must be positive: {', '.join(invalid)}"
            )
        nonnegative = {
            "start_lookahead_s": self.start_lookahead_s,
            "pose_smoothing_window_s": self.pose_smoothing_window_s,
            "minimum_route_step_m": self.minimum_route_step_m,
        }
        invalid = [name for name, value in nonnegative.items() if value < 0.0]
        if invalid:
            raise ValueError(
                f"configuration values must be non-negative: {', '.join(invalid)}"
            )
        if self.curve_minimum_path_m > self.curve_lookahead_m:
            raise ValueError("curve_minimum_path_m cannot exceed curve_lookahead_m")


@dataclass(frozen=True)
class TurnEvidence:
    """Diagnostic result for one timestamp.

    ``sufficient_data`` is deliberately separate from ``is_turn``.  A caller
    must not interpret insufficient data as confirmed straight motion.
    """

    timestamp_s: float
    sufficient_data: bool
    is_turn: bool
    is_curve: bool
    is_spin: bool
    direction: str
    reference_curvature_rad_per_m: Optional[float]
    future_path_m: float
    spin_translation_m: Optional[float]
    spin_yaw_change_rad: Optional[float]
    reason: str


@dataclass(frozen=True)
class StraightGateConfig:
    """Sampling and motion thresholds for normal straight driving."""

    start_probability: float = 0.01
    minimum_collection_duration_s: float = 6.0
    maximum_collection_duration_s: float = 12.0
    minimum_translation_m: float = NORMAL_STRAIGHT_MINIMUM_TRANSLATION_M
    maximum_yaw_change_rad: float = NORMAL_STRAIGHT_MAXIMUM_YAW_CHANGE_RAD

    def __post_init__(self) -> None:
        if not 0.0 <= self.start_probability <= 1.0:
            raise ValueError("start_probability must be within [0, 1]")
        if self.minimum_collection_duration_s <= 0.0:
            raise ValueError("minimum_collection_duration_s must be positive")
        if self.maximum_collection_duration_s < self.minimum_collection_duration_s:
            raise ValueError(
                "maximum_collection_duration_s cannot be shorter than the minimum"
            )
        if self.minimum_translation_m <= 0.0:
            raise ValueError("minimum_translation_m must be positive")
        if self.maximum_yaw_change_rad <= 0.0:
            raise ValueError("maximum_yaw_change_rad must be positive")


@dataclass(frozen=True)
class StraightCollection:
    """The fixed time budget selected for one straight-driving collection."""

    start_timestamp_s: float
    target_duration_s: float
    end_timestamp_s: float


@dataclass(frozen=True)
class _PreparedPose:
    timestamp_s: float
    x_m: float
    y_m: float
    yaw_rad: float  # unwrapped


@dataclass(frozen=True)
class _PreparedCache:
    segments: tuple[tuple[_PreparedPose, ...], ...]
    breaks: tuple[tuple[float, float, str], ...]


class _RandomSource(Protocol):
    def random(self) -> float: ...

    def uniform(self, minimum: float, maximum: float) -> float: ...


class TurnGate:
    """Streaming state machine for turn-data collection.

    A successful start owns the active turn state.  Ending is allowed only
    after subsequent calls observe continuous normal straight driving for the
    configured recovery duration.
    """

    def __init__(self, config: Optional[TurnGateConfig] = None) -> None:
        self.config = config or TurnGateConfig()
        self._active_start_timestamp_s: Optional[float] = None
        self._last_end_check_timestamp_s: Optional[float] = None
        self._straight_recovery_start_timestamp_s: Optional[float] = None

    @property
    def active_start_timestamp_s(self) -> Optional[float]:
        """Return the active clip's start node, if this gate owns one."""

        return self._active_start_timestamp_s

    @property
    def straight_recovery_start_timestamp_s(self) -> Optional[float]:
        """Return when sustained trailing-straight evidence began."""

        return self._straight_recovery_start_timestamp_s

    def reset(self) -> None:
        """Clear streaming state after an external recorder reset or abort."""

        self._active_start_timestamp_s = None
        self._last_end_check_timestamp_s = None
        self._straight_recovery_start_timestamp_s = None

    def start_collection(
        self,
        pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> GateResult:
        """Return ``(should_start, action_timestamp_s)`` for the stream.

        The one-second default look-ahead keeps approach context before the
        first timestamp with positive turn evidence.
        """

        timestamp_s = _finite_timestamp(decision_timestamp_s)
        if self._active_start_timestamp_s is not None:
            return False, timestamp_s
        samples = self._prepare_cache(pose_cache)
        for probe_timestamp_s in self._probe_times(
            timestamp_s,
            timestamp_s + self.config.start_lookahead_s,
        ):
            if self._evaluate_prepared(samples, probe_timestamp_s).is_turn:
                self._active_start_timestamp_s = timestamp_s
                self._last_end_check_timestamp_s = probe_timestamp_s
                self._straight_recovery_start_timestamp_s = None
                return True, timestamp_s
        return False, timestamp_s

    def end_collection(
        self,
        pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> GateResult:
        """Return ``(should_end, action_timestamp_s)`` for the stream.

        A turn-free observation is not enough.  The trajectory immediately
        before the action node must satisfy the configured normal-straight
        recovery window.  Insufficient, stationary, discontinuous, or renewed
        turn evidence cannot end the clip.
        """

        timestamp_s = _finite_timestamp(decision_timestamp_s)
        active_start_s = self._active_start_timestamp_s
        if active_start_s is None:
            return False, timestamp_s
        if timestamp_s < active_start_s:
            raise ValueError("end timestamp cannot precede collection start")

        previous_check_s = self._last_end_check_timestamp_s
        if previous_check_s is not None and timestamp_s < previous_check_s:
            # Start look-ahead may have already examined a future node.  Calls
            # before that known turn evidence are harmless and need no replay.
            return False, timestamp_s
        if previous_check_s is not None and timestamp_s == previous_check_s:
            return False, timestamp_s

        samples = self._prepare_cache(pose_cache)
        for probe_timestamp_s in self._stream_probe_times(
            previous_check_s, timestamp_s
        ):
            if not self._is_trailing_normal_straight(
                samples,
                probe_timestamp_s,
                window_s=self.config.straight_recovery_window_s,
                minimum_translation_m=self.config.straight_recovery_minimum_translation_m,
                maximum_yaw_change_rad=self.config.straight_recovery_maximum_yaw_change_rad,
            ):
                self._straight_recovery_start_timestamp_s = None
                continue
            if self._straight_recovery_start_timestamp_s is None:
                self._straight_recovery_start_timestamp_s = probe_timestamp_s
            recovery_end_s = (
                self._straight_recovery_start_timestamp_s
                + self.config.straight_recovery_persistence_s
            )
            if probe_timestamp_s + 1.0e-12 < recovery_end_s:
                continue
            self.reset()
            return True, recovery_end_s

        self._last_end_check_timestamp_s = timestamp_s
        return False, timestamp_s

    def evaluate(
        self,
        pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> TurnEvidence:
        """Return turn diagnostics for one timestamp without changing state."""

        return self._evaluate_prepared(
            self._prepare_cache(pose_cache), float(decision_timestamp_s)
        )

    def _is_trailing_normal_straight(
        self,
        cache: _PreparedCache,
        timestamp_s: float,
        *,
        window_s: float,
        minimum_translation_m: float,
        maximum_yaw_change_rad: float,
    ) -> bool:
        segment, _failure = self._continuous_segment(cache, timestamp_s)
        if segment is None:
            return False
        start_timestamp_s = timestamp_s - window_s
        if start_timestamp_s < segment[0].timestamp_s - 1.0e-9:
            return False

        start = _interpolate_pose(segment, start_timestamp_s)
        stop = _interpolate_pose(segment, timestamp_s)
        window = [start]
        window.extend(
            pose
            for pose in segment
            if start_timestamp_s < pose.timestamp_s < timestamp_s
        )
        window.append(stop)
        translation_m = max(
            math.hypot(pose.x_m - start.x_m, pose.y_m - start.y_m) for pose in window
        )
        if translation_m + 1.0e-12 < minimum_translation_m:
            return False
        if abs(stop.yaw_rad - start.yaw_rad) > maximum_yaw_change_rad:
            return False

        route_points = _collapse_route_points(window, self.config.minimum_route_step_m)
        curvature, _path_m = _estimate_route_curvature(
            route_points,
            lookahead_m=self.config.curve_lookahead_m,
            minimum_path_m=self.config.curve_minimum_path_m,
            resample_m=self.config.curve_resample_m,
        )
        return bool(
            curvature is not None
            and abs(curvature) < self.config.curvature_threshold_rad_per_m
        )

    def _prepare_cache(self, pose_cache: Sequence[PoseSampleLike]) -> _PreparedCache:
        if len(pose_cache) < 2:
            raise ValueError("pose cache must contain at least two samples")
        raw = [_coerce_pose_sample(item) for item in pose_cache]
        timestamps = [item.timestamp_s for item in raw]
        if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
            raise ValueError("pose timestamps must be strictly increasing")

        unwrapped_yaw = _unwrap_angles([item.yaw_rad for item in raw])
        samples: list[_PreparedPose] = [
            _PreparedPose(item.timestamp_s, item.x_m, item.y_m, yaw)
            for item, yaw in zip(raw, unwrapped_yaw)
        ]
        segments: list[tuple[_PreparedPose, ...]] = []
        breaks: list[tuple[float, float, str]] = []
        current = [samples[0]]
        for first, second in zip(samples, samples[1:]):
            valid, reason = self._valid_pair(first, second)
            if valid:
                current.append(second)
                continue
            segments.append(
                tuple(_smooth_poses(current, self.config.pose_smoothing_window_s))
            )
            breaks.append((first.timestamp_s, second.timestamp_s, reason))
            current = [second]
        segments.append(
            tuple(_smooth_poses(current, self.config.pose_smoothing_window_s))
        )
        return _PreparedCache(tuple(segments), tuple(breaks))

    def _evaluate_prepared(
        self, cache: _PreparedCache, timestamp_s: float
    ) -> TurnEvidence:
        if not math.isfinite(timestamp_s):
            raise ValueError("decision_timestamp_s must be finite")

        segment, failure = self._continuous_segment(cache, timestamp_s)
        if segment is None:
            return _empty_evidence(timestamp_s, failure)
        if (
            timestamp_s < segment[0].timestamp_s
            or timestamp_s > segment[-1].timestamp_s
        ):
            return _empty_evidence(timestamp_s, "timestamp_outside_cache")

        anchor = _interpolate_pose(segment, timestamp_s)
        spin_stop_s = timestamp_s + self.config.spin_measurement_window_s
        if spin_stop_s > segment[-1].timestamp_s + 1.0e-9:
            return _empty_evidence(timestamp_s, "future_time_insufficient")

        spin_stop = _interpolate_pose(segment, spin_stop_s)
        spin_window = [anchor]
        spin_window.extend(
            pose for pose in segment if timestamp_s < pose.timestamp_s < spin_stop_s
        )
        spin_window.append(spin_stop)
        spin_translation = max(
            math.hypot(pose.x_m - anchor.x_m, pose.y_m - anchor.y_m)
            for pose in spin_window
        )
        spin_yaw_change = spin_stop.yaw_rad - anchor.yaw_rad
        is_spin = (
            spin_translation <= self.config.spin_maximum_translation_m
            and abs(spin_yaw_change) + 1.0e-12
            >= self.config.spin_minimum_yaw_change_rad
        )

        curve_stop_s = min(
            segment[-1].timestamp_s,
            timestamp_s + self.config.curve_maximum_lookahead_s,
        )
        route = [anchor]
        route.extend(
            pose for pose in segment if timestamp_s < pose.timestamp_s <= curve_stop_s
        )
        route_points = _collapse_route_points(route, self.config.minimum_route_step_m)
        curvature, future_path_m = _estimate_route_curvature(
            route_points,
            lookahead_m=self.config.curve_lookahead_m,
            minimum_path_m=self.config.curve_minimum_path_m,
            resample_m=self.config.curve_resample_m,
        )
        is_curve = curvature is not None and (
            abs(curvature) + 1.0e-12 >= self.config.curvature_threshold_rad_per_m
        )

        # A confirmed stationary two-second window cannot contain an arc turn,
        # even when there is not enough translation to estimate curvature.
        curve_sufficient = curvature is not None or (
            spin_translation <= self.config.spin_maximum_translation_m
        )
        sufficient = bool(is_spin or is_curve or curve_sufficient)
        is_turn = bool(is_spin or is_curve)
        direction = _turn_direction(curvature, spin_yaw_change, is_curve, is_spin)
        reason = (
            "turn"
            if is_turn
            else ("straight" if sufficient else "future_path_insufficient")
        )
        return TurnEvidence(
            timestamp_s=timestamp_s,
            sufficient_data=sufficient,
            is_turn=is_turn,
            is_curve=bool(is_curve),
            is_spin=bool(is_spin),
            direction=direction,
            reference_curvature_rad_per_m=curvature,
            future_path_m=future_path_m,
            spin_translation_m=spin_translation,
            spin_yaw_change_rad=spin_yaw_change,
            reason=reason,
        )

    def _continuous_segment(
        self, cache: _PreparedCache, timestamp_s: float
    ) -> tuple[Optional[tuple[_PreparedPose, ...]], str]:
        if (
            timestamp_s < cache.segments[0][0].timestamp_s
            or timestamp_s > cache.segments[-1][-1].timestamp_s
        ):
            return None, "timestamp_outside_cache"
        for segment in cache.segments:
            if segment[0].timestamp_s <= timestamp_s <= segment[-1].timestamp_s:
                return segment, "ok"
        for first_timestamp_s, second_timestamp_s, reason in cache.breaks:
            if first_timestamp_s < timestamp_s < second_timestamp_s:
                return None, reason
        return None, "timestamp_outside_cache"

    def _valid_pair(
        self, first: _PreparedPose, second: _PreparedPose
    ) -> tuple[bool, str]:
        dt_s = second.timestamp_s - first.timestamp_s
        if dt_s <= 0.0 or dt_s > self.config.maximum_pose_gap_s:
            return False, "pose_gap"
        distance_m = math.hypot(second.x_m - first.x_m, second.y_m - first.y_m)
        yaw_delta = abs(second.yaw_rad - first.yaw_rad)
        if (
            distance_m > self.config.maximum_pose_jump_m
            or distance_m / dt_s > self.config.maximum_linear_speed_mps
            or yaw_delta > self.config.maximum_yaw_step_rad
            or yaw_delta / dt_s > self.config.maximum_yaw_rate_rps
        ):
            return False, "pose_jump"
        return True, "ok"

    def _probe_times(self, start_s: float, stop_s: float) -> list[float]:
        if not math.isfinite(start_s) or not math.isfinite(stop_s):
            raise ValueError("decision timestamps must be finite")
        if stop_s < start_s:
            raise ValueError("probe stop must not precede probe start")
        count = int(math.floor((stop_s - start_s) / self.config.probe_interval_s))
        values = [
            start_s + index * self.config.probe_interval_s for index in range(count + 1)
        ]
        if not values or stop_s - values[-1] > 1.0e-9:
            values.append(stop_s)
        return values

    def _stream_probe_times(
        self, previous_timestamp_s: Optional[float], current_timestamp_s: float
    ) -> list[float]:
        if previous_timestamp_s is None:
            return [current_timestamp_s]
        first_probe_s = previous_timestamp_s + self.config.probe_interval_s
        if first_probe_s >= current_timestamp_s - 1.0e-9:
            return [current_timestamp_s]
        return self._probe_times(first_probe_s, current_timestamp_s)


class StraightGate:
    """Randomly retain bounded clips from confirmed normal straight driving.

    One successful start locks a random duration, and the same instance must
    receive the corresponding end checks.  The probability is applied once per
    eligible call made while the gate is inactive.
    """

    def __init__(
        self,
        config: Optional[StraightGateConfig] = None,
        *,
        turn_gate: Optional[TurnGate] = None,
        rng: Optional[_RandomSource] = None,
    ) -> None:
        self.config = config or StraightGateConfig()
        self.turn_gate = turn_gate or TurnGate()
        self._rng = rng or random.Random()
        self._active_collection: Optional[StraightCollection] = None
        self._last_collection: Optional[StraightCollection] = None
        self._last_evidence: Optional[TurnEvidence] = None

    @property
    def active_collection(self) -> Optional[StraightCollection]:
        """Return the active clip budget, if collection has started."""

        return self._active_collection

    @property
    def last_collection(self) -> Optional[StraightCollection]:
        """Return the most recently completed clip budget, if any."""

        return self._last_collection

    @property
    def last_evidence(self) -> Optional[TurnEvidence]:
        """Return motion evidence from the most recent start attempt."""

        return self._last_evidence

    def reset(self) -> None:
        """Clear the active clip after an external recorder reset or abort."""

        self._active_collection = None

    def start_collection(
        self,
        pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> GateResult:
        """Return ``(should_start, action_timestamp_s)`` for one opportunity.

        A call made while a clip is active returns a false decision and does
        not redraw either the probability or the target duration.
        """

        timestamp_s = _finite_timestamp(decision_timestamp_s)
        if self._active_collection is not None:
            return False, timestamp_s
        evidence = self.turn_gate.evaluate(pose_cache, timestamp_s)
        self._last_evidence = evidence
        if not _is_normal_straight_evidence(
            evidence,
            minimum_translation_m=self.config.minimum_translation_m,
            maximum_yaw_change_rad=self.config.maximum_yaw_change_rad,
        ):
            return False, timestamp_s

        probability = self.config.start_probability
        if probability <= 0.0:
            return False, timestamp_s
        if probability < 1.0 and self._rng.random() >= probability:
            return False, timestamp_s

        duration_s = self._rng.uniform(
            self.config.minimum_collection_duration_s,
            self.config.maximum_collection_duration_s,
        )
        self._active_collection = StraightCollection(
            start_timestamp_s=timestamp_s,
            target_duration_s=duration_s,
            end_timestamp_s=timestamp_s + duration_s,
        )
        return True, timestamp_s

    def end_collection(
        self,
        _pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> GateResult:
        """Return ``(should_end, action_timestamp_s)`` for the stream.

        Motion changes after the start do not shorten the selected 6--12 second
        clip.  A true decision completes and clears the active clip.  The pose
        cache is accepted to keep the same controller-facing signature as
        :class:`TurnGate`, but duration is deliberately the only stop input.
        """

        timestamp_s = _finite_timestamp(decision_timestamp_s)
        collection = self._active_collection
        if collection is None:
            return False, timestamp_s
        if timestamp_s < collection.start_timestamp_s:
            raise ValueError("end timestamp cannot precede collection start")
        if timestamp_s + 1.0e-12 < collection.end_timestamp_s:
            return False, timestamp_s
        self._last_collection = collection
        self._active_collection = None
        return True, collection.end_timestamp_s


def _finite_timestamp(value: float) -> float:
    timestamp_s = float(value)
    if not math.isfinite(timestamp_s):
        raise ValueError("decision_timestamp_s must be finite")
    return timestamp_s


def _is_normal_straight_evidence(
    evidence: TurnEvidence,
    *,
    minimum_translation_m: float,
    maximum_yaw_change_rad: float,
) -> bool:
    return bool(
        evidence.sufficient_data
        and not evidence.is_turn
        and evidence.reference_curvature_rad_per_m is not None
        and evidence.spin_translation_m is not None
        and evidence.spin_translation_m + 1.0e-12 >= minimum_translation_m
        and evidence.spin_yaw_change_rad is not None
        and abs(evidence.spin_yaw_change_rad) <= maximum_yaw_change_rad
    )


def _coerce_pose_sample(item: PoseSampleLike) -> PoseSample:
    if isinstance(item, PoseSample):
        result = item
    elif isinstance(item, Mapping):
        timestamp = _mapping_value(item, "timestamp_s", "timestamp", "ts")
        pose_value = item.get("pose", item)
        if not isinstance(pose_value, Mapping):
            raise ValueError("pose must be a mapping")
        x_m = _mapping_value(pose_value, "x_m", "x")
        y_m = _mapping_value(pose_value, "y_m", "y")
        if "yaw_rad" in pose_value:
            yaw_rad = pose_value["yaw_rad"]
        elif "yaw_deg" in pose_value:
            yaw_rad = math.radians(float(pose_value["yaw_deg"]))
        else:
            raise ValueError("pose mapping must contain explicit yaw_rad or yaw_deg")
        result = PoseSample(float(timestamp), float(x_m), float(y_m), float(yaw_rad))
    elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
        if len(item) != 4:
            raise ValueError("pose sequence must be (timestamp_s, x_m, y_m, yaw_rad)")
        result = PoseSample(*(float(value) for value in item))
    else:
        raise TypeError("unsupported pose sample type")
    if not all(
        math.isfinite(value)
        for value in (result.timestamp_s, result.x_m, result.y_m, result.yaw_rad)
    ):
        raise ValueError("pose samples must contain only finite values")
    return result


def _mapping_value(mapping: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in mapping:
            return mapping[key]
    raise ValueError(f"mapping must contain one of: {', '.join(keys)}")


def _unwrap_angles(values: Sequence[float]) -> list[float]:
    output = [float(values[0])]
    for value in values[1:]:
        delta = math.atan2(
            math.sin(float(value) - output[-1]), math.cos(float(value) - output[-1])
        )
        output.append(output[-1] + delta)
    return output


def _smooth_poses(
    samples: Sequence[_PreparedPose], window_s: float
) -> list[_PreparedPose]:
    if window_s <= 0.0:
        return list(samples)
    radius_s = 0.5 * window_s
    timestamps = [item.timestamp_s for item in samples]
    output: list[_PreparedPose] = []
    for index, sample in enumerate(samples):
        start = bisect.bisect_left(timestamps, sample.timestamp_s - radius_s)
        stop = bisect.bisect_right(timestamps, sample.timestamp_s + radius_s)
        window = samples[start:stop]
        output.append(
            _PreparedPose(
                timestamp_s=sample.timestamp_s,
                x_m=float(statistics.median(item.x_m for item in window)),
                y_m=float(statistics.median(item.y_m for item in window)),
                yaw_rad=float(statistics.median(item.yaw_rad for item in window)),
            )
        )
    return output


def _interpolate_pose(
    samples: Sequence[_PreparedPose], timestamp_s: float
) -> _PreparedPose:
    timestamps = [item.timestamp_s for item in samples]
    index = bisect.bisect_left(timestamps, timestamp_s)
    if index < len(samples) and abs(timestamps[index] - timestamp_s) <= 1.0e-12:
        return samples[index]
    if index == 0 or index == len(samples):
        raise ValueError("interpolation timestamp is outside the pose segment")
    first, second = samples[index - 1], samples[index]
    fraction = (timestamp_s - first.timestamp_s) / (
        second.timestamp_s - first.timestamp_s
    )
    return _PreparedPose(
        timestamp_s=timestamp_s,
        x_m=first.x_m + fraction * (second.x_m - first.x_m),
        y_m=first.y_m + fraction * (second.y_m - first.y_m),
        yaw_rad=first.yaw_rad + fraction * (second.yaw_rad - first.yaw_rad),
    )


def _collapse_route_points(
    poses: Sequence[_PreparedPose], minimum_step_m: float
) -> list[tuple[float, float]]:
    if not poses:
        return []
    points = [(poses[0].x_m, poses[0].y_m)]
    for pose in poses[1:]:
        point = (pose.x_m, pose.y_m)
        if (
            math.hypot(point[0] - points[-1][0], point[1] - points[-1][1])
            >= minimum_step_m
        ):
            points.append(point)
    return points


def _estimate_route_curvature(
    points: Sequence[tuple[float, float]],
    *,
    lookahead_m: float,
    minimum_path_m: float,
    resample_m: float,
) -> tuple[Optional[float], float]:
    if len(points) < 2:
        return None, 0.0
    metrics = [0.0]
    for first, second in zip(points, points[1:]):
        metrics.append(
            metrics[-1] + math.hypot(second[0] - first[0], second[1] - first[1])
        )
    available_m = min(float(lookahead_m), metrics[-1])
    if available_m + 1.0e-12 < minimum_path_m:
        return None, available_m

    sample_count = max(3, int(available_m / resample_m) + 1)
    distances = [
        available_m * index / (sample_count - 1) for index in range(sample_count)
    ]
    sampled = [_point_at_distance(points, metrics, distance) for distance in distances]
    vectors = [
        (second[0] - first[0], second[1] - first[1])
        for first, second in zip(sampled, sampled[1:])
    ]
    lengths = [math.hypot(vector[0], vector[1]) for vector in vectors]
    valid = [index for index, length_m in enumerate(lengths) if length_m > 1.0e-6]
    if len(valid) < 2:
        return None, available_m
    headings = [math.atan2(vectors[index][1], vectors[index][0]) for index in valid]
    valid_lengths = [lengths[index] for index in valid]
    headings = _unwrap_angles(headings)
    local_curvatures = [
        (second_heading - first_heading)
        / max(1.0e-6, 0.5 * (first_length + second_length))
        for first_heading, second_heading, first_length, second_length in zip(
            headings,
            headings[1:],
            valid_lengths,
            valid_lengths[1:],
        )
    ]
    if not local_curvatures:
        return None, available_m
    return float(statistics.median(local_curvatures)), available_m


def _point_at_distance(
    points: Sequence[tuple[float, float]], metrics: Sequence[float], distance_m: float
) -> tuple[float, float]:
    index = min(
        len(points) - 2,
        max(0, bisect.bisect_right(metrics, distance_m) - 1),
    )
    length_m = metrics[index + 1] - metrics[index]
    fraction = 0.0 if length_m <= 1.0e-12 else (distance_m - metrics[index]) / length_m
    first, second = points[index], points[index + 1]
    return (
        first[0] + fraction * (second[0] - first[0]),
        first[1] + fraction * (second[1] - first[1]),
    )


def _turn_direction(
    curvature: Optional[float],
    yaw_change: float,
    is_curve: bool,
    is_spin: bool,
) -> str:
    if not is_curve and not is_spin:
        return "none"
    # Low-translation yaw evidence is the stronger direction signal when both
    # detectors fire (for example, during a very tight creeping turn).
    if is_spin:
        signed_value = yaw_change
    else:
        signed_value = float(curvature)
    return "left" if signed_value > 0.0 else "right"


def _empty_evidence(timestamp_s: float, reason: str) -> TurnEvidence:
    return TurnEvidence(
        timestamp_s=timestamp_s,
        sufficient_data=False,
        is_turn=False,
        is_curve=False,
        is_spin=False,
        direction="none",
        reference_curvature_rad_per_m=None,
        future_path_m=0.0,
        spin_translation_m=None,
        spin_yaw_change_rad=None,
        reason=reason,
    )


__all__ = [
    "GateResult",
    "NORMAL_STRAIGHT_MAXIMUM_YAW_CHANGE_RAD",
    "NORMAL_STRAIGHT_MINIMUM_TRANSLATION_M",
    "PoseSample",
    "StraightCollection",
    "StraightGate",
    "StraightGateConfig",
    "TURN_CURVATURE_THRESHOLD_RAD_PER_M",
    "TurnEvidence",
    "TurnGate",
    "TurnGateConfig",
]
