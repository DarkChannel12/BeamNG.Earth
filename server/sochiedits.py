"""Hand edits of Greater Sochi (2026-11-09): a file of fixes the server applies every time it builds the world there.

Why: BeamNG.Earth builds the world from the map data around the car each time, so a change made in the game's World
Editor is gone at the next load. The player and Claude fix a place together instead: the player shows it in the game
(the street name in the status bar, the coordinates over the minimap), the fix goes into EDITS_FILE, the server is
restarted and the place loads with it. Only inside BOUNDS (the Sochi box of docs/SOCHI.md): nothing outside it changes.

EDITS_FILE (server/sochi_edits.json; BNGE_SOCHI_EDITS names another one):

    {"roads": [
        {"name": "улица Новая Заря", "width": 9.0, "lanes": 2, "surface": "asphalt", "sidewalks": "both",
         "sidewalk_width": 3.0, "note": "why"},
        {"way": 123456789, "surface": "cobble"},
        {"name": "Курортный проспект", "near": [43.5965, 39.7350, 300], "lanes": 4}
     ],
     "areas": [
        {"ground": "cobble", "polygon": [[43.6041, 39.7311], [43.6046, 39.7318], [43.6040, 39.7322]],
         "note": "the square in front of MoreMall"}
     ]}

Roads are picked by OSM way id ("way", the road's source record) or by name ("name", exactly as the map has it, case
aside), optionally only within "near": [lat, lon, radius m] of a point. Fields (all optional):
  width           carriageway m (parking lanes included); with only "lanes", lanes x LANE_M
  lanes           driving lanes, both directions
  surface         asphalt | concrete | cobble | gravel | dirt
  sidewalks       both | left | right | none (left / right of the road's drawing direction)
  sidewalk_width  m
  drop            true: the road is not drawn at all
Areas are polygons of [lat, lon] points painted with "ground": asphalt | cobble | grass | lawn | dirt | gravel | sand,
both in the tyres' ground model and in the ground photo (a flat tint: the photo is what the ground looks like).

Applied in roads.road_specs (every road consumer sees the same specs: drawing, grading, traffic) and in the block's
ground masks and photo (surface.py). The file's fingerprint goes into the built-world caches' keys
(revisit.code_key, plancache.plans_format, roads._facts_key), so an edited file rebuilds what was built before. Read
once per process: restart the server after editing it. A broken file is logged and ignored (nothing applied).
"""
import hashlib
import json
import logging
import math
import os
import threading

import numpy as np

log = logging.getLogger("earthd.sochiedits")

HERE = os.path.dirname(os.path.abspath(__file__))
EDITS_FILE = os.environ.get("BNGE_SOCHI_EDITS") or os.path.join(HERE, "sochi_edits.json")
BOUNDS = (43.38, 39.05, 44.10, 40.45)   # (south, west, north, east): fabdem.BOUNDS, imagery_regional's Sochi box
LANE_M = 3.3
SURFACES = {"asphalt": ("paved", None), "concrete": ("paved", "concrete"), "cobble": ("cobble", None),
            "gravel": ("gravel", None), "dirt": ("dirt", None)}
SIDEWALKS = {"both": (1, -1), "left": (1,), "right": (-1,), "none": ()}
ROAD_KEYS = {"name", "way", "near", "width", "lanes", "surface", "sidewalks", "sidewalk_width", "drop", "note"}
AREA_KEYS = {"ground", "polygon", "note"}
# ground -> (surface.MATERIALS name, the photo's tint (r, g, b, opacity))
GROUNDS = {
    "asphalt": ("bngEarthGroundAsphalt", (95, 95, 94, 1.0)),
    "cobble": ("bngEarthGroundCobble", (112, 104, 96, 0.85)),
    "grass": ("bngEarthGroundGrass", (88, 112, 62, 0.75)),
    "lawn": ("bngEarthGroundLawn", (96, 128, 70, 0.75)),
    "dirt": ("bngEarthGroundDusty", (128, 106, 80, 0.75)),
    "gravel": ("bngEarthGroundLoose", (176, 170, 156, 0.7)),
    "sand": ("bngEarthGroundGolfBunker", (206, 190, 150, 0.8)),
}

_lock = threading.Lock()
_state = {}


