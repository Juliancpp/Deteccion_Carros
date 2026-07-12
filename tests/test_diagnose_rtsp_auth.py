import sys

from app.settings import CameraSettings, DVRSettings
from scripts import diagnose_rtsp_auth


class FakeCapture:
    def __init__(self, _url, _backend):
        self.url = _url

    def isOpened(self):
        return False

    def release(self):
        pass


class FakeCV2:
    CAP_FFMPEG = 1900
    VideoCapture = FakeCapture


def test_diagnose_output_does_not_print_secrets(monkeypatch, capsys):
    password = "@:/#%"
    dvr = DVRSettings(
        host="192.168.0.100",
        rtsp_port=554,
        username="detector",
        password=password,
    )
    camera = CameraSettings(
        camera_id="A1",
        name="Camara_A1",
        enabled=True,
        channel=101,
        target_fps=5.0,
    )
    monkeypatch.setitem(sys.modules, "cv2", FakeCV2)

    result = diagnose_rtsp_auth._diagnose_camera(dvr, camera)
    output = capsys.readouterr().out

    assert result is False
    assert password not in output
    assert "%40%3A%2F%23%25" not in output
    assert "rtsp://detector:***@" in output
