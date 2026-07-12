import pytest

from app.settings import SettingsError, load_settings


def base_env():
    return {
        "DVR_HOST": "192.168.0.100",
        "DVR_RTSP_PORT": "554",
        "DVR_USERNAME": "detector",
        "DVR_PASSWORD": "secret",
        "CAMERA_A1_ENABLED": "true",
        "CAMERA_A1_ID": "A1",
        "CAMERA_A1_NAME": "Camara_A1",
        "CAMERA_A1_CHANNEL": "101",
        "CAMERA_A1_TARGET_FPS": "5",
        "CAMERA_A5_ENABLED": "true",
        "CAMERA_A5_ID": "A5",
        "CAMERA_A5_NAME": "Camara_A5",
        "CAMERA_A5_CHANNEL": "501",
        "CAMERA_A5_TARGET_FPS": "5",
    }


def test_line_settings_defaults_are_loaded_independently():
    settings = load_settings(environ=base_env())

    a1, a5 = settings.cameras
    assert a1.line is not a5.line
    assert a1.line.start_x == 0.20
    assert a5.line.end_x == 0.80
    assert a1.line.event_directions == "both"
    assert a1.passage.event == "ENTRADA"
    assert a5.passage.event == "SALIDA"
    assert a1.passage.points is not a5.passage.points


def test_line_settings_from_environment():
    env = base_env()
    env.update(
        {
            "CAMERA_A1_LINE_ENABLED": "false",
            "CAMERA_A1_LINE_START_X": "0.10",
            "CAMERA_A1_LINE_START_Y": "0.20",
            "CAMERA_A1_LINE_END_X": "0.90",
            "CAMERA_A1_LINE_END_Y": "0.80",
            "CAMERA_A1_LINE_INVERT_DIRECTION": "true",
            "CAMERA_A1_EVENT_DIRECTIONS": "entrada",
        }
    )

    camera = load_settings(environ=env).cameras[0]

    assert camera.line.enabled is False
    assert camera.line.start_x == 0.10
    assert camera.line.invert_direction is True
    assert camera.line.event_directions == "entrada"


def test_passage_settings_from_environment():
    env = base_env()
    env.update(
        {
            "CAMERA_A1_PASSAGE_ENABLED": "true",
            "CAMERA_A1_PASSAGE_EVENT": "ENTRADA",
            "CAMERA_A1_PASSAGE_DIRECTION": "forward",
            "CAMERA_A1_TRIGGER_PROGRESS": "0.15",
            "CAMERA_A1_CORRIDOR_POINTS": "0.1,0.9;0.2,0.8;0.3,0.7",
            "CAMERA_A1_CORRIDOR_HALF_WIDTH": "0.07",
        }
    )

    passage = load_settings(environ=env).cameras[0].passage

    assert passage.points == ((0.1, 0.9), (0.2, 0.8), (0.3, 0.7))
    assert passage.half_width == 0.07
    assert passage.trigger_progress == 0.15


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CAMERA_A1_LINE_START_X", "-0.1"),
        ("CAMERA_A1_LINE_START_Y", "1.1"),
        ("CAMERA_A1_LINE_ENABLED", "yes"),
        ("CAMERA_A1_EVENT_DIRECTIONS", "invalid"),
        ("CAMERA_A1_CORRIDOR_POINTS", "0.1,0.9"),
        ("CAMERA_A1_CORRIDOR_POINTS", "0.1,0.9;1.2,0.8"),
        ("CAMERA_A1_CORRIDOR_HALF_WIDTH", "0"),
        ("CAMERA_A1_PASSAGE_EVENT", "OTRO"),
        ("CAMERA_A1_PASSAGE_DIRECTION", "sideways"),
        ("CAMERA_A1_TRIGGER_PROGRESS", "1.2"),
    ],
)
def test_invalid_line_settings_are_rejected(key, value):
    env = base_env()
    env[key] = value

    with pytest.raises(SettingsError):
        load_settings(environ=env)


def test_equal_line_points_are_rejected():
    env = base_env()
    env.update(
        {
            "CAMERA_A1_LINE_START_X": "0.5",
            "CAMERA_A1_LINE_START_Y": "0.5",
            "CAMERA_A1_LINE_END_X": "0.5",
            "CAMERA_A1_LINE_END_Y": "0.5",
        }
    )

    with pytest.raises(SettingsError):
        load_settings(environ=env)