def _parse(doc):
    """(roads, areas) from the file's JSON, checked; raises ValueError with what is wrong."""
    if not isinstance(doc, dict):
        raise ValueError("the file must be a JSON object with 'roads' and / or 'areas'")
    roads, areas = [], []
    for k, r in enumerate(doc.get("roads") or []):
        where = f"roads[{k}]"
        if not isinstance(r, dict):
            raise ValueError(f"{where}: not an object")
        bad = set(r) - ROAD_KEYS
        if bad:
            raise ValueError(f"{where}: unknown fields {sorted(bad)}")
        if ("name" in r) == ("way" in r):
            raise ValueError(f"{where}: give either 'name' or 'way'")
        e = {"name": str(r["name"]).strip().casefold() if "name" in r else None,
             "way": int(r["way"]) if "way" in r else None, "near": None}
        if "near" in r:
            la, lo, rad = (float(v) for v in r["near"])
            e["near"] = (la, lo, rad)
        if "width" in r:
            e["width"] = float(r["width"])
            if not 1.0 <= e["width"] <= 60.0:
                raise ValueError(f"{where}: width {e['width']} m is not believable")
        if "lanes" in r:
            e["lanes"] = int(r["lanes"])
            if not 1 <= e["lanes"] <= 12:
                raise ValueError(f"{where}: lanes {e['lanes']}")
        if "surface" in r:
            if r["surface"] not in SURFACES:
                raise ValueError(f"{where}: surface must be one of {sorted(SURFACES)}")
            e["surface"] = r["surface"]
        if "sidewalks" in r:
            if r["sidewalks"] not in SIDEWALKS:
                raise ValueError(f"{where}: sidewalks must be one of {sorted(SIDEWALKS)}")
            e["sidewalks"] = r["sidewalks"]
        if "sidewalk_width" in r:
            e["sidewalk_width"] = float(r["sidewalk_width"])
            if not 0.5 <= e["sidewalk_width"] <= 15.0:
                raise ValueError(f"{where}: sidewalk_width {e['sidewalk_width']} m")
        e["drop"] = bool(r.get("drop", False))
        roads.append(e)
    for k, a in enumerate(doc.get("areas") or []):
        where = f"areas[{k}]"
        if not isinstance(a, dict):
            raise ValueError(f"{where}: not an object")
        bad = set(a) - AREA_KEYS
        if bad:
            raise ValueError(f"{where}: unknown fields {sorted(bad)}")
        if a.get("ground") not in GROUNDS:
            raise ValueError(f"{where}: ground must be one of {sorted(GROUNDS)}")
        pts = np.asarray(a.get("polygon"), np.float64)
        if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 3:
            raise ValueError(f"{where}: polygon must be at least 3 [lat, lon] points")
        S, W, N, E = BOUNDS
        if not ((pts[:, 0] >= S) & (pts[:, 0] <= N) & (pts[:, 1] >= W) & (pts[:, 1] <= E)).all():
            raise ValueError(f"{where}: polygon leaves the Sochi box")
        areas.append({"ground": a["ground"], "lat": pts[:, 0].copy(), "lon": pts[:, 1].copy()})
    return roads, areas


def load(path=None):
    """{'roads': [...], 'areas': [...], 'fingerprint': str} of the edits file (read once per process; path: tests)."""
    path = path or EDITS_FILE
    with _lock:
        got = _state.get(path)
        if got is not None:
            return got
        got = {"roads": [], "areas": [], "fingerprint": ""}
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            raw = None
        except OSError as e:
            log.warning("sochi edits: %s: %s (no edits applied)", path, e)
            raw = None
        if raw is not None and raw.strip():
            try:
                roads, areas = _parse(json.loads(raw.decode("utf-8-sig")))
                got = {"roads": roads, "areas": areas,
                       "fingerprint": hashlib.sha1(raw).hexdigest()[:16] if roads or areas else ""}
                log.info("sochi edits: %d road and %d area edits from %s", len(roads), len(areas), path)
            except (ValueError, TypeError, KeyError, UnicodeDecodeError) as e:
                log.warning("sochi edits: %s is not usable: %s (no edits applied)", path, e)
        _state[path] = got
        return got


def reset():
    """Forget what was read (tests)."""
    with _lock:
        _state.clear()


def fingerprint():
    """'' without edits, else a hash of the file: in the built-world caches' keys."""
    return load()["fingerprint"]


# --- roads ---------------------------------------------------------------------------------------------------------
def _in_box(lon, lat):
    S, W, N, E = BOUNDS
    return (lat >= S) & (lat <= N) & (lon >= W) & (lon <= E)


def _way_of(feats, i):
    recs = feats.attrs.get("src_record")
    if recs is None:
        return None
    import osmtags
    return osmtags.way_id(str(recs[i]))


