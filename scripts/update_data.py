#!/usr/bin/env python3
"""
STFC Galaxy Map — Data Update Script
=====================================
Fetches live data from data.stfc.space and merges it with the existing
systems.geojson, preserving manually-curated data (armada events, armada
strength ranges, station hubs) that the API does not expose.

Strategy:
  New API data  ->  coordinates, level, warp, faction, mines, hostiles,
                    new system type flags (Mirror, Wave Defense, Surge, etc.)
  Preserved     ->  event (Armada/Swarm/Borg/Separatist/Eclipse),
                    uncommonArmadaRange, rareArmadaRange, epicArmadaRange,
                    stationHub

Outputs:
  assets/json/systems.geojson       -- all 2,453+ systems as map markers
  assets/json/travel-paths.geojson  -- warp lane connections
  assets/json/territories.geojson   -- faction territory polygons

Run manually:   python3 scripts/update_data.py
Run via CI:     .github/workflows/update-map-data.yml (weekly)
"""

import json
import math
import os
import sys
import urllib.request
import urllib.error
from collections import defaultdict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL   = "https://data.stfc.space"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(SCRIPT_DIR, "..", "assets", "json")

# Coordinate transform: game coords -> Leaflet map coords.
# Derived by least-squares regression across 828 matched systems.
# Note: axes are swapped -- game X maps to map Y and vice versa.
COORD_A = 0.9166    # new_y -> old_x  (scale)
COORD_B = -89.66    # old_x offset
COORD_C = 0.9202    # new_x -> old_y  (scale)
COORD_D = -5096.01  # old_y offset

def game_to_map(new_x, new_y):
    """Convert game coordinate space to Leaflet map coordinate space."""
    old_x = round(new_y * COORD_A + COORD_B)
    old_y = round(new_x * COORD_C + COORD_D)
    return [old_x, old_y]

# hull_type -> hostile display name.
# Derived from cross-referencing 350+ systems between old and new data.
HULL_TYPE_MAP = {
    0: "Interceptor",
    1: "Survey",
    2: "Explorer",
    3: "Battleship",
    5: "Explorer",  # newer hostile variant -- best fit from available data
}

# Known faction ID -> territory name
KNOWN_FACTION_NAMES = {
    2064723306: "Federation",
    4153667145: "Klingon",
    669838839:  "Romulan",
    2113010081: "Augment",
    2143656960: "Rogue",
    -1:         "Independent",
}

HUB_SYSTEM_IDS = {
    31, 39, 42, 52, 54, 55, 90, 2876, 71358,
    105928770, 146624309, 493472365, 622998673, 669580953,
    836491336, 846029245, 890410238, 999262121, 1045590996,
    1135473830, 1180879330, 1312983846, 1382564927, 1543362389,
    1691252927, 1702244411, 1753923484, 2095413858,
}

CAPITAL_SYSTEM_IDS = {439344754, 1498683722, 1761673303}

TERRITORY_COLORS = {
    "Federation": {"color": "#3498db", "fillColor": "#3498db"},
    "Klingon":    {"color": "#c0392b", "fillColor": "#c0392b"},
    "Romulan":    {"color": "#27ae60", "fillColor": "#2ecc71"},
    "Augment":    {"color": "#f1c40f", "fillColor": "#f1c40f"},
    "Rogue":      {"color": "#9b59b6", "fillColor": "#8e44ad"},
}

# Fields from old geojson to always carry forward -- the API doesn't provide these
PRESERVED_FIELDS = {
    "event",
    "uncommonArmadaRange",
    "rareArmadaRange",
    "epicArmadaRange",
    "stationHub",
}

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_version():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/version.txt", timeout=30) as r:
            return r.read().decode().strip()
    except Exception as e:
        print(f"  Warning: could not fetch version.txt ({e})")
        return None

def fetch_json(path, version=None):
    url = f"{BASE_URL}{path}"
    if version:
        url += f"?version={version}"
    print(f"  Fetching {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stfc-galaxy-map/updater"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code} fetching {url}")
        return None
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}")
        return None

# ---------------------------------------------------------------------------
# Load preserved curated data
# ---------------------------------------------------------------------------

