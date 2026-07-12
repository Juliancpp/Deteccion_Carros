"""
Configuracion sensible de DVR y camaras RTSP.

Este modulo no conecta camaras ni imprime URLs con credenciales.
La validacion estricta ocurre solo al llamar load_settings().
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = BASE_DIR / ".env"
CAMERA_PREFIXES = ("CAMERA_A1", "CAMERA_A5")
PLACEHOLDER_VALUES = {
    "CAMBIAR_LOCALMENTE",
    "PON_AQUI_LA_CONTRASENA_TEMPORAL",
    "PASSWORD",
    "CHANGE_ME",
}


class SettingsError(ValueError):
    """Error de configuracion seguro: no incluye secretos."""


@dataclass(frozen=True)
class DVRSettings:
    host: str
    rtsp_port: int
    username: str
    password: str


@dataclass(frozen=True)
class CameraLineSettings:
    enabled: bool
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    invert_direction: bool
    event_directions: str


@dataclass(frozen=True)
class CameraPassageSettings:
    enabled: bool
    points: tuple[tuple[float, float], ...]
    half_width: float
    event: str
    direction: str
    trigger_progress: float


@dataclass(frozen=True)
class CameraCorridorSettings:
    enabled: bool
    points: tuple[tuple[float, float], ...]
    half_width: float
    invert_direction: bool


@dataclass(frozen=True)
class CameraGateSettings:
    start: tuple[float, float]
    end: tuple[float, float]
    invert_direction: bool


@dataclass(frozen=True)
class RoomZoneSettings:
    room_number: int
    polygon: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class CameraSettings:
    camera_id: str
    name: str
    enabled: bool
    channel: int
    target_fps: float
    line: CameraLineSettings | None = None
    corridor: CameraCorridorSettings | None = None
    passage: CameraPassageSettings | None = None
    gate: CameraGateSettings | None = None
    rooms: tuple[RoomZoneSettings, ...] = ()


@dataclass(frozen=True)
class AppSettings:
    dvr: DVRSettings
    cameras: list[CameraSettings]


def load_settings(
    env_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    override_env: bool = False,
) -> AppSettings:
    """
    Carga y valida configuracion DVR/camaras desde .env y variables de entorno.

    Args:
        env_path: Ruta opcional al archivo .env.
        environ: Mapeo opcional para pruebas. Si se provee, no se lee .env.
        override_env: Si es True, .env tiene prioridad sobre variables exportadas.

    Raises:
        SettingsError: Si falta o es invalida alguna variable requerida.
    """
    if environ is None:
        load_dotenv(dotenv_path=env_path or DEFAULT_ENV_PATH, override=override_env)
        source = os.environ
    else:
        source = environ

    dvr = DVRSettings(
        host=_required_str(source, "DVR_HOST"),
        rtsp_port=_required_int(source, "DVR_RTSP_PORT", min_value=1, max_value=65535),
        username=_required_str(source, "DVR_USERNAME"),
        password=_required_secret(source, "DVR_PASSWORD"),
    )

    cameras = [_load_camera(source, prefix) for prefix in CAMERA_PREFIXES]
    _validate_unique_camera_ids(cameras)

    return AppSettings(dvr=dvr, cameras=cameras)


def build_rtsp_url(dvr_settings: DVRSettings, camera_settings: CameraSettings) -> str:
    """Construye una URL RTSP interna con usuario/password escapados."""
    username = quote(dvr_settings.username, safe="")
    password = quote(dvr_settings.password, safe="")
    return (
        f"rtsp://{username}:{password}@"
        f"{dvr_settings.host}:{dvr_settings.rtsp_port}"
        f"/Streaming/Channels/{camera_settings.channel}"
    )


def sanitize_rtsp_url(url: str, hide_username: bool = False) -> str:
    """Devuelve una URL RTSP apta para logs, sin password visible."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url

    userinfo, hostinfo = parts.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0] if userinfo else ""
    safe_username = "***" if hide_username or not username else quote(unquote(username), safe="")
    sanitized_netloc = f"{safe_username}:***@{hostinfo}"
    return urlunsplit((parts.scheme, sanitized_netloc, parts.path, parts.query, parts.fragment))


