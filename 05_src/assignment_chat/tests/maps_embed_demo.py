#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, socket, webbrowser, argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote
from pathlib import Path

# Hard-coded secrets file path (contains OPENAI_API_KEY and GOOGLE_MAPS_EMBED_KEY)
SECRETS_FILE = Path(r"C:\Users\scliu\Desktop\deploying-ai\05_src\.secrets")

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Maps Embed Demo</title>
<style>
 body{{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:24px;color:#0f172a}}
 h1{{margin:0 0 8px;font-size:22px}} .sub{{color:#475569;margin-bottom:16px}}
 .grid{{display:grid;grid-template-columns:1fr;gap:18px}}
 .card{{border:1px solid #e2e8f0;border-radius:12px;padding:12px;box-shadow:0 1px 2px rgba(0,0,0,.06)}}
 iframe{{width:100%;height:360px;border:0;border-radius:10px}}
 .meta{{font-size:13px;color:#64748b;margin-top:8px}}
 @media (min-width:900px){{.grid{{grid-template-columns:1fr 1fr}}}}
</style>
</head>
<body>
<h1>Site Location</h1>
<div class="sub">Name: {name} · Lat {lat}, Lon {lon} · TZ {tz} · Elev {elev} m</div>
<div class="grid">
  <div class="card">
    <h3>Neighborhood (zoom 15)</h3>
    <iframe loading="lazy" referrerpolicy="no-referrer-when-downgrade" src="{url_neigh}"></iframe>
    <div class="meta">‘view’ mode centered on the site.</div>
  </div>
  <div class="card">
    <h3>City (zoom 12)</h3>
    <iframe loading="lazy" referrerpolicy="no-referrer-when-downgrade" src="{url_city}"></iframe>
    <div class="meta">Wider urban context.</div>
  </div>
  <div class="card">
    <h3>Region (zoom 8, pinned)</h3>
    <iframe loading="lazy" referrerpolicy="no-referrer-when-downgrade" src="{url_region}"></iframe>
    <div class="meta">‘place’ mode with a coordinate pin.</div>
  </div>
</div>
<p class="meta">Key source: {key_source}</p>
</body></html>
"""

def load_google_maps_key_from_file(path: Path) -> str:
    """Return value of GOOGLE_MAPS_EMBED_KEY=... from the given .secrets file."""
    if not path.exists():
        raise SystemExit(f"Secrets file not found: {path}")
    # tolerate BOM / encodings
    for enc in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            text = path.read_text(encoding=enc)
            break
        except UnicodeError:
            continue
    else:
        text = path.read_bytes().decode("utf-8", "ignore")

    key = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = [s.strip() for s in line.split("=", 1)]
        if k.upper() == "GOOGLE_MAPS_EMBED_KEY" and v:
            key = v
            break
    if not key:
        raise SystemError(f"GOOGLE_MAPS_EMBED_KEY=... not found in {path}")
    return key

def embed_view_url(lat: float, lon: float, key: str, zoom: int, maptype: str = "roadmap") -> str:
    return f"https://www.google.com/maps/embed/v1/view?key={quote(key)}&center={lat:.6f},{lon:.6f}&zoom={int(zoom)}&maptype={quote(maptype)}"

def embed_place_url(lat: float, lon: float, key: str, zoom: int) -> str:
    q = f"{lat:.6f},{lon:.6f}"
    return f"https://www.google.com/maps/embed/v1/place?key={quote(key)}&q={quote(q)}&zoom={int(zoom)}"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404); self.end_headers(); return
        urls = {
            "url_neigh":  embed_view_url(self.server.lat, self.server.lon, self.server.gkey, 15),
            "url_city":   embed_view_url(self.server.lat, self.server.lon, self.server.gkey, 12),
            "url_region": embed_place_url(self.server.lat, self.server.lon, self.server.gkey, 8),
        }
        html = HTML.format(
            name=self.server.name, lat=self.server.lat, lon=self.server.lon,
            tz=self.server.tz, elev=self.server.elev, key_source=self.server.key_src, **urls
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

def free_port(start=8000) -> int:
    for p in range(start, start+50):
        try:
            s = socket.socket(); s.bind(("127.0.0.1", p)); s.close(); return p
        except OSError:
            continue
    raise RuntimeError("No free port available")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="CHICAGO_IL_USA TMY2-94846")
    ap.add_argument("--lat", type=float, default=41.78)
    ap.add_argument("--lon", type=float, default=-87.75)
    ap.add_argument("--tz", default="-6.0")
    ap.add_argument("--elev", default="190.0")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    gkey = load_google_maps_key_from_file(SECRETS_FILE)
    key_src = str(SECRETS_FILE)

    port = args.port or free_port(8000)
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    httpd.gkey = gkey
    httpd.key_src = key_src
    httpd.lat, httpd.lon = args.lat, args.lon
    httpd.tz,  httpd.elev = args.tz, args.elev
    httpd.name = args.name

    url = f"http://127.0.0.1:{port}/"
    print(f"Serving {url}  (key from {key_src})  — press Ctrl+C to stop")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    httpd.serve_forever()

if __name__ == "__main__":
    main()