def load_preserved_data():
    """
    Read existing systems.geojson and return a dict of
    {system_id: {field: value}} for fields the API doesn't expose.
    """
    path = os.path.join(ASSETS_DIR, "systems.geojson")
    if not os.path.exists(path):
        print("  No existing systems.geojson found -- starting fresh.")
        return {}

    with open(path) as f:
        geo = json.load(f)

    preserved = {}
    for feat in geo.get("features", []):
        p   = feat["properties"]
        sid = p.get("systemID")
        if sid is None:
            continue
        entry = {field: p[field] for field in PRESERVED_FIELDS if field in p}
        preserved[sid] = entry

    print(f"  Loaded {len(preserved)} existing systems")
    print(f"    With events:          {sum(1 for v in preserved.values() if v.get('event'))}")
    print(f"    With station hubs:    {sum(1 for v in preserved.values() if v.get('stationHub'))}")
    print(f"    With armada ranges:   {sum(1 for v in preserved.values() if any(v.get(k) for k in ['uncommonArmadaRange','rareArmadaRange','epicArmadaRange']))}")
    return preserved

# ---------------------------------------------------------------------------
# Lookup builders
# ---------------------------------------------------------------------------

def build_name_lookup(translations):
    if not translations:
        return {}
    return {str(item["id"]): item["text"]
            for item in translations if item.get("key") == "title"}

def build_faction_lookup(factions_data):
    lookup = dict(KNOWN_FACTION_NAMES)
    if factions_data:
        for item in factions_data:
            fid  = item.get("id")
            name = item.get("text", "").strip()
            if fid is not None and name and int(fid) not in lookup:
                lookup[int(fid)] = name
    return lookup

def build_resource_lookup(resource_summary, materials_data):
    """
    Build {mine_resource_id (int): display_name}.

    resource/summary.json fields:
      - "id"          internal table ID (small number — NOT what mine_resources uses)
      - "resource_id" game resource ID  (large number — matches mine_resources in systems)
      - "loca_id"     translation key   → look up display name in materials/hud translation

    Translation files (materials.json, hud.json) entries: {id, key, text}
      The "key" field varies per file and is NOT always "title", so we accept all entries.
    """
    lookup = {}
    if not resource_summary:
        print("  WARNING: resource/summary.json missing -- mine names will be blank")
        return lookup

    sample = resource_summary[0] if isinstance(resource_summary, list) else {}
    print(f"  resource/summary.json sample keys: {list(sample.keys())[:8]}")
    print(f"  resource/summary.json sample values: {list(sample.values())[:8]}")

    if not materials_data:
        print("  WARNING: translation data missing -- mine names will be blank")
        return lookup

    # Build translation lookup: loca_id -> display name.
    # Translation files have multiple entries per loca_id with different "key" values,
    # e.g. "short_name" = "Parsteel", "description" = "Used to upgrade 1★ Battleships."
    # We must prefer short_name > title > any other key to get display-safe names.
    KEY_PRIORITY = {"short_name": 0, "title": 1}
    loca_to_name  = {}   # loca_id -> best name found so far
    loca_priority = {}   # loca_id -> priority of that name (lower = better)

    for item in materials_data:
        text = item.get("text", "").strip()
        iid  = item.get("id")
        key  = item.get("key", "")
        if not text or iid is None:
            continue
        try:
            loca_id = int(iid)
        except (ValueError, TypeError):
            continue
        priority = KEY_PRIORITY.get(key, 2)
        if loca_id not in loca_priority or priority < loca_priority[loca_id]:
            loca_to_name[loca_id]  = text
            loca_priority[loca_id] = priority

    print(f"  Translation entries loaded (all keys): {len(loca_to_name)}")
    if loca_to_name:
        sample_loca = sorted(loca_to_name.keys())[:5]
        print(f"  Sample loca_ids: {sample_loca} -> {[loca_to_name[k] for k in sample_loca]}")

    # Build game resource id -> loca_id mapping.
    # "id" is the large integer that matches mine_resources in system data.
    # "resource_id" is a STRING like 'Resource_ShipXP' — do NOT use it for matching.
    resource_id_to_loca = {}
    for r in resource_summary:
        loca = r.get("loca_id")
        if loca is None:
            continue
        game_id = r.get("id")   # integer, matches mine_resource IDs in systems
        if game_id is not None:
            resource_id_to_loca[game_id] = loca

    print(f"  resource_id->loca mappings: {len(resource_id_to_loca)}")

    # Resolve: mine resource_id -> loca_id -> display name
    hits = 0
    for game_id, loca in resource_id_to_loca.items():
        name = loca_to_name.get(int(loca) if not isinstance(loca, int) else loca)
        if name:
            lookup[game_id] = name
            hits += 1

    if hits:
        print(f"  Mine names resolved: {hits}")
        return lookup

    print("  WARNING: mine names still unresolved. Diagnostic:")
    sample_gids = list(resource_id_to_loca.keys())[:5]
    sample_locas = [resource_id_to_loca[g] for g in sample_gids]
    print(f"    game resource ids:  {sample_gids}")
    print(f"    their loca_ids:     {sample_locas}")
    print(f"    loca_to_name keys:  {sorted(loca_to_name.keys())[:10]}")
    return lookup