def sanitize_sensitive_text(
    text: object,
    dvr_settings: DVRSettings,
    *,
    camera_settings: CameraSettings | None = None,
    hide_username: bool = False,
) -> str:
    """Sanitiza texto arbitrario que podria contener URL o credenciales RTSP."""
    message = str(text)
    candidates = set()
    candidates.add(dvr_settings.password)
    candidates.add(quote(dvr_settings.password, safe=""))
    candidates.add(dvr_settings.username)
    candidates.add(quote(dvr_settings.username, safe=""))

    if camera_settings is not None:
        url = build_rtsp_url(dvr_settings, camera_settings)
        candidates.add(url)
        message = message.replace(url, sanitize_rtsp_url(url, hide_username=hide_username))

    for candidate in sorted(candidates, key=len, reverse=True):
        if not candidate:
            continue
        replacement = "***" if hide_username or candidate != dvr_settings.username else dvr_settings.username
        if candidate in {dvr_settings.password, quote(dvr_settings.password, safe="")}:
            replacement = "***"
        message = message.replace(candidate, replacement)

    return message


def _load_camera(source: Mapping[str, str], prefix: str) -> CameraSettings:
    return CameraSettings(
        camera_id=_required_str(source, f"{prefix}_ID"),
        name=_required_str(source, f"{prefix}_NAME"),
        enabled=_required_bool(source, f"{prefix}_ENABLED"),
        channel=_required_int(source, f"{prefix}_CHANNEL", min_value=1),
        target_fps=_required_float(source, f"{prefix}_TARGET_FPS", min_value=0),
        line=_load_camera_line(source, prefix),
        corridor=_load_camera_corridor(source, prefix),
        passage=_load_camera_passage(source, prefix),
        gate=_load_camera_gate(source, prefix),
        rooms=_load_room_zones(source, prefix),
    )


def _load_camera_line(source: Mapping[str, str], prefix: str) -> CameraLineSettings:
    line = CameraLineSettings(
        enabled=_optional_bool(source, f"{prefix}_LINE_ENABLED", default=True),
        start_x=_optional_normalized_float(source, f"{prefix}_LINE_START_X", default=0.20),
        start_y=_optional_normalized_float(source, f"{prefix}_LINE_START_Y", default=0.55),
        end_x=_optional_normalized_float(source, f"{prefix}_LINE_END_X", default=0.80),
        end_y=_optional_normalized_float(source, f"{prefix}_LINE_END_Y", default=0.55),
        invert_direction=_optional_bool(source, f"{prefix}_LINE_INVERT_DIRECTION", default=False),
        event_directions=_optional_event_directions(source, f"{prefix}_EVENT_DIRECTIONS", default="both"),
    )
    if line.start_x == line.end_x and line.start_y == line.end_y:
        raise SettingsError(f"Configuracion invalida: {prefix}_LINE_START y {prefix}_LINE_END no pueden ser iguales")
    return line


def _load_camera_passage(source: Mapping[str, str], prefix: str) -> CameraPassageSettings:
    default_points = _default_passage_points(prefix)
    passage = CameraPassageSettings(
        enabled=_optional_bool(source, f"{prefix}_PASSAGE_ENABLED", default=True),
        points=_optional_points(source, f"{prefix}_CORRIDOR_POINTS", default=default_points),
        half_width=_optional_normalized_float(
            source,
            f"{prefix}_CORRIDOR_HALF_WIDTH",
            default=0.08,
            allow_zero=False,
        ),
        event=_optional_event_name(source, f"{prefix}_PASSAGE_EVENT", default=_default_passage_event(prefix)),
        direction=_optional_passage_direction(source, f"{prefix}_PASSAGE_DIRECTION", default="forward"),
        trigger_progress=_optional_normalized_float(
            source,
            f"{prefix}_TRIGGER_PROGRESS",
            default=_default_trigger_progress(prefix),
            allow_zero=True,
        ),
    )
    if len(passage.points) < 2:
        raise SettingsError(f"Configuracion invalida: {prefix}_CORRIDOR_POINTS requiere al menos dos puntos")
    return passage


