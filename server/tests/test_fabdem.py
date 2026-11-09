"""fabdem.py (Greater Sochi's bare earth) and its use in dem.Dem's z12 tiles: only inside the Sochi box, faded into the
filtered surface model at the box's edge, the filtered model kept when FABDEM cannot be read. No network."""
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem as demmod  # noqa: E402
import fabdem  # noqa: E402
import netfetch  # noqa: E402


def tile_of(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return z, x, y


MOREMALL = (43.6045, 39.7320)


class StubCog:
    """A 1-degree file as cog.Cog shows it: 3600 x 3600 px, height = 1000 * lon + 10 * lat (linear: bilinear exact)."""

    def __init__(self, lat0, lon0, nodata_west_of=None):
        self.lat0, self.lon0 = lat0, lon0
        self.width = self.height = 3600
        self.nodata = -9999.0
        self.nodata_west_of = nodata_west_of

    def pixel_of(self, x, y):
        return (np.asarray(x) - self.lon0) * 3600, (self.lat0 + 1 - np.asarray(y)) * 3600

    def read_window(self, c0, r0, w, h):
        col = np.arange(c0, c0 + w) + 0.5
        row = np.arange(r0, r0 + h) + 0.5
        lon = self.lon0 + col / 3600
        lat = self.lat0 + 1 - row / 3600
        a = (1000 * lon[None, :] + 10 * lat[:, None]).astype(np.float32)
        if self.nodata_west_of is not None:
            a[:, lon < self.nodata_west_of] = self.nodata
        return a


def stub_fabdem(cache, **kw):
    f = fabdem.Fabdem(cache, netfetch.Fetcher(offline=True))
    f._cog = lambda a, o: StubCog(a, o, **kw)
    return f


class NamesTest(unittest.TestCase):
    def test_file_names_and_urls(self):
        self.assertEqual(fabdem.file_name(43, 39), "N43E039")
        self.assertEqual(fabdem.file_name(-5, -70), "S05W070")
        self.assertEqual(fabdem.file_url(43, 39), fabdem.BASE_URL
                         + "/N40E030-N50E040_FABDEM_V1-2/N43E039_FABDEM_V1-2.tif")
        self.assertEqual(fabdem.file_url(44, 40), fabdem.BASE_URL
                         + "/N40E040-N50E050_FABDEM_V1-2/N44E040_FABDEM_V1-2.tif")

    def test_covers_only_the_sochi_box(self):
        self.assertTrue(fabdem.covers(*tile_of(*MOREMALL, 12)))
        self.assertFalse(fabdem.covers(*tile_of(55.75, 37.62, 12)))      # Moscow
        self.assertFalse(fabdem.covers(*tile_of(43.38 - 0.2, 39.5, 12)))  # out at sea south of the box

    def test_edge_weight(self):
        S, W, N, E = fabdem.BOUNDS
        w = fabdem.edge_weight(np.array([S, (S + N) / 2, (S + N) / 2, S - 0.1]),
                               np.array([(W + E) / 2, (W + E) / 2, W + 0.5 * fabdem.FEATHER_M / 111320 / 0.72, 39.5]))
        self.assertEqual(float(w[0]), 0.0)          # on the edge
        self.assertEqual(float(w[1]), 1.0)          # deep inside
        self.assertAlmostEqual(float(w[2]), 0.5, delta=0.02)
        self.assertEqual(float(w[3]), 0.0)          # outside


class TileTest(unittest.TestCase):
    def test_bilinear_on_dem_grid(self):
        with tempfile.TemporaryDirectory() as d:
            key = tile_of(*MOREMALL, 12)
            h, ok = stub_fabdem(d).tile(*key)
            self.assertTrue(ok.all())
            lat, lon = fabdem.tile_lat_lon(*key)
            np.testing.assert_allclose(h, 1000 * lon + 10 * lat, atol=0.05)

    def test_outside_is_none_and_voids_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(stub_fabdem(d).tile(*tile_of(55.75, 37.62, 12)))
            key = tile_of(*MOREMALL, 12)
            lat, lon = fabdem.tile_lat_lon(*key)
            mid = float(lon[0, 256])
            h, ok = stub_fabdem(d, nodata_west_of=mid).tile(*key)
            self.assertFalse(ok[:, :250].any())
            self.assertTrue(ok[:, 260:].all())


class DemBlendTest(unittest.TestCase):
    """Dem._bare12: FABDEM inside the box, the filtered surface model (_ground12) outside and at the box's edge."""

    def make(self, d, **kw):
        dm = demmod.Dem(d, fetcher=netfetch.Fetcher(offline=True))
        dm.fabdem = stub_fabdem(os.path.join(d, "fabdem"), **kw)
        dm.ground_calls = []

        def ground(x, y, a):
            dm.ground_calls.append((x, y))
            return np.full((demmod.TILE, demmod.TILE), -50.0, np.float32)
        dm._ground12 = ground
        return dm

    def test_inside_is_the_lower_ground(self):
        with tempfile.TemporaryDirectory() as d:
            dm = self.make(d)
            _, x, y = tile_of(*MOREMALL, 12)
            a = np.zeros((demmod.TILE, demmod.TILE), np.float32)
            want, _ = dm.fabdem.tile(12, x, y)
            np.testing.assert_array_equal(dm._bare12(x, y, a), np.minimum(want, -50.0))
            dm._ground12 = lambda x_, y_, a_: np.full((demmod.TILE, demmod.TILE), 1e5, np.float32)
            np.testing.assert_allclose(dm._bare12(x, y, a), want, atol=0.02,
                                       err_msg="FABDEM where the filtered model is higher")

    def test_outside_is_the_ground_filter(self):
        with tempfile.TemporaryDirectory() as d:
            dm = self.make(d)
            _, x, y = tile_of(55.75, 37.62, 12)
            got = dm._bare12(x, y, np.zeros((demmod.TILE, demmod.TILE), np.float32))
            self.assertEqual(float(got.max()), -50.0)

    def test_edge_fades(self):
        with tempfile.TemporaryDirectory() as d:
            dm = self.make(d)
            S, W, N, E = fabdem.BOUNDS
            _, x, y = tile_of((S + N) / 2, W + 0.001, 12)       # a tile across the west edge
            got = dm._bare12(x, y, np.zeros((demmod.TILE, demmod.TILE), np.float32))
            lat, lon = fabdem.tile_lat_lon(12, x, y)
            self.assertTrue(np.all(got[lon < W] == -50.0), "outside the box: the filtered model")
            self.assertEqual(len(dm.ground_calls), 1)

    def test_coast_void_fades(self):
        with tempfile.TemporaryDirectory() as d:
            key = tile_of(*MOREMALL, 12)
            lat, lon = fabdem.tile_lat_lon(*key)
            dm = self.make(d, nodata_west_of=float(lon[0, 256]))
            got = dm._bare12(key[1], key[2], np.zeros((demmod.TILE, demmod.TILE), np.float32))
            self.assertTrue(np.all(got[:, :250] == -50.0))

    def test_failure_keeps_ground_and_is_not_mirrored(self):
        with tempfile.TemporaryDirectory() as d:
            dm = self.make(d)

            def boom(*k):
                raise netfetch.Offline("offline")
            dm.fabdem.tile = boom
            _, x, y = tile_of(*MOREMALL, 12)
            dm._local.partial = False
            got = dm._bare12(x, y, np.zeros((demmod.TILE, demmod.TILE), np.float32))
            self.assertEqual(float(got.max()), -50.0)
            self.assertTrue(dm._local.partial, "a stand-in: not written to the mirror")


class MirrorPathTest(unittest.TestCase):
    def test_sochi_tiles_have_their_own_folder(self):
        with tempfile.TemporaryDirectory() as d:
            dm = demmod.Dem(d, fetcher=netfetch.Fetcher(offline=True))
            sochi12, sochi15 = tile_of(*MOREMALL, 12), tile_of(*MOREMALL, 15)
            moscow = tile_of(55.75, 37.62, 12)
            self.assertTrue(dm.fabdem_at(sochi12) and dm.fabdem_at(sochi15))
            self.assertFalse(dm.fabdem_at(moscow) or dm.fabdem_at(tile_of(*MOREMALL, 11)))
            self.assertIn(fabdem.MIRROR_TAG, dm._mirror_path(sochi15))
            self.assertNotIn(fabdem.MIRROR_TAG, dm._mirror_path(moscow))
            dm.fabdem = None                                    # BNE_FABDEM=0: the old folder
            self.assertNotIn(fabdem.MIRROR_TAG, dm._mirror_path(sochi15))


if __name__ == "__main__":
    unittest.main()