# ---------------------------------------------------------------------------
# Field derivation
# ---------------------------------------------------------------------------

def get_hostile_types(hostiles):
    types = set()
    for h in hostiles:
        if h.get("is_scout"):
            types.add("Scout")
        else:
            name = HULL_TYPE_MAP.get(h.get("hull_type"))
            if name:
                types.add(name)
    return ", ".join(sorted(types)) if types else ""

def get_mine_names(mine_resource_ids, resource_lookup):
    if not mine_resource_ids:
        return "None"
    names = [resource_lookup[rid] for rid in mine_resource_ids if rid in resource_lookup]
    return ", ".join(sorted(set(names))) if names else "None"

def get_territory(faction_ids, faction_lookup):
    for fid in faction_ids:
        name = faction_lookup.get(int(fid) if not isinstance(fid, int) else fid)
        if name and name != "Independent":
            return name
    return "Independent"

def get_new_system_event(system):
    if system.get("is_wave_defense"):
        return "Wave Defense"
    if system.get("is_mirror_universe"):
        return "Mirror Universe"
    if system.get("is_surge_system"):
        return "Surge"
    return ""

def get_icon(system_id):
    if system_id in CAPITAL_SYSTEM_IDS:
        return "capital"
    if system_id in HUB_SYSTEM_IDS:
        return "hub"
    return ""

# ---------------------------------------------------------------------------
# Systems GeoJSON
# ---------------------------------------------------------------------------

def build_systems_geojson(summary, system_names, faction_lookup,
                          resource_lookup, preserved_data):
    features      = []
    merged_count  = 0
    new_count     = 0
    mine_resolved = 0

    for s in summary:
        sid    = s["id"]
        name   = system_names.get(str(sid), f"System {sid}")
        coords = game_to_map(s["coords_x"], s["coords_y"])
        old    = preserved_data.get(sid, {})

        mine_names = get_mine_names(s.get("mine_resources", []), resource_lookup)
        if mine_names != "None":
            mine_resolved += 1

        # Events: keep preserved classic events; fall back to new-style flags
        event = old.get("event") or get_new_system_event(s)

        # Station hub: keep preserved value; fall back to API has_outpost
        station_hub = old.get("stationHub", 1 if s.get("has_outpost") else 0)

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": coords
            },
            "properties": {
                "name":               name,
                "systemID":           sid,
                "systemLevel":        s["level"],
                "warpRequired":       s["est_warp"],
                "territory":          get_territory(s.get("faction", []), faction_lookup),
                "icon":               get_icon(sid),
                "event":              event,
                "uncommonArmadaRange": old.get("uncommonArmadaRange", ""),
                "rareArmadaRange":    old.get("rareArmadaRange", ""),
                "epicArmadaRange":    old.get("epicArmadaRange", ""),
                "stationHub":         station_hub,
                "mines":              mine_names,
                "hostiles":           get_hostile_types(s.get("hostiles", [])),
                "deepDarkSpace":      1 if s.get("is_deep_space") else 0,
                "isMirrorUniverse":   1 if s.get("is_mirror_universe") else 0,
                "isWaveDefense":      1 if s.get("is_wave_defense") else 0,
                "isSurgeSystem":      1 if s.get("is_surge_system") else 0,
                "isRegionalSpace":    1 if s.get("is_regional_space") else 0,
                "hazardLevel":        s.get("hazard_level") or 0,
                "hasOutpost":         1 if s.get("has_outpost") else 0,
                "hasMissions":        1 if s.get("has_missions") else 0,
            }
        })

        if old:
            merged_count += 1
        else:
            new_count += 1

    print(f"  Updated existing systems:  {merged_count}")
    print(f"  New systems added:         {new_count}")
    print(f"  Systems with mine names:   {mine_resolved}")
    return {"type": "FeatureCollection", "features": features}