def _load_camera_corridor(source: Mapping[str, str], prefix: str) -> CameraCorridorSettings:
    default_points = _default_passage_points(prefix)
    points_key = f"{prefix}_CORRIDOR_POINTS"
    return CameraCorridorSettings(
        enabled=_optional_bool(source, f"{prefix}_CORRIDOR_ENABLED", default=True),
        points=_optional_points(source, points_key, default=default_points),
        half_width=_optional_normalized_float(
            source,
            f"{prefix}_CORRIDOR_HALF_WIDTH",
            default=0.10,
            allow_zero=False,
        ),
        invert_direction=_optional_bool(source, f"{prefix}_CORRIDOR_INVERT_DIRECTION", default=False),
    )


def _load_camera_gate(source: Mapping[str, str], prefix: str) -> CameraGateSettings:
    defaults = _default_gate(prefix)
    start_x = _optional_normalized_float(source, f"{prefix}_GATE_START_X", default=defaults[0][0])
    start_y = _optional_normalized_float(source, f"{prefix}_GATE_START_Y", default=defaults[0][1])
    end_x = _optional_normalized_float(source, f"{prefix}_GATE_END_X", default=defaults[1][0])
    end_y = _optional_normalized_float(source, f"{prefix}_GATE_END_Y", default=defaults[1][1])
    if start_x == end_x and start_y == end_y:
        raise SettingsError(f"Configuracion invalida: {prefix}_GATE_START y {prefix}_GATE_END no pueden ser iguales")
    return CameraGateSettings(
        start=(start_x, start_y),
        end=(end_x, end_y),
        invert_direction=_optional_bool(source, f"{prefix}_GATE_INVERT_DIRECTION", default=False),
    )


def _load_room_zones(source: Mapping[str, str], prefix: str) -> tuple[RoomZoneSettings, ...]:
    rooms = _room_numbers(prefix)
    zones = []
    for room_number in rooms:
        key = f"{prefix}_ROOM_{room_number}_POLYGON"
        raw_value = str(source.get(key, "")).strip()
        if not raw_value:
            continue
        polygon = _parse_polygon(raw_value, key)
        zones.append(RoomZoneSettings(room_number=room_number, polygon=polygon))
    return tuple(zones)


def _required_str(source: Mapping[str, str], key: str) -> str:
    value = source.get(key, "").strip()
    if not value:
        raise SettingsError(f"Configuracion invalida: falta {key} en .env")
    return value


def _required_secret(source: Mapping[str, str], key: str) -> str:
    value = _required_str(source, key)
    if value.upper() in PLACEHOLDER_VALUES:
        raise SettingsError(f"Configuracion invalida: {key} contiene un valor de ejemplo")
    return value


