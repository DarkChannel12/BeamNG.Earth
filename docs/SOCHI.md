# Sochi improvement (branch `sochi`)

Work log and hand-off for improving Greater Sochi in BeamNG.Earth. Any session (cloud or on the player's PC) starts
here. All changes live in branch `sochi` of `DarkChannel12/BeamNG.Earth` and must stay inside the Sochi box: the rest
of the world behaves exactly as before.

## Player's setup (Windows PC)

- The mod was installed from a ZIP (`C:\Users\yan\Downloads\BeamNG.Earth-main`), not a git clone. To get this
  branch: download https://github.com/DarkChannel12/BeamNG.Earth/archive/refs/heads/sochi.zip, unpack next to it
  (e.g. `BeamNG.Earth-sochi`), start the game from there the same way as before. The cache lives outside the mod
  folder, so nothing downloaded is lost.
- Keys come from environment variables, never from files in the repo:
  `setx BNGE_MAPTILER_KEY "<key>"` (then reopen the terminal / launcher). The player has a MapTiler Cloud key.
- Never commit a key, never print it in logs.

## Plan

1. **Imagery** (done): opt-in keyed regional sources for the Sochi box.
2. **Elevation**: FABDEM (bare earth, 30 m) instead of Copernicus GLO-30 (a DSM: trees and roofs) inside the Sochi
   box (done, see below), then tune road grading on steep slopes (next).
3. **Roads**: case by case from the player's screenshots / coordinates; separate mod-code fixes from OSM edits (OSM
   edits must not be traced from Yandex or Google imagery).
4. **Saved areas**: pre-cache the area with "Build the world too" so nothing is built while driving.

First test area: about 1 x 1 km around MoreMall / Gagarina street / Tsvetnoy bulvar in central Sochi (a saved area
is at least 0.05 km2). Do step 2 before pre-caching big areas, or they must be downloaded again.

## Step 1: imagery (done)

- `server/imagery_regional.py` `KEYED`: `maptiler_sochi` (MapTiler Satellite, `maps/satellite/256`, ~0.5 m at the sea
  port, key `BNGE_MAPTILER_KEY`, sent as `?key=`) and `esri_sochi` (Esri World Imagery, key `BNGE_ESRI_KEY`, sent
  as the `X-Esri-Authorization` header; Esri does not serve Russia, so it is unused for now).
- Box (south, west, north, east): 43.38, 39.05, 44.10, 40.45 (Lazarevskoye to Adler and Krasnaya Polyana / Rosa
  Khutor; stops at the Psou border).
- Without the key the source does not exist (chain, credits, cache keys unchanged).
- `netfetch.redact` cuts keys out of the net log and every error; keys never land in cache paths.
- Terms: MapTiler allows a temporary personal cache only and forbids bulk download, Esri is not for offline export,
  so these sources are NOT pre-downloaded by saved areas (`offline_ok` off). The player drives a place once online
  and its photos stay in the ordinary cache. MapTiler is capped at 3,000 requests a day (free plan quota).
- Do not add Yandex or Google imagery: their terms forbid it, even for personal use.
- Tests: `server/tests/test_imagery_keyed.py`. Checked live at the sea port (43.5800, 39.7195).

## Step 2: elevation (FABDEM done; road grading next)

- `server/fabdem.py`: FABDEM v1.2 from Fondazione LINKS' COG mirror (`huggingface.co/buckets/links-ads/fabdem`, one
  ~11 MB file per 1 x 1 degree, pixel values as Bristol's). The bucket redirects to a signed CDN address and netfetch
  follows no redirects, so each file is downloaded whole once (Fetcher.session + urllib, like gravity.py) into
  `<cache>/fabdem/V1-2/` and read locally after that (offline too). Four files cover the box: N43E039, N43E040,
  N44E039, N44E040.
- `dem.Dem._bare12`: inside the box the z12 tiles are FABDEM instead of `dsmground.ground` (which leaves hilly ground
  alone, i.e. most of Sochi), faded into the filtered model over 1.5 km inside the box's edge and 60 m around FABDEM
  voids; deeper zooms are z12 upsampled as before. A failed read keeps the filtered model and is not mirrored.
  Outside the box nothing changes (the decoded mirror for box tiles lives in `g1fab12/`, the rest in `g1/`).
- `BNE_FABDEM=0` turns it off. Licence: Non-Commercial Government Licence v2.0 (credits.py, CREDITS.md, SOURCES.md).
- Checked 2026-11-09 on a 1 km W-E profile through MoreMall (43.6045 N, z15): mean |2nd difference| raw DSM 1.26,
  filtered 1.13, FABDEM 0.80. FABDEM is not perfect: big footprints (MoreMall itself) partly keep their height.
- Tests: `server/tests/test_fabdem.py`. Built-world caches rebuild by themselves (revisit.code_key hashes the code).

## Tests

`python3 tools/run_tests.py`. These also fail on unmodified `main` in a sandbox without full network or fonts:
`test_e2e`, `test_credits`, `test_launcher_linux`, `test_advance_sign_layout`, `test_build_windturbines`,
`test_overture_dedup`; `test_integration_drive` needs the real network (Mapterhorn, OSM).

## Offline play

`python launcher.py -- --offline`: the server uses the disk cache only (nothing loads from the network; places not
cached stay plain). Pre-cache first, drive the area once online for the MapTiler photos, then play offline.
