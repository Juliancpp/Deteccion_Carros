from __future__ import annotations

from app import config


VALID_HOTEL_EVENT_SOURCES = {"phase1_analyzer"}


def validate_hotel_event_source(active_sources: set[str] | list[str] | tuple[str, ...] | None = None) -> str:
    """Garantiza una única fuente de eventos hoteleros para el MVP."""
    configured = getattr(config, "HOTEL_EVENT_SOURCE", "phase1_analyzer")
    if configured not in VALID_HOTEL_EVENT_SOURCES:
        raise ValueError(f"HOTEL_EVENT_SOURCE invalido: {configured}")
    sources = set(active_sources or {configured})
    hotel_sources = sources.intersection(VALID_HOTEL_EVENT_SOURCES.union({"virtual_line", "passage_corridor"}))
    if len(hotel_sources) != 1 or configured not in hotel_sources:
        raise ValueError("configuracion ambigua: debe existir una sola fuente hotelera activa")
    return configured
