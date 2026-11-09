"""Keyed (opt-in) regional imagery (imagery_regional.KEYED: Esri World Imagery and MapTiler Satellite over Greater
Sochi with the player's own keys): off without a key (the chain, credits and every source as before), only inside its
box with it, the key never in a source's URL, a log line, an error message or a cache path (a header for Esri; a URL
parameter added at the request for MapTiler, cut out by netfetch.redact), never pre-downloaded by a saved area, and
MapTiler capped per day (its free plan's quota). No network: each case imports the modules in a fresh process with
its own environment, or uses a fake fetcher."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import imagery_regional  # noqa: E402

ESRI_KEY = "test-esri-key-0123456789abcdef"
MT_KEY = "testMapTilerKey987"
ESRI_VAR = imagery_regional.KEYED["esri_sochi"][5]
MT_VAR = imagery_regional.KEYED["maptiler_sochi"][5]


def probe(environ):
    """The keyed sources as a fresh process sees them with these keys set (and the others unset)."""
    code = r"""
import json
import imagery, imagery_regional, netfetch, credits
def t(lat, lon):
    x, y = imagery.mercator_px(lat, lon, 17)
    return int(x) // imagery.TILE, int(y) // imagery.TILE
out = {"chain": list(imagery.DEFAULT_CHAIN), "regional": list(imagery_regional.REGIONAL),
       "attribution": imagery.ATTRIBUTION, "sources": {}}
for name in ("esri_sochi", "maptiler_sochi"):
    s = imagery.SOURCES.get(name)
    if s is None:
        continue
    host = netfetch.host_of(s.url)
    p = netfetch.POLICIES.get(host)
    out["sources"][name] = dict(
        url=s.url, headers=dict(s.headers), query=[list(q) for q in s.query], host=host,
        offline_ok=p.offline_ok, optional=p.optional, daily_hard=p.daily_hard, role=netfetch.role_of(host),
        short=credits.SHORT.get("img:" + name),
        inside=bool(imagery.in_bounds(s, 17, *t(43.5785, 39.7200))),       # Sochi sea port
        outside=bool(imagery.in_bounds(s, 17, *t(55.7539, 37.6208))),      # Moscow
        abkhazia=bool(imagery.in_bounds(s, 17, *t(43.20, 40.30))))         # south of the Psou border