def _required_int(
    source: Mapping[str, str],
    key: str,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    raw_value = _required_str(source, key)
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SettingsError(f"Configuracion invalida: {key} debe ser un entero") from exc

    if min_value is not None and value < min_value:
        raise SettingsError(f"Configuracion invalida: {key} debe ser mayor o igual que {min_value}")
    if max_value is not None and value > max_value:
        raise SettingsError(f"Configuracion invalida: {key} debe ser menor o igual que {max_value}")
    return value


def _required_float(source: Mapping[str, str], key: str, min_value: float | None = None) -> float:
    raw_value = _required_str(source, key)
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise SettingsError(f"Configuracion invalida: {key} debe ser numerico") from exc

    if min_value is not None and value <= min_value:
        raise SettingsError(f"Configuracion invalida: {key} debe ser mayor que {min_value}")
    return value


def _required_bool(source: Mapping[str, str], key: str) -> bool:
    raw_value = _required_str(source, key).lower()
    if raw_value in {"true", "1"}:
        return True
    if raw_value in {"false", "0"}:
        return False
    raise SettingsError(f"Configuracion invalida: {key} debe ser true, false, 1 o 0")


def _optional_bool(source: Mapping[str, str], key: str, default: bool) -> bool:
    if key not in source or str(source.get(key, "")).strip() == "":
        return default
    return _required_bool(source, key)


def _optional_normalized_float(
    source: Mapping[str, str],
    key: str,
    default: float,
    *,
    allow_zero: bool = True,
) -> float:
    if key not in source or str(source.get(key, "")).strip() == "":
        return default
    value = _required_float(source, key, min_value=-1.0)
    if value < 0.0 or value > 1.0:
        raise SettingsError(f"Configuracion invalida: {key} debe estar entre 0.0 y 1.0")
    if not allow_zero and value <= 0.0:
        raise SettingsError(f"Configuracion invalida: {key} debe ser mayor que 0.0")
    return value


def _optional_event_directions(source: Mapping[str, str], key: str, default: str) -> str:
    value = str(source.get(key, default)).strip().lower()
    if value not in {"both", "entrada", "salida"}:
        raise SettingsError(f"Configuracion invalida: {key} debe ser both, entrada o salida")
    return value


def _optional_event_name(source: Mapping[str, str], key: str, default: str) -> str:
    value = str(source.get(key, default)).strip().upper()
    if value not in {"ENTRADA", "SALIDA"}:
        raise SettingsError(f"Configuracion invalida: {key} debe ser ENTRADA o SALIDA")
    return value


def _optional_passage_direction(source: Mapping[str, str], key: str, default: str) -> str:
    value = str(source.get(key, default)).strip().lower()
    if value not in {"forward", "backward"}:
        raise SettingsError(f"Configuracion invalida: {key} debe ser forward o backward")
    return value


def _optional_points(
    source: Mapping[str, str],
    key: str,
    default: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    raw_value = str(source.get(key, "")).strip()
    raw_value = raw_value.strip('"').strip("'")
    if not raw_value:
        return default
    points: list[tuple[float, float]] = []
    for item in raw_value.split(";"):
        parts = [part.strip() for part in item.split(",")]
        if len(parts) != 2:
            raise SettingsError(f"Configuracion invalida: {key} debe usar pares x,y separados por ;")
        try:
            x = float(parts[0])
            y = float(parts[1])
        except ValueError as exc:
            raise SettingsError(f"Configuracion invalida: {key} contiene coordenadas no numericas") from exc
        if x < 0.0 or x > 1.0 or y < 0.0 or y > 1.0:
            raise SettingsError(f"Configuracion invalida: {key} debe contener coordenadas entre 0.0 y 1.0")
        points.append((x, y))
    if len(points) < 2:
        raise SettingsError(f"Configuracion invalida: {key} requiere al menos dos puntos")
    return tuple(points)


def _parse_polygon(raw_value: str, key: str) -> tuple[tuple[float, float], ...]:
    polygon = _optional_points({key: raw_value}, key, default=())
    if len(polygon) < 3:
        raise SettingsError(f"Configuracion invalida: {key} requiere al menos tres puntos")
    return polygon


def _default_passage_points(prefix: str) -> tuple[tuple[float, float], ...]:
    if prefix == "CAMERA_A5":
        return ((0.06, 0.98), (0.14, 0.86), (0.22, 0.74), (0.29, 0.64))
    return ((0.27, 0.98), (0.43, 0.83), (0.59, 0.69), (0.70, 0.58))


def _default_passage_event(prefix: str) -> str:
    return "SALIDA" if prefix == "CAMERA_A5" else "ENTRADA"


def _default_trigger_progress(prefix: str) -> float:
    return 0.55 if prefix == "CAMERA_A5" else 0.15


def _default_gate(prefix: str) -> tuple[tuple[float, float], tuple[float, float]]:
    if prefix == "CAMERA_A5":
        return ((0.10, 0.84), (0.36, 0.62))
    return ((0.28, 0.92), (0.52, 0.72))


def _room_numbers(prefix: str) -> tuple[int, ...]:
    return (4, 5, 6) if prefix == "CAMERA_A5" else (1, 2, 3)


def _validate_unique_camera_ids(cameras: list[CameraSettings]) -> None:
    seen: set[str] = set()
    for camera in cameras:
        if camera.camera_id in seen:
            raise SettingsError("Configuracion invalida: IDs de camaras duplicados")
        seen.add(camera.camera_id)
