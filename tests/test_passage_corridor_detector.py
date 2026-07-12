from datetime import datetime, timezone

from app.passage.corridor_detector import PassageCorridorDetector


def detector(**kwargs):
    defaults = {
        "points": ((0.5, 0.9), (0.5, 0.1)),
        "half_width": 0.10,
        "event_direction": "ENTRADA",
        "trigger_progress": 0.25,
        "min_observations": 3,
        "min_progress_delta": 0.08,
    }
    defaults.update(kwargs)
    return PassageCorridorDetector(**defaults)


def bbox_bottom(point):
    x, y = point
    return [x - 5, y - 10, x + 5, y]


def update(det, y, track_id=1, x=50):
    return det.update(
        track_id=track_id,
        bbox=bbox_bottom((x, y)),
        frame_width=100,
        frame_height=100,
        captured_at=datetime.now(timezone.utc),
    )


def test_point_outside_corridor():
    det = detector()
    observation, confirmation = update(det, 80, x=80)

    assert observation.inside_corridor is False
    assert confirmation is None


def test_point_inside_corridor_and_projection_progress():
    det = detector()
    progress, distance = det.project_normalized_point((0.5, 0.5))

    assert distance == 0
    assert 0.49 < progress < 0.51


def test_projection_on_polyline():
    det = detector(points=((0.1, 0.9), (0.5, 0.5), (0.9, 0.5)))

    progress, distance = det.project_normalized_point((0.7, 0.5))

    assert distance == 0
    assert 0.6 < progress < 1.0


def test_forward_motion_confirms_once():
    det = detector()

    assert update(det, 85)[1] is None
    assert update(det, 70)[1] is None
    confirmation = update(det, 55)[1]
    assert confirmation is not None
    assert confirmation.direction == "ENTRADA"
    assert update(det, 40)[1] is None


def test_backward_motion_not_confirmed_for_forward_detector():
    det = detector()

    update(det, 40)
    update(det, 55)
    _observation, confirmation = update(det, 70)

    assert confirmation is None


def test_jitter_without_accumulated_progress_does_not_confirm():
    det = detector()

    for y in [70, 69, 71, 70, 69]:
        _observation, confirmation = update(det, y)
        assert confirmation is None


def test_a1_generates_entrada_and_lateral_exit_after_confirmation_does_not_remove_event():
    det = detector(event_direction="ENTRADA", trigger_progress=0.15)

    update(det, 85)
    update(det, 70)
    confirmation = update(det, 55)[1]
    assert confirmation is not None
    assert confirmation.direction == "ENTRADA"
    assert update(det, 55, x=80)[1] is None


def test_a5_generates_salida():
    det = detector(event_direction="SALIDA", trigger_progress=0.55)

    update(det, 85)
    update(det, 60)
    confirmation = update(det, 40)[1]

    assert confirmation is not None
    assert confirmation.direction == "SALIDA"


def test_stationary_track_inside_corridor_does_not_confirm():
    det = detector()

    for _ in range(4):
        _observation, confirmation = update(det, 70)
        assert confirmation is None


def test_same_ids_are_isolated_and_reset_affects_one_instance():
    a1 = detector()
    a5 = detector(event_direction="SALIDA")

    update(a1, 85, track_id=5)
    update(a5, 85, track_id=5)
    a1.reset()

    assert a1._states == {}
    assert 5 in a5._states
