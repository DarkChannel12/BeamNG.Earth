"""Keyed (opt-in) regional imagery (imagery_regional.KEYED: Esri World Imagery over Greater Sochi with the player's
own ArcGIS key): off without the key (the chain, credits and every source as before), only inside its box with it,
the key in a request header (never the URL, a log line or a cache path), and never pre-downloaded by a saved area.
No network: each case imports the modules in a fresh process with its own environment, or uses a fake fetcher."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import imagery_regional  # noqa: E402

KEY = "test-key-0123456789abcdef"
VAR = imagery_regional.KEYED["esri_sochi"][5]


def probe(env_key):
    """The keyed source as a fresh process sees it with BNGE_ESRI_KEY = env_key (None: unset)."""
    code = r"""
import json, sys
import imagery, imagery_regional, netfetch, credits
s = imagery.SOURCES.get("esri_sochi")
def t(lat, lon):
    x, y = imagery.mercator_px(lat, lon, 17)
    return int(x) // imagery.TILE, int(y) // imagery.TILE
out = {"chain": list(imagery.DEFAULT_CHAIN), "regional": list(imagery_regional.REGIONAL),
       "attribution": imagery.ATTRIBUTION, "has": s is not None}
if s is not None:
    host = netfetch.host_of(s.url)
    p = netfetch.POLICIES.get(host)
    out.update(url=s.url, headers=dict(s.headers), bounds=list(s.bounds), host=host,
               offline_ok=p.offline_ok, optional=p.optional, role=netfetch.role_of(host),
               short=credits.SHORT.get("img:esri_sochi"),
               inside=bool(imagery.in_bounds(s, 17, *t(43.5785, 39.7200))),
               outside=bool(imagery.in_bounds(s, 17, *t(55.7539, 37.6208))),
               abkhazia=bool(imagery.in_bounds(s, 17, *t(43.20, 40.30))))
print(json.dumps(out))
"""
    env = {k: v for k, v in os.environ.items() if k != VAR}
    if env_key is not None:
        env[VAR] = env_key
    r = subprocess.run([sys.executable, "-c", code], cwd=str(SERVER), env=env, capture_output=True, text=True,
                       timeout=300)
    if r.returncode:
        raise AssertionError(r.stderr[-3000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


class KeyedImageryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.off = probe(None)
        cls.blank = probe("   ")
        cls.on = probe(KEY)

    def test_off_without_key(self):
        for got in (self.off, self.blank):
            self.assertFalse(got["has"])
            self.assertNotIn("esri_sochi", got["regional"])
            self.assertNotIn("esri_sochi", got["chain"])
            self.assertNotIn("Esri", got["attribution"])

    def test_key_only_adds_the_source(self):
        # with the key the chain is the same plus esri_sochi among the regional sources (before Sentinel-2)
        on, off = self.on["chain"], self.off["chain"]
        self.assertEqual([n for n in on if n != "esri_sochi"], off)
        self.assertLess(on.index("esri_sochi"), on.index("eox2023"))

    def test_key_in_header_not_url(self):
        on = self.on
        self.assertTrue(on["has"])
        self.assertNotIn(KEY, on["url"])
        self.assertEqual(on["headers"], {"X-Esri-Authorization": "Bearer " + KEY})
        self.assertNotIn(KEY, on["attribution"])

    def test_only_inside_sochi(self):
        self.assertTrue(self.on["inside"])          # Sochi sea port
        self.assertFalse(self.on["outside"])        # Moscow
        self.assertFalse(self.on["abkhazia"])       # south of the Psou border

    def test_no_predownload_and_optional(self):
        # Esri's terms: not for offline export, so no saved-area bulk download; its outage falls back to Sentinel-2
        self.assertFalse(self.on["offline_ok"])
        self.assertTrue(self.on["optional"])
        self.assertIn("Sochi", self.on["role"])
        self.assertTrue(self.on["short"])

    def test_keyed_helper(self):
        rows, auth = imagery_regional._keyed({VAR: KEY})
        self.assertEqual(set(rows), {"esri_sochi"})
        self.assertEqual(auth["esri_sochi"], {"X-Esri-Authorization": "Bearer " + KEY})
        self.assertEqual(imagery_regional._keyed({}), ({}, {}))


class HeaderSentTest(unittest.TestCase):
    def test_download_sends_the_header_and_keeps_it_out_of_the_cache(self):
        import imagery
        import netfetch
        src = imagery.Source("esri_sochi", imagery_regional.KEYED["esri_sochi"][0], 17, "Esri",
                             bounds=imagery_regional.KEYED["esri_sochi"][2], headers=(("X-Esri-Authorization",
                                                                                         "Bearer " + KEY),))
        seen = []

        class Resp:
            body = b"\xff\xd8jpeg"
            elapsed = 0.01

        class Fetcher:
            def miss_alive(self, miss, url):
                return False

            def get(self, url, kind="demand", headers=None, lane=None):
                seen.append((url, headers))
                return Resp()

        with tempfile.TemporaryDirectory() as d:
            im = imagery.Imagery.__new__(imagery.Imagery)
            im.cache_dir = d
            im.fetcher = Fetcher()
            im.disk = imagery.cachemod.NONE
            im._local = __import__("threading").local()
            im._lock = __import__("threading").Lock()
            im.fetch_ms = []
            im._count = lambda what: None
            x, y = imagery.mercator_px(43.5785, 39.72, 17)
            got = im._download(src, 17, int(x) // imagery.TILE, int(y) // imagery.TILE)
            self.assertEqual(got, Resp.body)
            url, headers = seen[0]
            self.assertNotIn(KEY, url)
            self.assertEqual(headers, {"X-Esri-Authorization": "Bearer " + KEY})
            for root, dirs, files in os.walk(d):
                for n in dirs + files:
                    self.assertNotIn(KEY, n)
        del netfetch


if __name__ == "__main__":
    unittest.main()
