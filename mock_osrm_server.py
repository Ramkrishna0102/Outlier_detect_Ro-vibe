#!/usr/bin/env python3
"""A tiny FAKE OSRM server (standard library only) so you can test the OSRM part of
outlier_qa.py without a real OSRM host.

  /table/v1/driving/...    road km = great-circle x 1.3 ; null if a point is in the water box
  /nearest/v1/driving/...  snap = 5 m normally, 3000 m inside the water box (river / sea)

    python mock_osrm_server.py --port 5000 --water 19.073 19.077 72.883 72.887
    python outlier_qa.py --input sample_input.xlsx --osrm-url http://127.0.0.1:5000/india --check-unroutable

NOT real road data - only for checking that the script's OSRM code path works.
"""
import argparse
import json
import math
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

WATER = None  # (lat_min, lat_max, lon_min, lon_max)


def hav_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def in_water(p):
    return WATER is not None and WATER[0] <= p[0] <= WATER[1] and WATER[2] <= p[1] <= WATER[3]


def parse_coords(s):  # "lon,lat;lon,lat" -> [(lat, lon)]
    return [(float(x.split(",")[1]), float(x.split(",")[0])) for x in s.split(";")]


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if "/table/v1/driving/" in u.path:
            pts = parse_coords(u.path.split("/table/v1/driving/")[1])
            src = [pts[int(i)] for i in q["sources"][0].split(";")]
            dst = [pts[int(i)] for i in q["destinations"][0].split(";")]
            rows = [[None if in_water(a) or in_water(b) else (0.0 if a == b else hav_m(a, b) * 1.3)
                     for b in dst] for a in src]
            return self._send(200, {"code": "Ok", "distances": rows})
        if "/nearest/v1/driving/" in u.path:
            p = parse_coords(u.path.split("/nearest/v1/driving/")[1])[0]
            return self._send(200, {"code": "Ok", "waypoints": [{"distance": 3000.0 if in_water(p) else 5.0}]})
        self._send(400, {"code": "InvalidUrl"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--water", nargs=4, type=float, metavar=("LAT_MIN", "LAT_MAX", "LON_MIN", "LON_MAX"))
    a = ap.parse_args()
    WATER = tuple(a.water) if a.water else None
    print(f"mock OSRM on http://127.0.0.1:{a.port}  water box={WATER}  (Ctrl+C to stop)")
    HTTPServer(("127.0.0.1", a.port), H).serve_forever()
