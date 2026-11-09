"""sochiedits.py: the hand edits of Greater Sochi (roads by name or OSM way, ground areas), only inside the Sochi box,
a broken file ignored, the fingerprint empty without edits. No network."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapgeom  # noqa: E402
import roads  # noqa: E402
import sochiedits  # noqa: E402
import surface  # noqa: E402


def feats_of(lines, names, records=None):
    """A roads Features of LINE features, each a list of (lon, lat)."""
    coords = np.concatenate([np.asarray(l, np.float64) for l in lines])
    off = np.cumsum([0] + [len(l) for l in lines])
    n = len(lines)
    attrs = {"name": mapgeom.text_column(names)}
    if records is not None:
        attrs["src_record"] = mapgeom.text_column(records)
    return mapgeom.Features(layer="roads", ids=np.array([f"id{i:03d}" for i in range(n)]),
                            kind=np.full(n, 2, np.uint8), part_off=np.arange(n + 1), ring_off=np.arange(n + 1),
                            coord_off=off.astype(np.int64), lonlat=coords, attrs=attrs)


SPEC = roads.Spec(width=7.0, kind="paved", urban=True, cls="residential", oneway=False)
ZARYA = [(39.7310, 43.6040), (39.7330, 43.6050)]                # in Sochi
MOSCOW = [(37.6170, 55.7550), (37.6190, 55.7560)]


class EditsCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "edits.json")
        self.saved = sochiedits.EDITS_FILE
        sochiedits.EDITS_FILE = self.path
        sochiedits.reset()

    def tearDown(self):
        sochiedits.EDITS_FILE = self.saved
        sochiedits.reset()
        self.dir.cleanup()

    def write(self, doc):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False))
        sochiedits.reset()


class LoadTest(EditsCase):
    def test_no_file_and_empty_file_change_nothing(self):
        self.assertEqual(sochiedits.fingerprint(), "")
        self.write({"roads": [], "areas": []})
        self.assertEqual(sochiedits.fingerprint(), "")

    def test_edits_have_a_fingerprint_that_follows_the_file(self):
        self.write({"roads": [{"name": "улица Новая Заря", "lanes": 2}]})
        a = sochiedits.fingerprint()
        self.write({"roads": [{"name": "улица Новая Заря", "lanes": 3}]})
        self.assertTrue(a and sochiedits.fingerprint() and a != sochiedits.fingerprint())

    def test_broken_files_are_ignored(self):
        for doc in ("{not json", {"roads": [{"lanes": 2}]}, {"roads": [{"name": "x", "colour": "red"}]},
                    {"roads": [{"name": "x", "surface": "lava"}]},
                    {"areas": [{"ground": "cobble", "polygon": [[55.7, 37.6], [55.8, 37.6], [55.8, 37.7]]}]}):
            self.write(doc)
            got = sochiedits.load()
            self.assertEqual((got["roads"], got["areas"], got["fingerprint"]), ([], [], ""), doc)


class RoadsTest(EditsCase):
    def test_by_name_inside_the_box_only(self):
        self.write({"roads": [{"name": "Улица новая заря", "lanes": 2, "surface": "cobble", "sidewalks": "left",
                               "sidewalk_width": 3.5}]})
        f = feats_of([ZARYA, MOSCOW, ZARYA], ["улица Новая Заря", "улица Новая Заря", "Курортный проспект"])
        got = sochiedits.apply_roads(f, [SPEC, SPEC, SPEC])
        self.assertEqual((got[0].lanes, got[0].width, got[0].kind, got[0].sides, got[0].walk),
                         (2, 6.6, "cobble", (1,), 3.5))
        self.assertIs(got[1], SPEC, "Moscow's street of the same name is left alone")
        self.assertIs(got[2], SPEC)

    def test_by_way_near_and_drop(self):
        self.write({"roads": [{"way": 555, "width": 12.0, "surface": "concrete"},
                              {"name": "улица Новая Заря", "near": [43.6045, 39.7320, 200], "drop": True}]})
        far = [(39.7500, 43.6200), (39.7510, 43.6210)]
        f = feats_of([ZARYA, ZARYA, far], ["a", "улица Новая Заря", "улица Новая Заря"],
                     records=["w555@3", "w1@1", "w2@1"])
        got = sochiedits.apply_roads(f, [SPEC, SPEC, SPEC])
        self.assertEqual((got[0].width, got[0].kind, got[0].surf), (12.0, "paved", "concrete"))
        self.assertIsNone(got[1], "dropped")
        self.assertIs(got[2], SPEC, "beyond 'near'")

    def test_untouched_list_is_returned_as_is(self):
        f = feats_of([ZARYA], ["улица Новая Заря"])
        specs = [SPEC]
        self.assertIs(sochiedits.apply_roads(f, specs), specs)
        self.write({"roads": [{"name": "другая", "lanes": 2}]})
        self.assertIs(sochiedits.apply_roads(f, specs), specs)


class AreasTest(EditsCase):
    def grid(self):
        lat = np.repeat(np.linspace(43.6050, 43.6040, 50)[:, None], 50, axis=1)
        lon = np.repeat(np.linspace(39.7310, 39.7330, 50)[None, :], 50, axis=0)
        return np.full((50, 50), surface.DIRT, np.uint8), lat, lon

    def test_area_painted_in_the_ground_model(self):
        self.write({"areas": [{"ground": "cobble",
                               "polygon": [[43.6042, 39.7312], [43.6048, 39.7312], [43.6048, 39.7328],
                                           [43.6042, 39.7328]]}]})
        idx, lat, lon = self.grid()
        out = sochiedits.paint_areas(idx, lat, lon)
        self.assertEqual(int(out[25, 25]), surface.COBBLE)
        self.assertEqual(int(out[0, 0]), surface.DIRT)
        self.assertEqual(int(idx[25, 25]), surface.DIRT, "the input is not changed")

    def test_no_area_no_change(self):
        idx, lat, lon = self.grid()
        self.assertIs(sochiedits.paint_areas(idx, lat, lon), idx)


MALL_LAT = np.array([43.6045, 43.6055, 43.6055, 43.6045])     # a square footprint around (43.6050, 39.7322)
MALL_LON = np.array([39.7315, 39.7315, 39.7330, 39.7330])


class BuildingsTest(EditsCase):
    def test_point_inside_the_footprint(self):
        self.write({"buildings": [{"at": [43.6050, 39.7322], "height": 24, "levels": 4, "facade_material": "glass",
                                   "facade_color": "#d8dde0", "roof": "flat", "class": "retail"}]})
        at = {"class": "commercial", "seed_id": "x"}
        got = sochiedits.building_attrs(at, MALL_LAT, MALL_LON)
        self.assertEqual((got["height"], got["num_floors"], got["facade_material"], got["roof_shape"], got["class"]),
                         (24.0, 4, "glass", "flat", "retail"))
        self.assertTrue(got["edited_facade"])
        self.assertEqual(at, {"class": "commercial", "seed_id": "x"}, "the input is not changed")
        other = sochiedits.building_attrs(at, MALL_LAT + 0.01, MALL_LON)
        self.assertIs(other, at, "a building the point is not in")

    def test_by_name_and_drop(self):
        self.write({"buildings": [{"name": "МореМолл", "drop": True}]})
        self.assertIsNone(sochiedits.building_attrs({"name": "Моремолл"}, MALL_LAT, MALL_LON))
        moscow = sochiedits.building_attrs({"name": "МореМолл"}, MALL_LAT + 12, MALL_LON - 2)
        self.assertEqual(moscow, {"name": "МореМолл"}, "outside the box: as it is")

    def test_bad_building_edits_are_refused(self):
        for b in ({"height": 10}, {"at": [43.6, 39.7], "roof": "banana"}, {"at": [43.6, 39.7], "facade_color": "zz"},
                  {"at": [55.7, 37.6], "height": 10}, {"at": [43.6, 39.7], "levels": 0}):
            self.write({"buildings": [b]})
            self.assertEqual(sochiedits.load()["buildings"], [], b)

    def test_glass_keeps_its_windows_on_a_big_box(self):
        import buildings
        ring = np.array([[0, 0], [120, 0], [120, 80], [0, 80]], np.float64)
        plain = buildings.plan(ring, {"class": "commercial", "seed_id": "m"}, 0, 43.6, 39.73)
        glass = buildings.plan(ring, {"class": "commercial", "seed_id": "m", "facade_material": "glass",
                                      "edited_facade": True, "num_floors": 2, "roof_shape": "flat"}, 0, 43.6, 39.73)
        self.assertNotEqual(plain.facade, glass.facade)
        self.assertTrue(str(glass.facade).startswith(buildings.OFFICE))


if __name__ == "__main__":
    unittest.main()
