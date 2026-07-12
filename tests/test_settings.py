from urllib.parse import unquote

import pytest

from app.settings import (
    SettingsError,
    build_rtsp_url,
    load_settings,
    sanitize_sensitive_text,
    sanitize_rtsp_url,
)


def valid_env(**overrides):
    env = {
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
    env.update(overrides)
    return env


def test_load_settings_success():
    settings = load_settings(environ=valid_env())

    assert settings.dvr.host == "192.168.0.100"
    assert settings.dvr.rtsp_port == 554
    assert [camera.camera_id for camera in settings.cameras] == ["A1", "A5"]
    assert settings.cameras[0].channel == 101
    assert settings.cameras[1].target_fps == 5.0


def test_missing_password_raises_without_secret():
    env = valid_env(DVR_PASSWORD="")

    with pytest.raises(SettingsError) as exc_info:
        load_settings(environ=env)

    message = str(exc_info.value)
    assert "DVR_PASSWORD" in message
    assert "secret" not in message


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
    ],
)
def test_bool_conversion(raw_value, expected):
    settings = load_settings(environ=valid_env(CAMERA_A1_ENABLED=raw_value))

    assert settings.cameras[0].enabled is expected


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_port(port):
    with pytest.raises(SettingsError):
        load_settings(environ=valid_env(DVR_RTSP_PORT=port))


def test_duplicate_camera_ids():
    with pytest.raises(SettingsError):
        load_settings(environ=valid_env(CAMERA_A5_ID="A1"))


def test_rtsp_url_channel_101():
    settings = load_settings(environ=valid_env())

    url = build_rtsp_url(settings.dvr, settings.cameras[0])

    assert url == "rtsp://detector:secret@192.168.0.100:554/Streaming/Channels/101"


def test_rtsp_url_channel_501():
    settings = load_settings(environ=valid_env())

    url = build_rtsp_url(settings.dvr, settings.cameras[1])

    assert url == "rtsp://detector:secret@192.168.0.100:554/Streaming/Channels/501"


def test_rtsp_url_escapes_special_characters():
    password = "@:/#%"
    settings = load_settings(environ=valid_env(DVR_USERNAME="user:name", DVR_PASSWORD=password))

    url = build_rtsp_url(settings.dvr, settings.cameras[0])

    assert "user%3Aname" in url
    assert "%40%3A%2F%23%25" in url
    assert unquote(url).startswith("rtsp://user:name:@:/#%@")


def test_sanitize_rtsp_url_hides_password():
    password = "secret@:/#%"
    settings = load_settings(environ=valid_env(DVR_PASSWORD=password))
    url = build_rtsp_url(settings.dvr, settings.cameras[0])

    sanitized_url = sanitize_rtsp_url(url)

    assert password not in sanitized_url
    assert "secret" not in sanitized_url
    assert sanitized_url == "rtsp://detector:***@192.168.0.100:554/Streaming/Channels/101"


def test_sanitize_rtsp_url_can_hide_username():
    settings = load_settings(environ=valid_env())
    url = build_rtsp_url(settings.dvr, settings.cameras[0])

    sanitized_url = sanitize_rtsp_url(url, hide_username=True)

    assert sanitized_url == "rtsp://***:***@192.168.0.100:554/Streaming/Channels/101"


def test_load_settings_from_env_file_overrides_exported_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DVR_HOST=192.168.0.100",
                "DVR_RTSP_PORT=554",
                "DVR_USERNAME=detector",
                "DVR_PASSWORD=file-password",
                "CAMERA_A1_ENABLED=true",
                "CAMERA_A1_ID=A1",
                "CAMERA_A1_NAME=Camara_A1",
                "CAMERA_A1_CHANNEL=101",
                "CAMERA_A1_TARGET_FPS=5",
                "CAMERA_A5_ENABLED=true",
                "CAMERA_A5_ID=A5",
                "CAMERA_A5_NAME=Camara_A5",
                "CAMERA_A5_CHANNEL=501",
                "CAMERA_A5_TARGET_FPS=5",
            ]
        )
    )
    monkeypatch.setenv("DVR_PASSWORD", "exported-password")

    settings = load_settings(env_path=env_file, override_env=True)

    assert settings.dvr.password == "file-password"


@pytest.mark.parametrize(
    "placeholder",
    ["CAMBIAR_LOCALMENTE", "PON_AQUI_LA_CONTRASENA_TEMPORAL", "PASSWORD", "CHANGE_ME"],
)
def test_placeholder_passwords_are_rejected(placeholder):
    with pytest.raises(SettingsError):
        load_settings(environ=valid_env(DVR_PASSWORD=placeholder))


def test_rtsp_url_does_not_encode_twice():
    settings = load_settings(environ=valid_env(DVR_PASSWORD="@"))

    url = build_rtsp_url(settings.dvr, settings.cameras[0])

    assert "%40" in url
    assert "%2540" not in url


def test_sanitize_sensitive_text_removes_raw_and_encoded_password():
    password = "@:/#% ?&"
    settings = load_settings(environ=valid_env(DVR_PASSWORD=password))
    url = build_rtsp_url(settings.dvr, settings.cameras[0])
    text = f"fallo {url} password={password}"

    sanitized = sanitize_sensitive_text(text, settings.dvr, camera_settings=settings.cameras[0])

    assert password not in sanitized
    assert "%40%3A%2F%23%25%20%3F%26" not in sanitized
    assert "rtsp://" in sanitized
    assert "***" in sanitized


def test_config_import_does_not_require_dvr_credentials():
    import app.config as config

    assert config.VIDEO_PATH.endswith("data/videos/test.mp4")