# ---------------------------------------------------------------------------
# Travel paths
# ---------------------------------------------------------------------------

def build_travel_paths(summary, special_paths=None):
    coord_map  = {s["id"]: game_to_map(s["coords_x"], s["coords_y"]) for s in summary}
    seen_pairs = set()
    features   = []

    for s in summary:
        for h in s.get("hostiles", []):
            patrol = h.get("systems", [])
            for i in range(len(patrol) - 1):
                a, b = patrol[i], patrol[i + 1]
                pair = (min(a, b), max(a, b))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                ca, cb = coord_map.get(a), coord_map.get(b)
                if ca is None or cb is None:
                    continue
                # Path endpoints must be stored as [y, x] (swapped vs system coords).
                # initTravelPaths processes them through xy([a,b]) = L.latLng(b,a),
                # while system nodes use L.circle([a,b]) = lat=a,lng=b directly.
                # Swap ensures both resolve to the same Leaflet position.
                ca_path = [ca[1], ca[0]]
                cb_path = [cb[1], cb[0]]
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": [ca_path, cb_path]},
                    "properties": {"className": ""}
                })

    if special_paths:
        features.extend(special_paths)

    return {"type": "FeatureCollection", "features": features}

# ---------------------------------------------------------------------------
# Territory polygons
# ---------------------------------------------------------------------------

def convex_hull(points):
    points = sorted(set(map(tuple, points)))
    if len(points) < 3:
        return [list(p) for p in points]
    def cross(O, A, B):
        return (A[0]-O[0])*(B[1]-O[1]) - (A[1]-O[1])*(B[0]-O[0])
    lower, upper = [], []
    for p in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]

def expand_polygon(polygon, margin=60):
    if len(polygon) < 3:
        return polygon
    cx = sum(p[0] for p in polygon) / len(polygon)
    cy = sum(p[1] for p in polygon) / len(polygon)
    expanded = []
    for x, y in polygon:
        dx, dy = x - cx, y - cy
        dist   = math.sqrt(dx*dx + dy*dy) or 1
        expanded.append([round(x + dx/dist*margin, 1),
                         round(y + dy/dist*margin, 1)])
    return expanded

def build_territories(summary, faction_lookup, existing_territories=None):
    faction_coords = defaultdict(list)
    for s in summary:
        territory = get_territory(s.get("faction", []), faction_lookup)
        if territory and territory != "Independent":
            faction_coords[territory].append(game_to_map(s["coords_x"], s["coords_y"]))

    features   = []
    HAND_DRAWN = {"Federation", "Klingon", "Romulan", "Augment", "Rogue"}

    if existing_territories:
        features.extend(existing_territories.get("features", []))

    for territory, coords in sorted(faction_coords.items()):
        if territory in HAND_DRAWN or len(coords) < 3:
            continue
        hull        = convex_hull(coords)
        hull        = expand_polygon(hull, margin=60)
        hull_closed = hull + [hull[0]]
        colors      = TERRITORY_COLORS.get(territory, {"color": "#95a5a6", "fillColor": "#95a5a6"})
        slug        = territory.lower().replace(" ", "-").replace("'", "")
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [hull_closed]},
            "properties": {
                "popupContent": f"{slug}-territory",
                "color":        colors["color"],
                "fillColor":    colors["fillColor"],
                "fillOpacity":  0.05,
                "weight":       1,
                "opacity":      0.3,
            }
        })
        print(f"  Generated territory: {territory} ({len(coords)} systems, {len(hull)} vertices)")

    return {"type": "FeatureCollection", "features": features}

# ---------------------------------------------------------------------------
# Navigation / special paths (transwarp, borg, arena)
# ---------------------------------------------------------------------------

