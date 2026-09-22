"""Collection gates for retaining turns and sampled straight-driving data.

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
from dataclasses import dataclass, replace
from functools import wraps
from itertools import chain
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
    Union,
    cast,
)


TURN_CURVATURE_THRESHOLD_RAD_PER_M = math.radians(8.0)
NORMAL_STRAIGHT_MINIMUM_TRANSLATION_M = 0.10
NORMAL_STRAIGHT_MAXIMUM_YAW_CHANGE_RAD = math.radians(3.0)
DEFAULT_MAXIMUM_COLLECTION_INTERVAL_S = 45.0
MINIMUM_SAVED_COLLECTION_DURATION_S = 4.0
STABLE_STOP_WINDOW_S = 3.0
STABLE_STOP_MAXIMUM_TRANSLATION_M = 0.03
STABLE_STOP_MAXIMUM_YAW_CHANGE_RAD = math.radians(3.0)

GateResult = tuple[bool, float]
_EndCollectionMethod = TypeVar("_EndCollectionMethod", bound=Callable[..., GateResult])


@dataclass(frozen=True)
class CompletedCollection:
    """Final action window; the recorder must discard it when should_save is false."""

    start_timestamp_s: float
    end_timestamp_s: float
    target_confirmed: bool = True

    @property
    def duration_s(self) -> float:
        return self.end_timestamp_s - self.start_timestamp_s

    @property
    def should_save(self) -> bool:
        return self.duration_s >= MINIMUM_SAVED_COLLECTION_DURATION_S and self.target_confirmed


def with_hard_stop_conditions(
    method: _EndCollectionMethod,
) -> _EndCollectionMethod:
    """Decorate ``end_collection`` with shared pose and time hard stops.

    The gate must expose ``active_start_timestamp_s``,
    ``config.maximum_collection_interval_s``, and ``reset()``.  Gates that
    expose ``_pose_hard_stop_timestamp()`` additionally stop at a pose jump or
    sustained stable pose.  Natural end conditions are evaluated through the
    earliest hard stop first, so an earlier action node still wins when a
    streaming call arrives late.  Every completed window is published as
    ``last_completed_collection`` for the recorder's keep/discard decision.
    """

    @wraps(method)
    def wrapped(
        self: Any,
        pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> GateResult:
        timestamp_s = _finite_timestamp(decision_timestamp_s)
        active_start_s = self.active_start_timestamp_s
        if active_start_s is None:
            setattr(self, "_hard_stop_decorator_state", None)
            return method(self, pose_cache, timestamp_s)
        if timestamp_s < active_start_s:
            return method(self, pose_cache, timestamp_s)

        maximum_end_s = active_start_s + self.config.maximum_collection_interval_s
        hard_stop_s = maximum_end_s
        pose_hard_stop = getattr(self, "_pose_hard_stop_timestamp", None)
        if pose_hard_stop is not None:
            state = getattr(self, "_hard_stop_decorator_state", None)
            scan_start_s = (
                active_start_s
                if state is None or state[0] != active_start_s
                else min(float(state[1]), timestamp_s)
            )
            motion_start_s = getattr(self, "core_start_timestamp_s", None)
            pose_stop_s = pose_hard_stop(
                pose_cache,
                active_start_s if motion_start_s is None else motion_start_s,
                scan_start_s,
                min(timestamp_s, maximum_end_s),
            )
            if pose_stop_s is not None:
                hard_stop_s = min(hard_stop_s, pose_stop_s)

        evaluation_timestamp_s = min(timestamp_s, hard_stop_s)
        try:
            result = method(self, pose_cache, evaluation_timestamp_s)
        except ValueError:
            if timestamp_s + 1.0e-12 < hard_stop_s or len(pose_cache) >= 2:
                raise
            # A hard deadline remains actionable during a pose outage.  With
            # fewer than two samples the wrapped TurnGate method can only fail
            # its minimum-cache validation, so no earlier stop can be found.
            result = (False, evaluation_timestamp_s)

        if not result[0]:
            if timestamp_s + 1.0e-12 < hard_stop_s:
                setattr(
                    self,
                    "_hard_stop_decorator_state",
                    (active_start_s, evaluation_timestamp_s),
                )
                return result
            result = (True, hard_stop_s)
        target_confirmed = True
        confirm_target = getattr(self, "_hard_stop_target_confirmed", None)
        if confirm_target is not None:
            target_confirmed = confirm_target(pose_cache, active_start_s, result[1])
        prepare_continuation = getattr(self, "_prepare_continuation", None)
        if target_confirmed and prepare_continuation is not None:
            prepare_continuation(pose_cache, result[1])
        if self.active_start_timestamp_s is not None:
            finish_hard_stop = getattr(self, "_finish_hard_stop", None)
            if finish_hard_stop is None:
                self.reset()
            else:
                finish_hard_stop(result[1])
        setattr(self, "_hard_stop_decorator_state", None)
        self.last_completed_collection = CompletedCollection(active_start_s, result[1], target_confirmed)
        return result

    return cast(_EndCollectionMethod, wrapped)


# Compatibility name retained for collection targets that adopted the first
# Tmax-only version.  The implementation now also applies pose hard stops when
# the decorated class exposes ``_pose_hard_stop_timestamp``.
with_maximum_collection_interval = with_hard_stop_conditions


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
    """Motion confirmation, context and pose-quality thresholds (angles in radians)."""

    # Legacy route-curvature diagnostics; confirmation uses the motion thresholds below.
    curve_lookahead_m: float = 1.0
    curve_minimum_path_m: float = 0.30
    curve_maximum_lookahead_s: float = 15.0
    curve_resample_m: float = 0.05
    curvature_threshold_rad_per_m: float = TURN_CURVATURE_THRESHOLD_RAD_PER_M

    # Local curvature is diagnostic; sustained yaw and route direction confirm turns.
    turn_minimum_yaw_change_rad: float = math.radians(8.0)
    turn_yaw_deadband_rad: float = math.radians(2.0)
    turn_minimum_path_m: float = 0.15
    turn_minimum_heading_change_rad: float = math.radians(3.0)
    weak_turn_minimum_yaw_change_rad: float = math.radians(4.0)
    weak_turn_minimum_path_m: float = 0.08
    weak_turn_maximum_path_m: float = 0.80
    weak_turn_minimum_support_s: float = 1.5
    weak_turn_settle_s: float = 0.5
    pre_context_s: float = 4.0
    post_context_s: float = 4.0

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
    straight_recovery_maximum_line_error_m: float = 0.03
    straight_recovery_trend_window_s: float = 4.0
    stable_stop_window_s: float = STABLE_STOP_WINDOW_S
    stable_stop_maximum_translation_m: float = STABLE_STOP_MAXIMUM_TRANSLATION_M
    stable_stop_maximum_yaw_change_rad: float = STABLE_STOP_MAXIMUM_YAW_CHANGE_RAD
    maximum_collection_interval_s: float = DEFAULT_MAXIMUM_COLLECTION_INTERVAL_S
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
            "turn_minimum_yaw_change_rad": self.turn_minimum_yaw_change_rad,
            "turn_yaw_deadband_rad": self.turn_yaw_deadband_rad,
            "turn_minimum_path_m": self.turn_minimum_path_m,
            "turn_minimum_heading_change_rad": self.turn_minimum_heading_change_rad,
            "weak_turn_minimum_yaw_change_rad": self.weak_turn_minimum_yaw_change_rad,
            "weak_turn_minimum_path_m": self.weak_turn_minimum_path_m,
            "weak_turn_maximum_path_m": self.weak_turn_maximum_path_m,
            "weak_turn_minimum_support_s": self.weak_turn_minimum_support_s,
            "weak_turn_settle_s": self.weak_turn_settle_s,
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
            "straight_recovery_maximum_line_error_m": self.straight_recovery_maximum_line_error_m,
            "straight_recovery_trend_window_s": self.straight_recovery_trend_window_s,
            "stable_stop_window_s": self.stable_stop_window_s,
            "stable_stop_maximum_translation_m": self.stable_stop_maximum_translation_m,
            "stable_stop_maximum_yaw_change_rad": self.stable_stop_maximum_yaw_change_rad,
            "maximum_collection_interval_s": self.maximum_collection_interval_s,
            "probe_interval_s": self.probe_interval_s,
            "maximum_pose_gap_s": self.maximum_pose_gap_s,
            "maximum_pose_jump_m": self.maximum_pose_jump_m,
            "maximum_linear_speed_mps": self.maximum_linear_speed_mps,
            "maximum_yaw_step_rad": self.maximum_yaw_step_rad,
            "maximum_yaw_rate_rps": self.maximum_yaw_rate_rps,
        }
        invalid = [
            name for name, value in positive.items()
            if not math.isfinite(value) or value <= 0.0
        ]
        if invalid:
            raise ValueError(
                f"configuration values must be finite and positive: {', '.join(invalid)}"
            )
        nonnegative = {
            "pre_context_s": self.pre_context_s,
            "post_context_s": self.post_context_s,
            "start_lookahead_s": self.start_lookahead_s,
            "pose_smoothing_window_s": self.pose_smoothing_window_s,
            "minimum_route_step_m": self.minimum_route_step_m,
        }
        invalid = [
            name for name, value in nonnegative.items()
            if not math.isfinite(value) or value < 0.0
        ]
        if invalid:
            raise ValueError(
                f"configuration values must be finite and non-negative: {', '.join(invalid)}"
            )
        if self.curve_minimum_path_m > self.curve_lookahead_m:
            raise ValueError("curve_minimum_path_m cannot exceed curve_lookahead_m")
        if not self.turn_yaw_deadband_rad < self.weak_turn_minimum_yaw_change_rad <= self.turn_minimum_yaw_change_rad:
            raise ValueError("yaw thresholds must satisfy deadband < weak <= turn")
        if self.weak_turn_minimum_path_m > self.weak_turn_maximum_path_m:
            raise ValueError("weak turn minimum path cannot exceed maximum path")
        if self.pre_context_s >= self.maximum_collection_interval_s:
            raise ValueError("pre_context_s must be shorter than maximum collection interval")
        if self.maximum_collection_interval_s < self.start_lookahead_s:
            raise ValueError(
                "maximum_collection_interval_s cannot be shorter than start_lookahead_s"
            )


@dataclass(frozen=True)
class TurnEvidence:
    """Diagnostic result for one timestamp.

    ``sufficient_data`` is deliberately separate from ``is_turn``.  A caller
    must not interpret insufficient data as confirmed straight motion.
    Positive evidence includes the end of its observation window; natural
    recovery cannot be decided before that future motion has occurred.
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
    observation_end_timestamp_s: Optional[float] = None
    core_start_timestamp_s: Optional[float] = None
    core_end_timestamp_s: Optional[float] = None
    yaw_excursion_rad: Optional[float] = None
    route_heading_change_rad: Optional[float] = None


