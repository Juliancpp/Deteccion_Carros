from datetime import datetime, timezone

from app.line_detector import VirtualLineDetector


def vehicle(track_id=1, center=(50, 40), bbox_size=10):
    x, y = center
    half = bbox_size // 2
    return {
        "id": track_id,
        "class": "Auto",
        "class_id": 2,
        "confidence": 0.9,
        "bbox": [x - half, y - half, x + half, y + half],
    }


def detector(**kwargs):
    return VirtualLineDetector(
        normalized_start=(0.2, 0.5),
        normalized_end=(0.8, 0.5),
        track_ttl_seconds=10,
        duplicate_cooldown_seconds=5,
        **kwargs,
    )


def update(line, center, track_id=1):
    return line.update(
        tracked_objects=[vehicle(track_id, center)],
        frame_width=100,
        frame_height=100,
        captured_at=datetime.now(timezone.utc),
        frame_sequence=1,
    )


def test_normalized_line_converts_to_pixels():
    line = detector()

    assert line.line_points_for_frame(1280, 720) == ((256, 360), (1024, 360))
    assert line.line_points_for_frame(640, 480) == ((128, 240), (512, 240))


def test_first_observation_does_not_generate_crossing():
    line = detector()

    assert update(line, (50, 40)) == []


def test_negative_to_positive_generates_single_crossing():
    line = detector()

    assert update(line, (50, 40)) == []
    crossings = update(line, (50, 60))
    assert len(crossings) == 1
    assert crossings[0].direction == "ENTRADA"
    assert update(line, (50, 70)) == []


def test_jitter_near_line_does_not_duplicate_with_cooldown():
    line = detector()

    update(line, (50, 40))
    assert len(update(line, (50, 60))) == 1
    update(line, (50, 40))
    assert update(line, (50, 60)) == []
    assert line.duplicate_suppressed_count == 1


def test_invert_direction_swaps_entrada_salida():
    line = detector(invert_direction=True)

    update(line, (50, 40))
    crossings = update(line, (50, 60))

    assert crossings[0].direction == "SALIDA"


def test_event_direction_filters():
    entrada = detector(event_directions="entrada")
    salida = detector(event_directions="salida")

    update(entrada, (50, 40))
    update(salida, (50, 40))

    assert len(update(entrada, (50, 60))) == 1
    assert update(salida, (50, 60)) == []


def test_ttl_removes_old_track_state():
    now = [0.0]
    line = VirtualLineDetector(
        normalized_start=(0.2, 0.5),
        normalized_end=(0.8, 0.5),
        track_ttl_seconds=1,
        duplicate_cooldown_seconds=5,
        time_fn=lambda: now[0],
    )

    update(line, (50, 40))
    assert 1 in line.previous_sides
    now[0] = 2.0
    update(line, (60, 40), track_id=2)

    assert 1 not in line.previous_sides


def test_reset_affects_only_instance_and_same_ids_do_not_mix():
    a1 = detector()
    a5 = detector()
    update(a1, (50, 40), track_id=5)
    update(a5, (50, 60), track_id=5)

    a1.reset()

    assert a1.previous_sides == {}
    assert a5.previous_sides[5] == 1