def parse_navigation_paths(nav_data, coord_map):
    if not nav_data:
        return []
    # Log structure on first encounter to implement full parsing in a follow-up
    print(f"  navigation.json: {len(nav_data)} entries")
    if nav_data:
        sample = nav_data[0] if isinstance(nav_data, list) else str(nav_data)[:200]
        print(f"  Sample: {sample}")
    return []

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("STFC Galaxy Map -- Data Updater")
    print("=" * 60)

    print("\n[1/7] Loading preserved curated data...")
    preserved_data = load_preserved_data()

    print("\n[2/7] Fetching API version...")
    version = fetch_version()
    print(f"  Version: {version or 'unpinned'}")

    print("\n[3/7] Fetching data from data.stfc.space...")
    system_summary    = fetch_json("/system/summary.json",           version)
    resource_summary  = fetch_json("/resource/summary.json",         version)
    system_names_raw  = fetch_json("/translations/en/systems.json",  version)
    faction_names_raw = fetch_json("/translations/en/factions.json", version)
    materials_raw     = fetch_json("/translations/en/materials.json", version)
    hud_raw           = fetch_json("/translations/en/hud.json",       version)
    navigation_raw    = fetch_json("/translations/en/navigation.json", version)

    if not system_summary:
        print("\nERROR: Could not fetch system summary. Aborting.")
        sys.exit(1)
    print(f"\n  Systems loaded: {len(system_summary)}")

    print("\n[4/7] Building lookup tables...")
    system_names    = build_name_lookup(system_names_raw or [])
    faction_lookup  = build_faction_lookup(faction_names_raw or [])
    # Combine materials + hud translations: base resource names (loca_id 63, 64…)
    # live in hud.json while level-specific names are in materials.json
    combined_names  = (materials_raw or []) + (hud_raw or [])
    resource_lookup = build_resource_lookup(resource_summary, combined_names)
    print(f"  System names:    {len(system_names)}")
    print(f"  Factions:        {len(faction_lookup)}")
    print(f"  Mine resources:  {len(resource_lookup)}")
    if not resource_lookup:
        print("  WARNING: mine names will show as 'None' this run.")

    existing_territories = None
    territories_path = os.path.join(ASSETS_DIR, "territories.geojson")
    if os.path.exists(territories_path):
        with open(territories_path) as f:
            existing_territories = json.load(f)

    print("\n[5/7] Transforming systems...")
    systems_geo = build_systems_geojson(
        system_summary, system_names, faction_lookup,
        resource_lookup, preserved_data
    )
    print(f"  Total features: {len(systems_geo['features'])}")

    print("\n[6/7] Building travel paths...")
    coord_map     = {s["id"]: game_to_map(s["coords_x"], s["coords_y"]) for s in system_summary}
    special_paths = parse_navigation_paths(navigation_raw, coord_map)
    paths_geo     = build_travel_paths(system_summary, special_paths)
    print(f"  Warp lanes: {len(paths_geo['features'])}")

    print("\n[7/7] Building territory polygons...")
    territories_geo = build_territories(system_summary, faction_lookup, existing_territories)
    print(f"  Territory polygons: {len(territories_geo['features'])}")

    print("\n[Writing files...]")
    os.makedirs(ASSETS_DIR, exist_ok=True)

    def write_json(filename, data):
        path = os.path.join(ASSETS_DIR, filename)
        with open(path, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        print(f"  OK {filename}: {os.path.getsize(path)/1024:.0f} KB")

    write_json("systems.geojson",      systems_geo)
    write_json("travel-paths.geojson", paths_geo)
    write_json("territories.geojson",  territories_geo)

    # Final summary
    p = [f["properties"] for f in systems_geo["features"]]
    print("\n" + "=" * 60)
    print("Done. Galaxy map data updated successfully.")
    print("=" * 60)
    print(f"  Systems:                 {len(p)}")
    print(f"  Warp lanes:              {len(paths_geo['features'])}")
    print(f"  With classic events:     {sum(1 for x in p if x.get('event') in ('Armada','Swarm','Borg','Borg Armada','Borg Megacube','Eclipse Armada','Separatist'))}")
    print(f"  With new-type events:    {sum(1 for x in p if x.get('event') in ('Wave Defense','Mirror Universe','Surge'))}")
    print(f"  With armada ranges:      {sum(1 for x in p if any(x.get(k) for k in ['uncommonArmadaRange','rareArmadaRange','epicArmadaRange']))}")
    print(f"  With mines:              {sum(1 for x in p if x.get('mines') not in ('None',''))}")
    print(f"  Station hubs:            {sum(1 for x in p if x.get('stationHub'))}")
    print(f"  Mirror Universe systems: {sum(1 for x in p if x.get('isMirrorUniverse'))}")
    print(f"  Wave Defense systems:    {sum(1 for x in p if x.get('isWaveDefense'))}")

if __name__ == "__main__":
    main()