def _apply(sp, e):
    ch = {}
    if "lanes" in e:
        ch["lanes"] = e["lanes"]
        if "width" not in e:
            ch["width"] = round(e["lanes"] * LANE_M, 2)
    if "width" in e:
        ch["width"] = e["width"]
    if "surface" in e:
        kind, surf = SURFACES[e["surface"]]
        ch["kind"], ch["surf"] = kind, surf
    if "sidewalks" in e:
        ch["sides"] = SIDEWALKS[e["sidewalks"]]
    if "sidewalk_width" in e:
        ch["walk"] = e["sidewalk_width"]
    return sp._replace(**ch) if ch else sp


def apply_roads(feats, specs):
    """specs (roads.road_specs' list for feats) with the road edits applied; the same list object when none applies."""
    edits = load()["roads"]
    if not edits or feats is None or len(feats) == 0 or len(feats.lonlat) == 0:
        return specs
    ll = feats.lonlat
    S, W, N, E = BOUNDS
    if ll[:, 0].max() < W or ll[:, 0].min() > E or ll[:, 1].max() < S or ll[:, 1].min() > N:
        return specs
    names = feats.attrs.get("name")
    out = None
    for i, sp in enumerate(specs):
        if sp is None:
            continue
        lo = ll[feats.coord_off[feats.ring_off[feats.part_off[i]]]:feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]]
        if len(lo) == 0 or not _in_box(lo[:, 0], lo[:, 1]).any():
            continue
        nm = str(names[i]).strip().casefold() if names is not None else ""
        way = None
        for e in edits:
            if e["name"] is not None and e["name"] != nm:
                continue
            if e["way"] is not None:
                if way is None:
                    way = _way_of(feats, i) or -1
                if way != e["way"]:
                    continue
            if e["near"] is not None:
                la, lo0, rad = e["near"]
                dy = (lo[:, 1] - la) * 111320.0
                dx = (lo[:, 0] - lo0) * 111320.0 * math.cos(math.radians(la))
                if not (dx * dx + dy * dy <= rad * rad).any():
                    continue
            if out is None:
                out = list(specs)
            out[i] = None if e["drop"] else _apply(out[i], e)
            if out[i] is None:
                break
    return specs if out is None else out


# --- areas ---------------------------------------------------------------------------------------------------------
def _areas_near(lat, lon):
    areas = load()["areas"]
    if not areas:
        return []
    la0, la1 = float(np.min(lat)), float(np.max(lat))
    lo0, lo1 = float(np.min(lon)), float(np.max(lon))
    return [a for a in areas if a["lat"].max() >= la0 and a["lat"].min() <= la1
            and a["lon"].max() >= lo0 and a["lon"].min() <= lo1]


def paint_areas(idx, lat, lon):
    """The block's material index grid idx (surface.py; lat / lon its sample grids) with the area edits painted in."""
    near = _areas_near(lat, lon)
    if not near:
        return idx
    import surface
    names = [m[0] for m in surface.MATERIALS]
    out = idx
    for a in near:
        out = _paint_material(out, lat, lon, a, names.index(GROUNDS[a["ground"]][0]))
    return out


def _paint_material(idx, lat, lon, a, material):
    from PIL import Image, ImageDraw
    import surface
    h, w = idx.shape
    to_px = surface.grid_affine(lat, lon)
    c, r = to_px(a["lon"], a["lat"])
    if c.max() < 0 or c.min() > w or r.max() < 0 or r.min() > h:
        return idx
    im = Image.new("L", (w, h), 0)
    ImageDraw.Draw(im).polygon(list(zip(c.tolist(), r.tolist())), fill=255)
    out = idx.copy()
    out[np.asarray(im) > 0] = material
    return out


def photo_areas(rgb, frame, x0, y0, size):
    """The ground photo rgb over [x0, x0 + size]² of frame with the area edits tinted in (under the roads)."""
    areas = load()["areas"]
    if not areas or rgb is None:
        return rgb
    import fast
    import surface
    h, w = rgb.shape[:2]
    out = None
    for a in areas:
        polys = surface._pads_photo_px(frame, x0, y0, size, w, h, [(a["lat"], a["lon"])])
        if not polys:
            continue
        fast.require(140, "pad painting")
        r, g, b, op = GROUNDS[a["ground"]][1]
        if out is None:
            out = np.array(rgb, np.uint8, copy=True, order="C")
        fast._ef.paint_pads(out, polys, [float(r), float(g), float(b)], float(np.float32(op)))
    return rgb if out is None else out
