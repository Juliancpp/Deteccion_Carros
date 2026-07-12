import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.calibrate_hotel_zones import (
    CAMERA_ROOMS,
    HOTEL_REAL_PRESET,
    HotelLayout,
    load_layout,
    validate_layout,
    write_env_layout,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_calibrate_phase1_script_help_direct():
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "calibrate_hotel_phase1.py"), "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--preset" in result.stdout


def test_calibrate_phase1_script_help_as_module():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.calibrate_hotel_phase1", "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--validate-only" in result.stdout


def test_hotel_real_preset_rooms_and_normalized_coordinates():
    assert set(HOTEL_REAL_PRESET["A1"]["rooms"]) == {1, 2, 3}
    assert set(HOTEL_REAL_PRESET["A5"]["rooms"]) == {4, 5, 6}

    for camera_id, preset in HOTEL_REAL_PRESET.items():
        assert len(preset["corridor"]) >= 2
        for point in preset["corridor"]:
            assert 0.0 <= point[0] <= 1.0
            assert 0.0 <= point[1] <= 1.0
        for polygon in preset["rooms"].values():
            assert len(polygon) >= 3
            for point in polygon:
                assert 0.0 <= point[0] <= 1.0
                assert 0.0 <= point[1] <= 1.0


def test_camera_room_sets_are_isolated():
    assert CAMERA_ROOMS["A1"] == (1, 2, 3)
    assert CAMERA_ROOMS["A5"] == (6, 5, 4)

    a1_layout = HotelLayout("A1", [(0.1, 0.9), (0.9, 0.1)], 0.1, False, {4: [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2)]})
    with pytest.raises(ValueError):
        validate_layout(a1_layout)

    a5_layout = HotelLayout("A5", [(0.1, 0.9), (0.9, 0.1)], 0.1, False, {1: [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2)]})
    with pytest.raises(ValueError):
        validate_layout(a5_layout)


def test_preset_corridors_are_ordered_exterior_to_interior():
    assert HOTEL_REAL_PRESET["A1"]["corridor"][0][1] > HOTEL_REAL_PRESET["A1"]["corridor"][-1][1]
    assert HOTEL_REAL_PRESET["A5"]["corridor"][0][0] < HOTEL_REAL_PRESET["A5"]["corridor"][-1][0]


def test_hotel_real_preset_is_used_when_env_has_no_corridor(monkeypatch):
    class Camera:
        camera_id = "A1"
        rooms = ()

        class corridor:
            points = ((0.99, 0.99), (0.98, 0.98))
            half_width = 0.2
            invert_direction = True

    monkeypatch.delenv("CAMERA_A1_CORRIDOR_POINTS", raising=False)

    layout = load_layout(Camera(), preset="hotel-real")

    assert layout.corridor == HOTEL_REAL_PRESET["A1"]["corridor"]
    assert layout.half_width == HOTEL_REAL_PRESET["A1"]["half_width"]


def test_write_a1_preserves_a5_and_credentials(tmp_path):
    env_path = tmp_path / ".env"
    backup_path = tmp_path / ".env.backup_hotel_phase1"
    env_path.write_text(
        "\n".join(
            [
                "DVR_USERNAME=detector",
                "DVR_PASSWORD=\"SECRET_VALUE\"",
                "CAMERA_A5_CORRIDOR_POINTS=\"0.1,0.9;0.9,0.1\"",
                "CAMERA_A5_ROOM_4_POLYGON=\"0.1,0.1;0.2,0.1;0.2,0.2\"",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)

    preset = HOTEL_REAL_PRESET["A1"]
    layout = HotelLayout(
        "A1",
        list(preset["corridor"]),
        preset["half_width"],
        preset["invert_direction"],
        {room: list(points) for room, points in preset["rooms"].items()},
    )

    updated = write_env_layout(layout, env_path=env_path, backup_path=backup_path)
    content = env_path.read_text(encoding="utf-8")

    assert backup_path.exists()
    assert stat.S_IMODE(env_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    assert "DVR_PASSWORD=\"SECRET_VALUE\"" in content
    assert "CAMERA_A5_CORRIDOR_POINTS" in content
    assert "CAMERA_A1_ROOM_1_POLYGON" in content
    assert "CAMERA_A1_ROOM_2_POLYGON" in content
    assert "CAMERA_A1_ROOM_3_POLYGON" in content
    assert "DVR_PASSWORD" not in updated


def test_validate_only_incomplete_layout_detected():
    layout = HotelLayout("A1", [(0.1, 0.9)], 0.1, False, {1: [], 2: [], 3: []})

    with pytest.raises(ValueError):
        validate_layout(layout)
