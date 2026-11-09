"""Greater Sochi's bare-earth terrain: FABDEM v1.2 (Forest And Buildings removed Copernicus DEM, 30 m), 2026-11-09.

Why: outside the lidar areas Mapterhorn is Copernicus GLO-30, a surface model (roofs and tree tops), and
dsmground.ground leaves hilly ground alone (relief over RELIEF_HILLS_M across 600 m: real ridges must stay). Central
Sochi is hilly almost everywhere, so its blocks and its wooded slopes stayed in the terrain: mounds beside the roads
and roads graded into them. FABDEM is the same Copernicus DEM with forests and buildings taken out by the University
of Bristol (Hawker et al. 2022, doi:10.1088/1748-9326/ac4d4f), heights in metres above EGM2008 like Mapterhorn's.

Only inside BOUNDS (the Sochi box of docs/SOCHI.md; the same box as imagery_regional's Sochi sources): the rest of the
world is unchanged. dem.Dem puts it into the z12 tiles there, in place of the ground filter, blended into that ground
over FEATHER_M inside the box's edge (and over COAST_FEATHER_M where FABDEM has no data: the sea); the deeper zooms are
that z12 tile upsampled, as everywhere outside lidar.

Data: Fondazione LINKS' mirror of the Bristol release as Cloud-Optimised GeoTIFFs, one per 1 x 1 degree
(https://huggingface.co/datasets/links-ads/fabdem-v12: pixel values unchanged, float32, nodata -9999, EPSG:4326),
named by the south-west corner (N43E039 covers 43-44 N, 39-40 E) inside 10-degree folders. The bucket answers with a
redirect to a signed CDN address, which netfetch does not follow, so a file is downloaded whole once (~11 MB; four
cover the box) through Fetcher.session and urllib (as gravity.py does), kept in <cache>/fabdem/<RELEASE>/ and read
locally from then on (offline too).

Licence: FABDEM v1.2 is under the Non-Commercial Government Licence v2.0 (non-commercial use; BeamNG.Earth is free).

    f = Fabdem(cache_dir, fetcher)
    got = f.tile(z, x, y)       # None, or (heights float32 [512, 512], valid bool [512, 512]) on dem.py's grid
"""
import http.client
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request

import numpy as np

import cog as cogmod
import netfetch

log = logging.getLogger("earthd.fabdem")

ON = os.environ.get("BNE_FABDEM", "1") != "0"
RELEASE = "V1-2"
BASE_URL = "https://huggingface.co/buckets/links-ads/fabdem/resolve/tiles"
HOST = "huggingface.co"
BOUNDS = (43.38, 39.05, 44.10, 40.45)   # (south, west, north, east): Lazarevskoye to Adler and Krasnaya Polyana
FEATHER_M = 1500.0      # m inside the box's edge over which FABDEM fades into the filtered Mapterhorn ground
COAST_FEATHER_M = 60.0  # m over which it fades where FABDEM has no data (the sea, a void)
NODATA_LIMIT = -1000.0
TILE = 512              # dem.py's tile size
MIRROR_TAG = "fab12min"     # dem.py's decoded-mirror folder suffix for tiles made from FABDEM (raise it when they change)
MIN_BYTES = 1 << 20     # a whole file is at least this (an error page is not kept)
TIMEOUT_S = 60.0


def file_name(lat0, lon0):
    """'N43E039': the 1-degree file whose south-west corner is (lat0, lon0) (integers)."""
    return f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"


def file_url(lat0, lon0):
    """The file's address: <BASE_URL>/<10-degree folder>/<name>_FABDEM_V1-2.tif."""
    fl, fo = 10 * math.floor(lat0 / 10), 10 * math.floor(lon0 / 10)
    folder = f"{file_name(fl, fo)}-{file_name(fl + 10, fo + 10)}_FABDEM_{RELEASE}"
    return f"{BASE_URL}/{folder}/{file_name(lat0, lon0)}_FABDEM_{RELEASE}.tif"


def tile_bounds(z, x, y):
    """(south, west, north, east) of Web-Mercator tile (z, x, y)."""
    n = 2 ** z
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))     # noqa: E731
    return lat(y + 1), x / n * 360.0 - 180.0, lat(y), (x + 1) / n * 360.0 - 180.0


def covers(z, x, y, bounds=BOUNDS):
    """Whether tile (z, x, y) touches the box."""
    s, w, n, e = tile_bounds(z, x, y)
    S, W, N, E = bounds
    return s < N and n > S and w < E and e > W


def tile_lat_lon(z, x, y, size=TILE):
    """Pixel-centre lat / lon grids [size, size] of tile (z, x, y) on dem.py's grid (row 0 north)."""
    n = 2 ** z
    i = (np.arange(size) + 0.5) / size
    lon = (x + i) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + i) / n))))
    return np.broadcast_to(lat[:, None], (size, size)), np.broadcast_to(lon[None, :], (size, size))


def edge_weight(lat, lon, bounds=BOUNDS, feather_m=FEATHER_M):
    """[0, 1]: 0 outside the box and on its edge, rising linearly to 1 at feather_m inside it. Pure."""
    S, W, N, E = bounds
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    my = 111320.0
    mx = 111320.0 * np.cos(np.radians(lat))
    d = np.minimum.reduce([(lat - S) * my, (N - lat) * my, (lon - W) * mx, (E - lon) * mx])
    return np.clip(d / feather_m, 0.0, 1.0).astype(np.float32)


class LocalRange:
    """A whole file on disk as cog.Cog's remote (read(start, end, kind), size)."""

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        self._lock = threading.Lock()
        self._f = open(path, "rb")

    def read(self, start, end, kind="demand"):
        with self._lock:
            self._f.seek(start)
            return self._f.read(max(0, min(end, self.size) - start))

    def blocks(self, i0, i1, kind="demand"):
        return {}


