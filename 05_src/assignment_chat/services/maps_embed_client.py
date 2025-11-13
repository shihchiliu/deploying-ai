# -*- coding: utf-8 -*-
"""
maps_embed_client.py
- Loads GOOGLE_MAPS_EMBED_KEY from a local `.secrete` file at project root.
- Builds Google Maps Embed API URLs for different zoom levels / modes.
- Pure stdlib; no external deps.
"""
from __future__ import annotations
from pathlib import Path
from urllib.parse import quote


# Hardcoded path to your secrets file at project root
DEFAULT_SECRETS_PATH = Path(__file__).resolve().parent.parent / ".secrete"


def load_secrets(path: Path | None = None) -> dict:
    """
    Read simple key=value lines from .secrete, ignoring blanks and comments.
    """
    secrets = {}
    p = path or DEFAULT_SECRETS_PATH
    if not p.exists():
        raise FileNotFoundError(f"Secrets file not found: {p}")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            secrets[k.strip()] = v.strip()
    return secrets


def get_google_maps_key(path: Path | None = None) -> str:
    secrets = load_secrets(path)
    key = secrets.get("GOOGLE_MAPS_EMBED_KEY")
    if not key:
        raise RuntimeError("GOOGLE_MAPS_EMBED_KEY not found in .secrete")
    return key


def embed_view_url(lat: float, lon: float, key: str, zoom: int = 13, maptype: str = "roadmap") -> str:
    """
    Returns a Google Maps Embed 'view' URL centered on (lat, lon).
    Docs: https://developers.google.com/maps/documentation/embed/get-started
    """
    return (
        "https://www.google.com/maps/embed/v1/view"
        f"?key={quote(key)}&center={lat:.6f},{lon:.6f}&zoom={int(zoom)}&maptype={quote(maptype)}"
    )


def embed_place_url(lat: float, lon: float, key: str, zoom: int = 12) -> str:
    """
    Returns a Google Maps Embed 'place' URL using a lat,lon query (pins the spot).
    """
    q = f"{lat:.6f},{lon:.6f}"
    return f"https://www.google.com/maps/embed/v1/place?key={quote(key)}&q={quote(q)}&zoom={int(zoom)}"


def build_three_scales(lat: float, lon: float, key: str) -> dict:
    """
    Convenience helper: neighborhood/city/region iframes.
    """
    return {
        "neighborhood": embed_view_url(lat, lon, key, zoom=15, maptype="roadmap"),
        "city":         embed_view_url(lat, lon, key, zoom=12, maptype="roadmap"),
        "region":       embed_place_url(lat, lon, key, zoom=8),
    }