print(json.dumps(out))
"""
    env = {k: v for k, v in os.environ.items() if k not in (ESRI_VAR, MT_VAR)}
    env.update(environ)
    r = subprocess.run([sys.executable, "-c", code], cwd=str(SERVER), env=env, capture_output=True, text=True,
                       timeout=300)
    if r.returncode:
        raise AssertionError(r.stderr[-3000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


class KeyedImageryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.off = probe({})
        cls.blank = probe({ESRI_VAR: "   ", MT_VAR: ""})
        cls.mt = probe({MT_VAR: MT_KEY})
        cls.both = probe({ESRI_VAR: ESRI_KEY, MT_VAR: MT_KEY})

    def test_off_without_key(self):
        for got in (self.off, self.blank):
            self.assertEqual(got["sources"], {})
            self.assertFalse({"esri_sochi", "maptiler_sochi"} & set(got["regional"] + got["chain"]))
            self.assertNotIn("Esri", got["attribution"])
            self.assertNotIn("MapTiler", got["attribution"])

    def test_key_only_adds_the_source(self):
        off = self.off["chain"]
        self.assertEqual([n for n in self.mt["chain"] if n != "maptiler_sochi"], off)
        self.assertEqual(set(self.mt["sources"]), {"maptiler_sochi"})
        on = self.both["chain"]
        self.assertEqual([n for n in on if n not in ("esri_sochi", "maptiler_sochi")], off)
        self.assertLess(on.index("esri_sochi"), on.index("maptiler_sochi"))
        self.assertLess(on.index("maptiler_sochi"), on.index("eox2023"))

    def test_keys_never_in_the_source_url(self):
        esri, mt = self.both["sources"]["esri_sochi"], self.both["sources"]["maptiler_sochi"]
        for s in (esri, mt):
            self.assertNotIn(ESRI_KEY, s["url"])
            self.assertNotIn(MT_KEY, s["url"])
        self.assertEqual(esri["headers"], {"X-Esri-Authorization": "Bearer " + ESRI_KEY})
        self.assertEqual(esri["query"], [])
        self.assertEqual(mt["headers"], {})
        self.assertEqual(mt["query"], [["key", MT_KEY]])
        self.assertNotIn(ESRI_KEY, self.both["attribution"])
        self.assertNotIn(MT_KEY, self.both["attribution"])

    def test_only_inside_sochi(self):
        for s in self.both["sources"].values():
            self.assertTrue(s["inside"])
            self.assertFalse(s["outside"])
            self.assertFalse(s["abkhazia"])

    def test_no_predownload_optional_and_capped(self):
        # their terms: no bulk / offline download, so no saved-area download; an outage falls back to Sentinel-2
        for s in self.both["sources"].values():
            self.assertFalse(s["offline_ok"])
            self.assertTrue(s["optional"])
            self.assertIn("Sochi", s["role"])
            self.assertTrue(s["short"])
        self.assertEqual(self.both["sources"]["maptiler_sochi"]["daily_hard"], 3000)

    def test_keyed_helper(self):
        rows, auth, query, secrets, hard = imagery_regional._keyed({MT_VAR: MT_KEY})
        self.assertEqual(set(rows), {"maptiler_sochi"})
        self.assertEqual(auth, {})
        self.assertEqual(query, {"maptiler_sochi": (("key", MT_KEY),)})
        self.assertEqual(secrets, {MT_KEY})
        self.assertEqual(hard, {"api.maptiler.com": 3000})
        self.assertEqual(imagery_regional._keyed({}), ({}, {}, {}, set(), {}))


class RedactTest(unittest.TestCase):
    def test_keys_cut_out_of_messages_and_errors(self):
        import netfetch
        with unittest.mock.patch.object(imagery_regional, "SECRETS", {MT_KEY}):
            url = f"https://api.maptiler.com/maps/satellite/256/17/1/2.jpg?key={MT_KEY}"
            self.assertEqual(netfetch.redact(url), "https://api.maptiler.com/maps/satellite/256/17/1/2.jpg?key=<key>")
            e = netfetch.NotFound(f"{url}: HTTP 404", url, "api.maptiler.com", status=404)
            self.assertNotIn(MT_KEY, str(e))
            self.assertNotIn(MT_KEY, e.url)
            self.assertIsNone(netfetch.redact(None))


class Resp:
    body = b"\xff\xd8jpeg"
    elapsed = 0.01


class FakeFetcher:
    def __init__(self):
        self.seen = []

    def miss_alive(self, miss, url):
        return False

    def get(self, url, kind="demand", headers=None, lane=None):
        self.seen.append((url, headers))
        return Resp()


def bare_imagery(d):
    import imagery
    im = imagery.Imagery.__new__(imagery.Imagery)
    im.cache_dir = d
    im.fetcher = FakeFetcher()
    im.disk = imagery.cachemod.NONE
    im._local = threading.local()
    im._lock = threading.Lock()
    im.fetch_ms = []
    im._count = lambda what: None
    return im


def sochi_tile():
    import imagery
    x, y = imagery.mercator_px(43.5785, 39.72, 17)
    return int(x) // imagery.TILE, int(y) // imagery.TILE


class RequestTest(unittest.TestCase):
    def assert_no_key_on_disk(self, d, key):
        for root, dirs, files in os.walk(d):
            for n in dirs + files:
                self.assertNotIn(key, n)

    def test_header_key(self):
        import imagery
        row = imagery_regional.KEYED["esri_sochi"]
        src = imagery.Source("esri_sochi", row[0], 17, "Esri", bounds=row[2],
                             headers=(("X-Esri-Authorization", "Bearer " + ESRI_KEY),))
        with tempfile.TemporaryDirectory() as d:
            im = bare_imagery(d)
            self.assertEqual(im._download(src, 17, *sochi_tile()), Resp.body)
            url, headers = im.fetcher.seen[0]
            self.assertNotIn(ESRI_KEY, url)
            self.assertEqual(headers, {"X-Esri-Authorization": "Bearer " + ESRI_KEY})
            self.assert_no_key_on_disk(d, ESRI_KEY)

    def test_query_key(self):
        import imagery
        row = imagery_regional.KEYED["maptiler_sochi"]
        src = imagery.Source("maptiler_sochi", row[0], 17, "MapTiler", bounds=row[2], query=(("key", MT_KEY),))
        x, y = sochi_tile()
        with tempfile.TemporaryDirectory() as d:
            im = bare_imagery(d)
            self.assertEqual(im._download(src, 17, x, y), Resp.body)
            url, headers = im.fetcher.seen[0]
            self.assertEqual(url, f"https://api.maptiler.com/maps/satellite/256/17/{x}/{y}.jpg?key={MT_KEY}")
            self.assertIsNone(headers)
            self.assert_no_key_on_disk(d, MT_KEY)

    def test_keyless_source_unchanged(self):
        # a source without a key is asked exactly as before (fetchers that take no headers argument still work)
        import imagery

        class OldFetcher(FakeFetcher):
            def get(self, url, kind="demand"):
                self.seen.append((url, None))
                return Resp()

        src = imagery.SOURCES["eox2023"]
        with tempfile.TemporaryDirectory() as d:
            im = bare_imagery(d)
            im.fetcher = OldFetcher()
            self.assertEqual(im._download(src, 14, 9922, 5984), Resp.body)
            self.assertEqual(im.fetcher.seen[0][0], src.url.format(z=14, x=9922, y=5984, ty=0))


if __name__ == "__main__":
    unittest.main()
