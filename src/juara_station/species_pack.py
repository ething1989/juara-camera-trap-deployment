from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import math
import subprocess
import sys
import tempfile

from .paths import atomic_replace_text


@dataclass(frozen=True)
class SpeciesPackSelection:
    latitude: float
    longitude: float
    region_key: str | None
    region_file: str | None
    cell_files: tuple[str, ...]
    species_count: int
    source: str = "species_pack"


def build_species_list_from_pack(
    pack_root: Path,
    latitude: float,
    longitude: float,
    *,
    nearest_cell_count: int = 4,
    cells_with_region: int = 4,
    cells_without_region: int = 6,
) -> tuple[list[str], SpeciesPackSelection]:
    pack_root = Path(pack_root)
    cells = _load_cells(pack_root)
    if not cells:
        raise FileNotFoundError(f"No cell index rows found in {pack_root / 'metadata' / 'cell_index.csv'}")
    region = _select_region(pack_root, latitude, longitude)
    # Use regions only as metadata. Unioning an entire biome such as Amazon
    # Rainforest makes the active list much too broad for local deployments.
    cell_count = nearest_cell_count
    nearest_cells = sorted(
        cells,
        key=lambda row: _haversine_km(latitude, longitude, float(row["center_lat"]), float(row["center_lon"])),
    )[: max(1, cell_count)]

    species: set[str] = set()
    region_file = region["species_file"] if region is not None else None
    cell_files = tuple(row["file"] for row in nearest_cells)
    for relative in cell_files:
        species.update(_read_species_file(pack_root / relative))

    selected = sorted(value for value in species if value)
    return selected, SpeciesPackSelection(
        latitude=latitude,
        longitude=longitude,
        region_key=region["key"] if region else None,
        region_file=region_file,
        cell_files=cell_files,
        species_count=len(selected),
    )


def write_active_species_list(
    pack_root: Path,
    output_path: Path,
    latitude: float,
    longitude: float,
    *,
    nearest_cell_count: int = 4,
    cells_with_region: int = 4,
    cells_without_region: int = 6,
) -> SpeciesPackSelection:
    species, selection = build_species_list_from_pack(
        pack_root,
        latitude,
        longitude,
        nearest_cell_count=nearest_cell_count,
        cells_with_region=cells_with_region,
        cells_without_region=cells_without_region,
    )
    atomic_replace_text(Path(output_path), "\n".join(species) + "\n")
    _write_metadata(output_path, selection)
    return selection


def write_world_species_list(pack_root: Path, output_path: Path) -> SpeciesPackSelection:
    pack_root = Path(pack_root)
    species = _read_species_file(pack_root / "regions" / "world.txt")
    if not species:
        values: set[str] = set()
        for row in _load_cells(pack_root):
            values.update(_read_species_file(pack_root / row["file"]))
        species = sorted(values)
    selected = sorted(value for value in species if value)
    atomic_replace_text(Path(output_path), "\n".join(selected) + "\n")
    selection = SpeciesPackSelection(
        latitude=-1,
        longitude=-1,
        region_key="world",
        region_file="regions/world.txt",
        cell_files=(),
        species_count=len(selected),
        source="world",
    )
    _write_metadata(output_path, selection, {"filter": "all_birds"})
    return selection


def write_birdnet_location_species_list(
    output_path: Path,
    latitude: float,
    longitude: float,
    *,
    week: int = -1,
    threshold: float = 0.03,
    timeout_seconds: int = 180,
) -> SpeciesPackSelection:
    output_path = Path(output_path)
    code = (
        "from birdnet_analyzer.species import species; "
        "import sys; "
        "species(sys.argv[1], lat=float(sys.argv[2]), lon=float(sys.argv[3]), "
        "week=int(sys.argv[4]), sf_thresh=float(sys.argv[5]), sortby='alpha')"
    )
    with tempfile.TemporaryDirectory(prefix="juara-birdnet-species-") as tmp:
        tmp_path = Path(tmp)
        subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(tmp_path),
                str(latitude),
                str(longitude),
                str(week),
                str(threshold),
            ],
            check=True,
            timeout=timeout_seconds,
        )
        species: set[str] = set()
        for path in sorted(tmp_path.glob("*.txt")):
            species.update(_read_species_file(path))
    selected = sorted(value for value in species if value)
    if not selected:
        raise RuntimeError("BirdNET location filter produced no species")
    atomic_replace_text(output_path, "\n".join(selected) + "\n")
    selection = SpeciesPackSelection(
        latitude=latitude,
        longitude=longitude,
        region_key="birdnet_location",
        region_file=None,
        cell_files=(),
        species_count=len(selected),
        source="birdnet_location",
    )
    _write_metadata(
        output_path,
        selection,
        {
            "filter": "birdnet_location_frequency",
            "week": week,
            "threshold": threshold,
            "timeout_seconds": timeout_seconds,
            "note": "BirdNET metadata location filter; not an eBird API radius query.",
        },
    )
    return selection


def _write_metadata(output_path: Path, selection: SpeciesPackSelection, extra: dict | None = None) -> None:
    metadata = {
        "latitude": selection.latitude,
        "longitude": selection.longitude,
        "region_key": selection.region_key,
        "region_file": selection.region_file,
        "cell_files": list(selection.cell_files),
        "species_count": selection.species_count,
        "source": selection.source,
    }
    if extra:
        metadata.update(extra)
    atomic_replace_text(Path(output_path).with_suffix(Path(output_path).suffix + ".metadata.json"), _json(metadata))


def _load_cells(pack_root: Path) -> list[dict[str, str]]:
    path = pack_root / "metadata" / "cell_index.csv"
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _load_regions(pack_root: Path) -> list[dict[str, str]]:
    path = pack_root / "metadata" / "region_index.csv"
    with path.open(newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("key") != "world"]


def _select_region(pack_root: Path, latitude: float, longitude: float) -> dict[str, str] | None:
    matches = [row for row in _load_regions(pack_root) if _region_contains(row, latitude, longitude)]
    if not matches:
        return None
    return sorted(matches, key=_region_specificity)[0]


def _region_contains(row: dict[str, str], latitude: float, longitude: float) -> bool:
    lat_min = float(row["lat_min"])
    lat_max = float(row["lat_max"])
    lon_min = float(row["lon_min"])
    lon_max = float(row["lon_max"])
    if not (lat_min <= latitude <= lat_max):
        return False
    if str(row.get("bbox_wraps_dateline", "")).lower() == "true":
        return longitude >= lon_min or longitude <= lon_max
    return lon_min <= longitude <= lon_max


def _region_specificity(row: dict[str, str]) -> tuple[float, int, str]:
    lat_span = abs(float(row["lat_max"]) - float(row["lat_min"]))
    lon_min = float(row["lon_min"])
    lon_max = float(row["lon_max"])
    if str(row.get("bbox_wraps_dateline", "")).lower() == "true":
        lon_span = 360.0 - abs(lon_max - lon_min)
    else:
        lon_span = abs(lon_max - lon_min)
    try:
        cell_count = int(row.get("cell_count") or 0)
    except ValueError:
        cell_count = 0
    return (lat_span * lon_span, cell_count, row.get("key", ""))


def _read_species_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    species = []
    for line in path.read_text(errors="replace").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "\t" in value:
            continue
        species.append(value)
    return species


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _json(value: dict) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True) + "\n"