@dataclass(frozen=True)
class StraightGateConfig:
    """Sampling and motion thresholds for normal straight driving."""

    start_probability: float = 0.01
    minimum_collection_duration_s: float = 6.0
    maximum_collection_duration_s: float = 12.0
    maximum_collection_interval_s: float = DEFAULT_MAXIMUM_COLLECTION_INTERVAL_S
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
        if self.maximum_collection_interval_s < self.minimum_collection_duration_s:
            raise ValueError(
                "maximum_collection_interval_s cannot be shorter than the minimum duration"
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
    raw_segments: tuple[tuple[_PreparedPose, ...], ...] = ()


def _direction_lobes(poses: Sequence[_PreparedPose], deadband: float) -> list[tuple[int, int, bool]]:
    """Directional hysteresis: small reversals never accumulate absolute yaw."""
    lo = hi = start = extreme = 0
    direction = 0
    result = []
    for i in range(1, len(poses)):
        yaw = poses[i].yaw_rad
        if not direction:
            if yaw <= poses[lo].yaw_rad:
                lo = i
            if yaw >= poses[hi].yaw_rad:
                hi = i
            if yaw - poses[lo].yaw_rad >= deadband:
                start, extreme, direction = lo, i, 1
            elif poses[hi].yaw_rad - yaw >= deadband:
                start, extreme, direction = hi, i, -1
        elif direction * (yaw - poses[extreme].yaw_rad) > 1.0e-10:
            extreme = i
        elif direction * (poses[extreme].yaw_rad - yaw) >= deadband:
            result.append((start, extreme, True))
            start, extreme, direction = extreme, i, -direction
    if direction and extreme > start:
        result.append((start, extreme, False))
    return result


def _fit_route_direction(
    poses: Sequence[_PreparedPose], distance: Sequence[float],
) -> Optional[tuple[float, float, float]]:
    """Distance-weighted Huber fit; residual is measured about the fitted line."""
    if len(poses) < 3 or distance[-1] - distance[0] < 0.01:
        return None
    s = [v - distance[0] for v in distance]
    xy = [(p.x_m - poses[0].x_m, p.y_m - poses[0].y_m) for p in poses]
    weights = [max(s[1], 1.0e-6)] + [max(b - a, 1.0e-6) for a, b in zip(s, s[1:])]
    robust = [1.0] * len(s)
    for _ in range(3):
        w = [a * b for a, b in zip(weights, robust)]
        total = sum(w)
        mean_s = sum(v * t for v, t in zip(w, s)) / total
        mean_x = sum(v * p[0] for v, p in zip(w, xy)) / total
        mean_y = sum(v * p[1] for v, p in zip(w, xy)) / total
        denominator = sum(v * (t - mean_s) ** 2 for v, t in zip(w, s))
        if denominator < 1.0e-12:
            return None
        dx = sum(v * (t - mean_s) * (p[0] - mean_x) for v, t, p in zip(w, s, xy)) / denominator
        dy = sum(v * (t - mean_s) * (p[1] - mean_y) for v, t, p in zip(w, s, xy)) / denominator
        error = [math.hypot(p[0] - mean_x - dx * (t - mean_s), p[1] - mean_y - dy * (t - mean_s)) for p, t in zip(xy, s)]
        scale = max(0.001, statistics.median(error) * 1.4826)
        robust = [min(1.0, 1.5 * scale / max(e, 1.0e-12)) for e in error]
    length = math.hypot(dx, dy)
    if length < 0.1:
        return None
    dx, dy = dx / length, dy / length
    residuals = sorted(abs(dx * (p[1] - mean_y) - dy * (p[0] - mean_x)) for p in xy)
    return dx, dy, residuals[int(0.8 * (len(residuals) - 1))]


def _route_heading_change(poses: Sequence[_PreparedPose]) -> tuple[float, Optional[float], float]:
    distance = [0.0]
    for a, b in zip(poses, poses[1:]):
        distance.append(distance[-1] + math.hypot(b.x_m - a.x_m, b.y_m - a.y_m))
    length = distance[-1]
    if len(poses) < 3 or length < 0.06:
        return length, None, math.inf
    baseline = min(0.5, 0.35 * length)
    first_end = min(len(poses), max(3, bisect.bisect_left(distance, baseline) + 1))
    last_start = max(0, min(len(poses) - 3, bisect.bisect_left(distance, length - baseline)))
    first = _fit_route_direction(poses[:first_end], distance[:first_end])
    last = _fit_route_direction(poses[last_start:], distance[last_start:])
    if first is None or last is None:
        return length, None, math.inf
    angle = math.atan2(first[0] * last[1] - first[1] * last[0], first[0] * last[0] + first[1] * last[1])
    # Independent XY winding, never unwrap geometry against body yaw.
    route = _collapse_route_points(poses, 0.03)
    if len(route) >= 3:
        headings = _unwrap_angles([math.atan2(b[1] - a[1], b[0] - a[0]) for a, b in zip(route, route[1:])])
        angle += round((headings[-1] - headings[0] - angle) / math.tau) * math.tau
    return length, angle, max(first[2], last[2])


def _angular_support_s(poses: Sequence[_PreparedPose]) -> float:
    """Time supporting 5%-95% of the net turn; stationary tails add no support."""
    delta = poses[-1].yaw_rad - poses[0].yaw_rad
    if abs(delta) < 1.0e-12:
        return 0.0
    progress = [(p.yaw_rad - poses[0].yaw_rad) / delta for p in poses]
    a = next(i for i, v in enumerate(progress) if v >= 0.05)
    b = next(i for i, v in enumerate(progress) if v >= 0.95)
    return poses[b].timestamp_s - poses[a].timestamp_s


class _RandomSource(Protocol):
    def random(self) -> float: ...

    def uniform(self, minimum: float, maximum: float) -> float: ...


class TurnGate:
    """Streaming state machine for turn-data collection.

    A successful start owns the active turn state.  Ending is allowed after
    subsequent calls observe continuous normal straight driving or a stable
    stop.  Every active clip also has a hard maximum interval.
    """

    def __init__(self, config: Optional[TurnGateConfig] = None) -> None:
        self.config = config or TurnGateConfig()
        self.last_completed_collection: Optional[CompletedCollection] = None
        self._active_start_timestamp_s: Optional[float] = None
        self._core_start_timestamp_s: Optional[float] = None
        self._completed_through_timestamp_s: Optional[float] = None
        self._continuation_evidence: Optional[TurnEvidence] = None
        self._start_evidence_end_timestamp_s: Optional[float] = None
        self._start_evidence_start_timestamp_s: Optional[float] = None
        self._active_is_continuation = False
        self._last_end_check_timestamp_s: Optional[float] = None
        self._straight_recovery_start_timestamp_s: Optional[float] = None
        self._hard_stop_decorator_state: Optional[tuple[float, float]] = None

    @property
    def active_start_timestamp_s(self) -> Optional[float]:
        """Return the active clip's start node, if this gate owns one."""

        return self._active_start_timestamp_s

    @property
    def core_start_timestamp_s(self) -> Optional[float]:
        """Motion anchor, distinct from preceding capture context."""
        return self._core_start_timestamp_s

    @property
    def straight_recovery_start_timestamp_s(self) -> Optional[float]:
        """Return when sustained trailing-straight evidence began."""

        return self._straight_recovery_start_timestamp_s

    def reset(self) -> None:
        """Clear streaming state after an external recorder reset or abort."""

        self._active_start_timestamp_s = None
        self._core_start_timestamp_s = None
        self._completed_through_timestamp_s = None
        self._continuation_evidence = None
        self._start_evidence_end_timestamp_s = None
        self._start_evidence_start_timestamp_s = None
        self._active_is_continuation = False
        self._last_end_check_timestamp_s = None
        self._straight_recovery_start_timestamp_s = None
        self._hard_stop_decorator_state = None

    def start_collection(
        self,
        pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> GateResult:
        """Return ``(should_start, action_timestamp_s)`` for the stream.

        Confirmation and capture boundaries are separate. Return a cached
        node up to pre_context_s before the confirmed motion core.
        """

        timestamp_s = _finite_timestamp(decision_timestamp_s)
        if self._active_start_timestamp_s is not None:
            return False, timestamp_s
        samples = self._prepare_cache(pose_cache)
        probes = self._probe_times(
            timestamp_s,
            timestamp_s + self.config.start_lookahead_s,
        )
        # A six-second delayed recorder must still accumulate a slow turn over
        # the configured fifteen seconds. Revisit one bounded historical anchor.
        history_s = max(samples.segments[0][0].timestamp_s,
                        timestamp_s - self.config.curve_maximum_lookahead_s + self.config.spin_measurement_window_s)
        if self._completed_through_timestamp_s is not None:
            history_s = max(history_s, self._completed_through_timestamp_s)
        if history_s < timestamp_s:
            probes.append(history_s)
        continuation = self._continuation_evidence
        self._continuation_evidence = None
        evidence_candidates = chain(
            [continuation] if continuation is not None else [],
            (self._evaluate_prepared(samples, probe) for probe in probes),
        )
        for evidence in evidence_candidates:
            if evidence.is_turn:
                core_start_s = evidence.core_start_timestamp_s
                assert core_start_s is not None
                if (self._completed_through_timestamp_s is not None
                        and evidence.core_end_timestamp_s <= self._completed_through_timestamp_s + 1.0e-9):
                    continue
                segment, _ = self._continuous_segment(samples, core_start_s)
                if segment is None:
                    continue  # The recorder no longer has this history.
                capture_start_s = max(segment[0].timestamp_s, min(timestamp_s, core_start_s - self.config.pre_context_s))
                self._active_start_timestamp_s = capture_start_s
                self._core_start_timestamp_s = core_start_s
                self._start_evidence_end_timestamp_s = evidence.observation_end_timestamp_s
                self._start_evidence_start_timestamp_s = evidence.timestamp_s
                self._active_is_continuation = evidence is continuation
                # Recovery cannot precede the future motion that justified starting.
                self._last_end_check_timestamp_s = evidence.observation_end_timestamp_s
                self._straight_recovery_start_timestamp_s = None
                return True, capture_start_s
        return False, timestamp_s

    @with_hard_stop_conditions
    def end_collection(
        self,
        pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> GateResult:
        """Return ``(should_end, action_timestamp_s)`` for the stream.

        A turn-free observation is not enough.  The trajectory immediately
        before the action node must satisfy the configured normal-straight
        recovery window.  The shared hard-stop decorator handles stable poses,
        pose jumps, and the maximum interval.
        """

        timestamp_s = _finite_timestamp(decision_timestamp_s)
        active_start_s = self._active_start_timestamp_s
        if active_start_s is None:
            return False, timestamp_s
        if timestamp_s < active_start_s:
            raise ValueError("end timestamp cannot precede collection start")
        previous_check_s = self._last_end_check_timestamp_s
        if previous_check_s is not None and timestamp_s < previous_check_s:
            # The start decision already observed this future turn window.
            # Hard stops still apply, but natural recovery must wait for it.
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
                + max(self.config.straight_recovery_persistence_s,
                      self.config.post_context_s)
            )
            if probe_timestamp_s + 1.0e-12 < recovery_end_s:
                continue
            return True, recovery_end_s

        self._last_end_check_timestamp_s = timestamp_s
        return False, timestamp_s

    def _finish_hard_stop(self, action_timestamp_s: float) -> None:
        continuation = self._continuation_evidence
        self.reset()
        self._completed_through_timestamp_s = action_timestamp_s
        self._continuation_evidence = continuation

    def _prepare_continuation(self, pose_cache: Sequence[PoseSampleLike], end_s: float) -> None:
        """Carry actual core overlap over Tmax, not a pure context-only tail."""
        self._continuation_evidence = None
        if (self._active_start_timestamp_s is None or len(pose_cache) < 2
                or abs(end_s - self._active_start_timestamp_s - self.config.maximum_collection_interval_s) > 1.0e-8):
            return
        cache = self._prepare_cache(pose_cache)
        segment, _ = self._continuous_segment(cache, end_s)
        if segment is None:
            return
        for lookback_s in (self.config.curve_maximum_lookahead_s - self.config.spin_measurement_window_s,
                           self.config.pre_context_s, self.config.spin_measurement_window_s):
            evidence = self._evaluate_prepared(cache, max(segment[0].timestamp_s, end_s - lookback_s))
            if (evidence.is_turn and evidence.core_start_timestamp_s < end_s
                    and evidence.core_end_timestamp_s > end_s + 1.0e-9):
                # The full bounded core was confirmed before splitting. Its
                # remaining portion need not independently accumulate eight degrees.
                self._continuation_evidence = replace(evidence, core_start_timestamp_s=end_s)
                break

    def finish_collection(
        self, pose_cache: Sequence[PoseSampleLike], end_timestamp_s: float,
    ) -> GateResult:
        """Close an active clip at external EOF; first drain delayed start/end probes.

        Never supplies imaginary future poses. Repeated calls are idempotent.
        The caller must pass the actual media boundary and consume should_save.
        """
        end_s = _finite_timestamp(end_timestamp_s)
        start_s = self._active_start_timestamp_s
        if start_s is None:
            return False, end_s
        if end_s < start_s:
            raise ValueError("end timestamp cannot precede collection start")
        # Respect natural/pose/Tmax stops before applying the external boundary.
        result = self.end_collection(pose_cache, end_s)
        if result[0]:
            return result
        confirmed = self._hard_stop_target_confirmed(pose_cache, start_s, end_s)
        self._finish_hard_stop(end_s)
        self.last_completed_collection = CompletedCollection(start_s, end_s, confirmed)
        return True, end_s

    def _pose_hard_stop_timestamp(
        self,
        pose_cache: Sequence[PoseSampleLike],
        active_start_s: float,
        scan_start_s: float,
        scan_stop_s: float,
    ) -> Optional[float]:
        """Return the earliest pose-jump or stable-pose stop in the interval."""

        if scan_stop_s < active_start_s or len(pose_cache) < 2:
            return None
        raw = [_coerce_pose_sample(item) for item in pose_cache]
        raw_timestamps = [item.timestamp_s for item in raw]
        if any(
            right <= left for left, right in zip(raw_timestamps, raw_timestamps[1:])
        ):
            raise ValueError("pose timestamps must be strictly increasing")
        unwrapped_yaw = _unwrap_angles([item.yaw_rad for item in raw])
        raw_prepared = [
            _PreparedPose(item.timestamp_s, item.x_m, item.y_m, yaw_rad)
            for item, yaw_rad in zip(raw, unwrapped_yaw)
        ]
        candidates = []
        for first, second in zip(raw_prepared, raw_prepared[1:]):
            if (
                second.timestamp_s <= scan_start_s + 1.0e-12
                or second.timestamp_s > scan_stop_s + 1.0e-12
            ):
                continue
            dt_s = second.timestamp_s - first.timestamp_s
            translation_m = math.hypot(second.x_m - first.x_m, second.y_m - first.y_m)
            yaw_change_rad = abs(second.yaw_rad - first.yaw_rad)
            is_discontinuity = bool(
                dt_s > self.config.maximum_pose_gap_s
                or translation_m > self.config.maximum_pose_jump_m
                or yaw_change_rad > self.config.maximum_yaw_step_rad
                or (
                    dt_s <= self.config.maximum_pose_gap_s
                    and (
                        translation_m / dt_s > self.config.maximum_linear_speed_mps
                        or yaw_change_rad / dt_s > self.config.maximum_yaw_rate_rps
                    )
                )
            )
            if is_discontinuity:
                candidates.append(max(active_start_s, first.timestamp_s))
                break

        cache = self._prepare_cache(pose_cache)

        first_stable_probe_s = max(
            active_start_s + self.config.stable_stop_window_s,
            scan_start_s,
        )
        if first_stable_probe_s <= scan_stop_s + 1.0e-12:
            for probe_timestamp_s in self._probe_times(
                min(first_stable_probe_s, scan_stop_s), scan_stop_s
            ):
                if self._is_trailing_stable_stop(
                    cache,
                    probe_timestamp_s,
                    window_s=self.config.stable_stop_window_s,
                    maximum_translation_m=self.config.stable_stop_maximum_translation_m,
                    maximum_yaw_change_rad=self.config.stable_stop_maximum_yaw_change_rad,
                ):
                    candidates.append(probe_timestamp_s)
                    break
        return min(candidates) if candidates else None

    def _hard_stop_target_confirmed(
        self, pose_cache: Sequence[PoseSampleLike], start_s: float, end_s: float,
    ) -> bool:
        """Do not save a hard stop that cut off the turn used to start it."""
        evidence_end_s = self._start_evidence_end_timestamp_s
        evidence_start_s = self._start_evidence_start_timestamp_s
        if (evidence_end_s is not None and end_s >= evidence_end_s
                and evidence_start_s is not None
                and (start_s <= evidence_start_s or self._active_is_continuation)):
            return True
        # Confirm against the final window only; samples after its end must
        # not influence smoothing or supply the missing future turn again.
        poses = [_coerce_pose_sample(pose) for pose in pose_cache]
        poses = [pose for pose in poses if start_s <= pose.timestamp_s <= end_s]
        if len(poses) < 2:
            return False
        cache = self._prepare_cache(poses)
        return any(
            self._evaluate_prepared(cache, timestamp).is_turn
            for timestamp in self._probe_times(poses[0].timestamp_s, poses[-1].timestamp_s)
        )

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
        window = self._trailing_window(cache, timestamp_s, window_s)
        if window is None:
            return False
        start = window[0]
        stop = window[-1]
        translation_m = max(
            math.hypot(pose.x_m - start.x_m, pose.y_m - start.y_m) for pose in window
        )
        if translation_m + 1.0e-12 < minimum_translation_m:
            return False
        if max(p.yaw_rad for p in window) - min(p.yaw_rad for p in window) > maximum_yaw_change_rad:
            return False
        # A shallow sustained arc can pass a two-second straight check even
        # though it reached the eight-degree entry threshold over a longer span.
        # Require its directional trend to cease before starting post-context.
        trend = self._trailing_window(
            cache, timestamp_s,
            min(self.config.straight_recovery_trend_window_s,
                self.config.curve_maximum_lookahead_s),
        )
        if trend is not None:
            angle = abs(trend[-1].yaw_rad - trend[0].yaw_rad)
            variation = sum(abs(b.yaw_rad - a.yaw_rad) for a, b in zip(trend, trend[1:]))
            if (angle + 1.0e-12 >= self.config.turn_yaw_deadband_rad
                    and angle >= 0.85 * variation
                    and _angular_support_s(trend) + 1.0e-9 >= self.config.weak_turn_minimum_support_s):
                return False
        distance = [0.0]
        for first, second in zip(window, window[1:]):
            distance.append(distance[-1] + math.hypot(second.x_m - first.x_m, second.y_m - first.y_m))
        displacement = math.hypot(stop.x_m - start.x_m, stop.y_m - start.y_m)
        if displacement < 0.95 * distance[-1]:
            return False
        fit = _fit_route_direction(window, distance)
        return fit is not None and fit[2] <= self.config.straight_recovery_maximum_line_error_m

    def _is_trailing_stable_stop(
        self,
        cache: _PreparedCache,
        timestamp_s: float,
        *,
        window_s: float,
        maximum_translation_m: float,
        maximum_yaw_change_rad: float,
    ) -> bool:
        window = self._trailing_window(cache, timestamp_s, window_s)
        if window is None:
            return False
        start = window[0]
        maximum_translation = max(
            math.hypot(pose.x_m - start.x_m, pose.y_m - start.y_m) for pose in window
        )
        maximum_yaw_change = max(abs(pose.yaw_rad - start.yaw_rad) for pose in window)
        return bool(
            maximum_translation <= maximum_translation_m + 1.0e-12
            and maximum_yaw_change <= maximum_yaw_change_rad + 1.0e-12
        )

    def _trailing_window(
        self,
        cache: _PreparedCache,
        timestamp_s: float,
        window_s: float,
    ) -> Optional[list[_PreparedPose]]:
        segment, _failure = self._continuous_segment(cache, timestamp_s)
        if segment is None:
            return None
        start_timestamp_s = timestamp_s - window_s
        if start_timestamp_s < segment[0].timestamp_s - 1.0e-9:
            return None
        start_timestamp_s = max(start_timestamp_s, segment[0].timestamp_s)

        window = [_interpolate_pose(segment, start_timestamp_s)]
        window.extend(
            pose
            for pose in segment
            if start_timestamp_s < pose.timestamp_s < timestamp_s
        )
        window.append(_interpolate_pose(segment, timestamp_s))
        return window

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
        raw_segments: list[tuple[_PreparedPose, ...]] = []
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
            raw_segments.append(tuple(current))
            breaks.append((first.timestamp_s, second.timestamp_s, reason))
            current = [second]
        segments.append(
            tuple(_smooth_poses(current, self.config.pose_smoothing_window_s))
        )
        raw_segments.append(tuple(current))
        return _PreparedCache(tuple(segments), tuple(breaks), tuple(raw_segments))

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

        # Crop raw observations before filtering. Pose/yaw/geometry and filter
        # support all belong to the same bounded, continuous evidence window.
        raw_segment = next((s for s in cache.raw_segments if s[0].timestamp_s <= timestamp_s <= s[-1].timestamp_s), segment)
        support_end_s = min(raw_segment[-1].timestamp_s, timestamp_s + self.config.curve_maximum_lookahead_s)
        bounded = [_interpolate_pose(raw_segment, timestamp_s)]
        bounded.extend(p for p in raw_segment if timestamp_s < p.timestamp_s <= support_end_s + 1.0e-9)
        segment = tuple(_smooth_poses(bounded, self.config.pose_smoothing_window_s))

        anchor = _interpolate_pose(segment, timestamp_s)
        spin_stop_s = timestamp_s + self.config.spin_measurement_window_s
        spin_window_complete = spin_stop_s <= segment[-1].timestamp_s + 1.0e-9
        spin_stop_s = min(spin_stop_s, segment[-1].timestamp_s)

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
        # Accumulate distance, not a fixed two-second budget. Stop observing as
        # soon as the target path is covered; stationary tail samples add no evidence.
        route = []
        route_length_m = 0.0
        for pose in segment:
            if not timestamp_s <= pose.timestamp_s <= curve_stop_s:
                continue
            if route:
                step_m = math.hypot(pose.x_m - route[-1].x_m, pose.y_m - route[-1].y_m)
                if step_m < self.config.minimum_route_step_m:
                    continue
                route_length_m += step_m
            else:
                route_length_m = math.hypot(pose.x_m - anchor.x_m, pose.y_m - anchor.y_m)
            route.append(pose)
            if route_length_m >= self.config.curve_lookahead_m:
                break
        route_points = [(pose.x_m, pose.y_m) for pose in route]
        curvature, future_path_m = _estimate_route_curvature(
            route_points,
            lookahead_m=self.config.curve_lookahead_m,
            minimum_path_m=self.config.curve_minimum_path_m,
            resample_m=self.config.curve_resample_m,
            initial_path_m=(
                math.hypot(route[0].x_m - anchor.x_m, route[0].y_m - anchor.y_m)
                if route else 0.0
            ),
        )
        core, uncertain = self._motion_core(segment, timestamp_s)
        is_curve = core is not None and core[0] != "spin_turn"
        if core is not None and core[0] == "spin_turn":
            is_spin = True
        if is_spin and core is None:
            spin_support_end_s = min(segment[-1].timestamp_s,
                                     spin_stop_s + 0.5 * self.config.pose_smoothing_window_s)
            core = ("spin_turn", anchor, spin_stop, spin_yaw_change, None, spin_support_end_s)
        curve_sufficient = curvature is not None or spin_translation <= self.config.spin_maximum_translation_m
        sufficient = bool(is_spin or is_curve or (spin_window_complete and curve_sufficient and not uncertain))
        is_turn = bool(is_spin or is_curve)
        angle = core[3] if core is not None else 0.0
        return TurnEvidence(
            timestamp_s=timestamp_s, sufficient_data=sufficient,
            is_turn=is_turn, is_curve=is_curve, is_spin=is_spin,
            direction=("left" if angle > 0.0 else "right") if is_turn else "none",
            reference_curvature_rad_per_m=curvature, future_path_m=future_path_m,
            spin_translation_m=spin_translation, spin_yaw_change_rad=spin_yaw_change,
            reason=core[0] if core else "uncertain_turn" if uncertain else "straight" if sufficient else "future_path_insufficient" if spin_window_complete else "future_time_insufficient",
            observation_end_timestamp_s=core[5] if core else None,
            core_start_timestamp_s=core[1].timestamp_s if core else None,
            core_end_timestamp_s=core[2].timestamp_s if core else None,
            yaw_excursion_rad=core[3] if core else None,
            route_heading_change_rad=core[4] if core else None,
        )

    def _motion_core(
        self, segment: Sequence[_PreparedPose], timestamp_s: float,
    ) -> tuple[Optional[tuple[str, _PreparedPose, _PreparedPose, float, Optional[float], float]], bool]:
        """Confirm one directional lobe from a single bounded observation window.

        Weak lobes must close or settle before confirmation; a short online
        prefix of a long shallow drift is not a completed short turn.
        """
        cfg = self.config
        stop_s = min(segment[-1].timestamp_s, timestamp_s + cfg.curve_maximum_lookahead_s)
        window = [_interpolate_pose(segment, timestamp_s)]
        window.extend(p for p in segment if timestamp_s < p.timestamp_s <= stop_s + 1.0e-9)
        if len(window) < 3:
            return None, False
        # Do not borrow evidence beyond an intervening stable stop.
        times = [p.timestamp_s for p in window]
        for i, pose in enumerate(window):
            left_s = pose.timestamp_s - cfg.stable_stop_window_s
            if left_s < timestamp_s - 1.0e-9:
                continue
            left_s = max(timestamp_s, left_s)
            anchor = _interpolate_pose(window, left_s)
            tail = window[bisect.bisect_left(times, left_s):i + 1]
            if (max(math.hypot(p.x_m - anchor.x_m, p.y_m - anchor.y_m) for p in tail) <= cfg.stable_stop_maximum_translation_m
                    and max(abs(p.yaw_rad - anchor.yaw_rad) for p in tail) <= cfg.stable_stop_maximum_yaw_change_rad):
                window = window[:i + 1]
                break
        uncertain = False
        for a, b, reversed_direction in _direction_lobes(window, cfg.turn_yaw_deadband_rad):
            core = window[a:b + 1]
            if len(core) < 3:
                continue
            # A parked anchor cannot nominate an unrelated distant manoeuvre.
            if core[0].timestamp_s > timestamp_s + cfg.spin_measurement_window_s + 1.0e-9:
                continue
            angle = core[-1].yaw_rad - core[0].yaw_rad
            magnitude = abs(angle)
            if magnitude + 1.0e-12 < cfg.weak_turn_minimum_yaw_change_rad:
                continue
            uncertain = True
            path, heading, residual = _route_heading_change(core)
            displacement = max(math.hypot(p.x_m - core[0].x_m, p.y_m - core[0].y_m) for p in core)
            aligned = heading is not None and heading * angle > 0.0
            branch = None
            # Keep the original short-window spin contract. Slow stationary
            # rotations below it remain uncertain; stable-stop semantics prevail.
            if (core[-1].timestamp_s - core[0].timestamp_s <= cfg.spin_measurement_window_s + 1.0e-9
                    and displacement <= cfg.spin_maximum_translation_m
                    and magnitude + 1.0e-12 >= cfg.spin_minimum_yaw_change_rad):
                branch = 'spin_turn'
            elif (magnitude + 1.0e-12 >= cfg.turn_minimum_yaw_change_rad
                  and path >= cfg.turn_minimum_path_m and aligned
                  and abs(heading) + 1.0e-12 >= cfg.turn_minimum_heading_change_rad):
                branch = 'curve_turn'
            settled_until_s = core[-1].timestamp_s + cfg.weak_turn_settle_s
            settled = (settled_until_s <= window[-1].timestamp_s + 1.0e-9
                       and max(abs(p.yaw_rad - core[-1].yaw_rad) for p in window[b:]
                               if p.timestamp_s <= settled_until_s + 1.0e-9) <= 0.5 * cfg.turn_yaw_deadband_rad)
            variation = sum(abs(q.yaw_rad - p.yaw_rad) for p, q in zip(core, core[1:]))
            consistency = magnitude / max(variation, 1.0e-12)
            if (branch is None and (reversed_direction or settled)
                    and cfg.weak_turn_minimum_path_m <= path <= cfg.weak_turn_maximum_path_m
                    and _angular_support_s(core) + 1.0e-9 >= cfg.weak_turn_minimum_support_s
                    and consistency >= 0.85 and aligned
                    and max(math.radians(2.0), 0.4 * magnitude) <= abs(heading) <= 2.0 * magnitude + math.radians(3.0)
                    and residual <= max(0.003, 0.01 * path)):
                branch = 'short_slow_turn'
            if branch:
                observed_end = core[-1].timestamp_s
                if branch == 'short_slow_turn':
                    observed_end = (window[-1].timestamp_s if reversed_direction else settled_until_s)
                # Centred filtering may use half a second beyond the core.
                observed_end = min(segment[-1].timestamp_s, max(observed_end, core[-1].timestamp_s + 0.5 * cfg.pose_smoothing_window_s))
                return (branch, core[0], core[-1], angle, heading, observed_end), uncertain
        return None, uncertain

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
        self.last_completed_collection: Optional[CompletedCollection] = None
        self.turn_gate = turn_gate or TurnGate()
        self._rng = rng or random.Random()
        self._active_collection: Optional[StraightCollection] = None
        self._last_collection: Optional[StraightCollection] = None
        self._last_evidence: Optional[TurnEvidence] = None
        self._hard_stop_decorator_state: Optional[tuple[float, float]] = None

    @property
    def active_collection(self) -> Optional[StraightCollection]:
        """Return the active clip budget, if collection has started."""

        return self._active_collection

    @property
    def active_start_timestamp_s(self) -> Optional[float]:
        """Return the active clip's start node for the hard-stop decorator."""

        collection = self._active_collection
        return None if collection is None else collection.start_timestamp_s

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
        self._hard_stop_decorator_state = None

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
            curvature_threshold_rad_per_m=self.turn_gate.config.curvature_threshold_rad_per_m,
        ):
            return False, timestamp_s

        probability = self.config.start_probability
        if probability <= 0.0:
            return False, timestamp_s
        if probability < 1.0 and self._rng.random() >= probability:
            return False, timestamp_s

        maximum_duration_s = min(
            self.config.maximum_collection_duration_s,
            self.config.maximum_collection_interval_s,
        )
        duration_s = min(
            self._rng.uniform(
                self.config.minimum_collection_duration_s,
                maximum_duration_s,
            ),
            maximum_duration_s,
        )
        self._active_collection = StraightCollection(
            start_timestamp_s=timestamp_s,
            target_duration_s=duration_s,
            end_timestamp_s=timestamp_s + duration_s,
        )
        return True, timestamp_s

    def _pose_hard_stop_timestamp(
        self,
        pose_cache: Sequence[PoseSampleLike],
        active_start_s: float,
        scan_start_s: float,
        scan_stop_s: float,
    ) -> Optional[float]:
        return self.turn_gate._pose_hard_stop_timestamp(
            pose_cache,
            active_start_s,
            scan_start_s,
            scan_stop_s,
        )

    def _finish_hard_stop(self, action_timestamp_s: float) -> None:
        collection = self._active_collection
        if collection is not None:
            self._last_collection = StraightCollection(
                start_timestamp_s=collection.start_timestamp_s,
                target_duration_s=action_timestamp_s - collection.start_timestamp_s,
                end_timestamp_s=action_timestamp_s,
            )
        self._active_collection = None

    @with_hard_stop_conditions
    def end_collection(
        self,
        _pose_cache: Sequence[PoseSampleLike],
        decision_timestamp_s: float,
    ) -> GateResult:
        """Return ``(should_end, action_timestamp_s)`` for the stream.

        The natural deadline is the selected 6--12 second duration; the shared
        decorator can stop earlier at a pose jump or stable stop.  Completed
        windows shorter than four seconds must be discarded by the recorder.
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
    curvature_threshold_rad_per_m: float,
) -> bool:
    return bool(
        evidence.sufficient_data
        and not evidence.is_turn
        and evidence.reference_curvature_rad_per_m is not None
        and abs(evidence.reference_curvature_rad_per_m) < curvature_threshold_rad_per_m
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
    initial_path_m: float = 0.0,
) -> tuple[Optional[float], float]:
    if len(points) < 2:
        return None, 0.0
    # Interpolated anchor distance counts toward support without inserting a
    # synthetic vertex into the three-point curvature calculation.
    metrics = [initial_path_m]
    for first, second in zip(points, points[1:]):
        metrics.append(
            metrics[-1] + math.hypot(second[0] - first[0], second[1] - first[1])
        )
    available_m = min(float(lookahead_m), metrics[-1])
    if available_m + 1.0e-12 < minimum_path_m:
        return None, available_m

    # Thin actual vertices instead of inserting collinear points into sparse
    # poses: those artificial zero turns would bias the curvature median.
    sampled = [points[0]]
    last_distance_m = initial_path_m
    for point, distance_m in zip(points[1:], metrics[1:]):
        if distance_m > available_m + 1.0e-12:
            break
        if distance_m - last_distance_m + 1.0e-12 >= resample_m:
            sampled.append(point)
            last_distance_m = distance_m

    local_curvatures = _route_curvatures(sampled)
    if not local_curvatures:
        return None, available_m
    return float(statistics.median(local_curvatures)), available_m


def _route_curvatures(points: Sequence[tuple[float, float]]) -> list[float]:
    """Signed three-point circle curvature, independent of vertex spacing."""
    local_curvatures = []
    for first, middle, last in zip(points, points[1:], points[2:]):
        ax, ay = middle[0] - first[0], middle[1] - first[1]
        bx, by = last[0] - middle[0], last[1] - middle[1]
        denominator = math.hypot(ax, ay) * math.hypot(bx, by) * math.hypot(ax + bx, ay + by)
        if denominator > 1.0e-12:
            local_curvatures.append(2.0 * (ax * by - ay * bx) / denominator)
    return local_curvatures


def _turn_direction(
    curvature: Optional[float],
    yaw_change: float,
    is_curve: bool,
    is_spin: bool,
) -> str:
    if not is_curve and not is_spin:
        return "none"
    # Spin direction comes from yaw; arc direction comes from the XY route.
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
    "CompletedCollection",
    "DEFAULT_MAXIMUM_COLLECTION_INTERVAL_S",
    "GateResult",
    "MINIMUM_SAVED_COLLECTION_DURATION_S",
    "NORMAL_STRAIGHT_MAXIMUM_YAW_CHANGE_RAD",
    "NORMAL_STRAIGHT_MINIMUM_TRANSLATION_M",
    "PoseSample",
    "StraightCollection",
    "StraightGate",
    "StraightGateConfig",
    "STABLE_STOP_MAXIMUM_TRANSLATION_M",
    "STABLE_STOP_MAXIMUM_YAW_CHANGE_RAD",
    "STABLE_STOP_WINDOW_S",
    "TURN_CURVATURE_THRESHOLD_RAD_PER_M",
    "TurnEvidence",
    "TurnGate",
    "TurnGateConfig",
    "with_hard_stop_conditions",
    "with_maximum_collection_interval",
]