class Fabdem:
    def __init__(self, cache_dir, fetcher, bounds=BOUNDS):
        self.dir = os.path.join(cache_dir, RELEASE)
        self.fetcher = fetcher
        self.bounds = bounds
        self._cogs = {}                     # name -> Cog, or None (no such file: the open sea)
        self._locks = {}
        self._lock = threading.Lock()
        self._tiles = cogmod.TileCache(64 * 1024 * 1024)
        self.stats = {"tiles": 0, "downloads": 0, "errors": 0}

    def covers(self, z, x, y):
        return covers(z, x, y, self.bounds)

    def _path(self, name):
        return os.path.join(self.dir, name + ".tif")

    def _download(self, name, url):
        """The whole file to <dir>/<name>.tif (a .part first). Returns False for a missing file (404 / 410: a 1-degree
        square with no land), raises netfetch.FetchError / OSError otherwise."""
        path = self._path(name)
        miss = path + ".404"
        if os.path.exists(miss):
            return False
        os.makedirs(self.dir, exist_ok=True)
        part = f"{path}.{os.getpid()}.part"
        tls = netfetch.windows_ssl_context()
        t0 = time.monotonic()
        try:
            with self.fetcher.session(url) as s:
                req = urllib.request.Request(url, headers=s.headers)
                try:
                    resp = urllib.request.urlopen(req, timeout=TIMEOUT_S, context=tls)
                except urllib.error.HTTPError as e:
                    if e.code in (404, 410):
                        s.status = e.code
                        with open(miss, "wb"):
                            pass
                        return False
                    raise
                with resp, open(part, "wb") as f:
                    s.status = resp.status
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        s.nbytes += len(chunk)
            if os.path.getsize(part) < MIN_BYTES:
                raise http.client.HTTPException(f"FABDEM {name}: short file ({os.path.getsize(part)} bytes)")
            with open(part, "rb") as f:
                if f.read(4) not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
                    raise http.client.HTTPException(f"FABDEM {name}: not a TIFF")
            os.replace(part, path)
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
        with self._lock:
            self.stats["downloads"] += 1
        log.info("fabdem: %s downloaded (%.1f MB, %.1f s)", name, os.path.getsize(path) / 1e6, time.monotonic() - t0)
        return True

    def _cog(self, lat0, lon0):
        name = file_name(lat0, lon0)
        with self._lock:
            if name in self._cogs:
                return self._cogs[name]
            lk = self._locks.setdefault(name, threading.Lock())
        with lk:                            # (one download per file; the others wait for it)
            with self._lock:
                if name in self._cogs:
                    return self._cogs[name]
            path = self._path(name)
            if not os.path.exists(path) and not self._download(name, file_url(lat0, lon0)):
                c = None
            else:
                try:
                    c = cogmod.Cog(LocalRange(path), "demand", tiles=self._tiles)
                except cogmod.CogError:
                    os.remove(path)         # (a damaged file: fetched again next time)
                    raise
                if c.epsg not in (None, 4326) or c.sx is None:
                    raise cogmod.CogError(f"FABDEM {name}: unexpected georeferencing (EPSG {c.epsg})")
            with self._lock:
                self._cogs[name] = c
            return c

    def tile(self, z, x, y, size=TILE):
        """Heights and their validity on dem.py's tile (z, x, y), or None where FABDEM has nothing there (outside the
        box, the sea). Network failures raise netfetch.FetchError / OSError (the caller keeps its own heights)."""
        if not self.covers(z, x, y):
            return None
        lat, lon = tile_lat_lon(z, x, y, size)
        out = np.zeros((size, size), np.float32)
        ok = np.zeros((size, size), bool)
        la0 = np.floor(lat).astype(np.int64)
        lo0 = np.floor(lon).astype(np.int64)
        for a in np.unique(la0):
            for o in np.unique(lo0):
                m = (la0 == a) & (lo0 == o)
                if not m.any():
                    continue
                c = self._cog(int(a), int(o))
                if c is None:
                    continue
                col, row = c.pixel_of(lon[m], lat[m])
                col, row = col - 0.5, row - 0.5         # pixel centres at integers
                c0 = max(0, int(math.floor(col.min())) - 1)
                r0 = max(0, int(math.floor(row.min())) - 1)
                c1 = min(c.width, int(math.floor(col.max())) + 3)
                r1 = min(c.height, int(math.floor(row.max())) + 3)
                win = c.read_window(c0, r0, c1 - c0, r1 - r0).astype(np.float32)
                bad = ~np.isfinite(win) | (win < NODATA_LIMIT)
                if c.nodata is not None:
                    bad |= win == np.float32(c.nodata)
                if bad.all():
                    continue
                u = np.clip(col - c0, 0, win.shape[1] - 1.001)
                v = np.clip(row - r0, 0, win.shape[0] - 1.001)
                u0, v0 = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
                fu, fv = (u - u0).astype(np.float32), (v - v0).astype(np.float32)
                a00, a01 = win[v0, u0], win[v0, u0 + 1]
                a10, a11 = win[v0 + 1, u0], win[v0 + 1, u0 + 1]
                good = ~(bad[v0, u0] | bad[v0, u0 + 1] | bad[v0 + 1, u0] | bad[v0 + 1, u0 + 1])
                h = (a00 * (1 - fu) + a01 * fu) * (1 - fv) + (a10 * (1 - fu) + a11 * fu) * fv
                out[m] = np.where(good, h, 0.0)
                ok[m] = good
        with self._lock:
            self.stats["tiles"] += 1
        if not ok.any():
            return None
        return out, ok
