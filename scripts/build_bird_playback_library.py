#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import random
import shutil
import subprocess
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "bird_playback_test"
CACHE = BASE / "source_cache"
SINGLES = BASE / "singles"
MIXES = BASE / "mixes"
MANIFESTS = BASE / "manifests"
EXTRACTED = CACHE / "extracted_soundscapes"

ANNOTATIONS = CACHE / "per_annotations.csv"
SPECIES = CACHE / "per_species.csv"
ZIP_PATH = CACHE / "per_soundscape_data.zip"

SOURCE_DATASET = "PER fully annotated soundscapes, Southwestern Amazon Basin"
SOURCE_DOI = "10.5281/zenodo.7079124"
SOURCE_URL = "https://zenodo.org/records/7079124"
SOURCE_LICENSE = "cc-by-4.0"

CLIP_SECONDS = 10.0
MIX_SECONDS = 20.0
TARGET_SINGLES = 80
TARGET_MIXES = 40
MAX_PER_SPECIES = 5
RANDOM_SEED = 20260510

# Broadly relevant or useful controls for western/central Brazil playback tests.
# The source dataset is Amazon Basin, so this list is intentionally conservative
# but not treated as a final site species checklist.
PRIORITY_CODES = [
    "undtin1",  # Undulated Tinamou
    "thlwre1",  # Thrush-like Wren
    "greant1",  # Great Antshrike
    "grasal3",  # Blue-gray Saltator
    "pirfly1",  # Piratic Flycatcher
    "meapar",  # Mealy Parrot
    "whwbec1",  # White-winged Becard
    "ducfly",  # Dusky-capped Flycatcher
    "littin1",  # Little Tinamou
    "grfdov1",  # Gray-fronted Dove
    "plupig2",  # Plumbeous Pigeon
    "bucmot4",  # Amazonian Motmot
    "butsal1",  # Buff-throated Saltator
    "blbthr1",  # Black-billed Thrush
    "tabsco1",  # Tawny-bellied Screech-Owl
    "amapyo1",  # Amazonian Pygmy-Owl
    "fepowl",  # Ferruginous Pygmy-Owl
    "horscr1",  # Horned Screamer
    "stbwoo2",  # Straight-billed Woodcreeper
    "rebmac2",  # Red-bellied Macaw
    "sobcac1",  # Solitary Black Cacique
    "forela1",  # Forest Elaenia
]


@dataclass(frozen=True)
class Annotation:
    filename: str
    start: float
    end: float
    low_freq: int
    high_freq: int
    code: str


@dataclass(frozen=True)
class Candidate:
    annotation: Annotation
    clip_start: float
    clip_end: float
    isolated: bool
    priority_rank: int
    annotation_count: int


def main() -> None:
    for path in [SINGLES, MIXES, MANIFESTS, EXTRACTED]:
        path.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is required to cut and mix playback clips.")

    species = load_species()
    annotations = load_annotations()
    candidates = select_candidates(annotations)
    if not all_expected_singles_exist(candidates, species):
        if not ZIP_PATH.exists():
            raise SystemExit(f"Missing {ZIP_PATH}. Download soundscape_data.zip first.")
        needed_files = sorted({candidate.annotation.filename for candidate in candidates})
        extract_soundscapes(needed_files)
    else:
        print("Using existing single-bird clips; source soundscape archive is not needed", flush=True)

    single_rows = build_singles(candidates, species)
    mix_rows = build_mixes(single_rows, species)
    all_rows = single_rows + mix_rows
    write_manifests(all_rows)
    write_summary(all_rows, single_rows, mix_rows)


def all_expected_singles_exist(candidates: list[Candidate], species: dict[str, dict[str, str]]) -> bool:
    for idx, candidate in enumerate(candidates, start=1):
        meta = species[candidate.annotation.code]
        clip_id = f"single_{idx:03d}_{slug(meta['Common Name'])}_{candidate.annotation.code}"
        if not valid_duration(SINGLES / f"{clip_id}.wav", CLIP_SECONDS):
            return False
    return True


def load_species() -> dict[str, dict[str, str]]:
    with SPECIES.open(newline="") as handle:
        species = {row["Species eBird Code"]: row for row in csv.DictReader(handle)}
    gbif_path = CACHE / "per_species_gbif_bbox.csv"
    if gbif_path.exists():
        with gbif_path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                if row["Species eBird Code"] in species:
                    species[row["Species eBird Code"]]["gbif_bbox_count"] = row.get("gbif_bbox_count", "")
    return species


def load_annotations() -> list[Annotation]:
    rows: list[Annotation] = []
    with ANNOTATIONS.open(newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                Annotation(
                    filename=row["Filename"],
                    start=float(row["Start Time (s)"]),
                    end=float(row["End Time (s)"]),
                    low_freq=int(row["Low Freq (Hz)"]),
                    high_freq=int(row["High Freq (Hz)"]),
                    code=row["Species eBird Code"],
                )
            )
    return rows


def select_candidates(annotations: list[Annotation]) -> list[Candidate]:
    by_file: dict[str, list[Annotation]] = defaultdict(list)
    counts = Counter()
    gbif_counts = load_gbif_counts()
    for ann in annotations:
        by_file[ann.filename].append(ann)
        counts[ann.code] += 1
    for anns in by_file.values():
        anns.sort(key=lambda ann: ann.start)

    priority = {code: idx for idx, code in enumerate(PRIORITY_CODES)}
    candidates: list[Candidate] = []
    for ann in annotations:
        if ann.code == "????":
            continue
        if gbif_counts.get(ann.code) == 0:
            continue
        midpoint = (ann.start + ann.end) / 2
        clip_start = max(0.0, midpoint - CLIP_SECONDS / 2)
        clip_end = clip_start + CLIP_SECONDS
        if clip_end > 3599.0:
            clip_end = 3599.0
            clip_start = clip_end - CLIP_SECONDS
        isolated = is_isolated(ann, by_file[ann.filename], clip_start, clip_end)
        if not isolated:
            continue
        candidates.append(
            Candidate(
                annotation=ann,
                clip_start=clip_start,
                clip_end=clip_end,
                isolated=isolated,
                priority_rank=priority.get(ann.code, 999),
                annotation_count=counts[ann.code],
            )
        )

    candidates.sort(
        key=lambda item: (
            item.priority_rank,
            -item.annotation_count,
            item.annotation.filename,
            item.annotation.start,
        )
    )
    selected: list[Candidate] = []
    per_species = Counter()
    seen_windows: set[tuple[str, int]] = set()
    for candidate in candidates:
        if per_species[candidate.annotation.code] >= MAX_PER_SPECIES:
            continue
        window_key = (candidate.annotation.filename, int(candidate.clip_start))
        if window_key in seen_windows:
            continue
        selected.append(candidate)
        per_species[candidate.annotation.code] += 1
        seen_windows.add(window_key)
        if len(selected) >= TARGET_SINGLES:
            break
    return selected


def load_gbif_counts() -> dict[str, int]:
    path = CACHE / "per_species_gbif_bbox.csv"
    if not path.exists():
        return {}
    counts: dict[str, int] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                counts[row["Species eBird Code"]] = int(row.get("gbif_bbox_count", ""))
            except ValueError:
                continue
    return counts


def is_isolated(target: Annotation, anns: list[Annotation], clip_start: float, clip_end: float) -> bool:
    for other in anns:
        if other.code == target.code:
            continue
        if other.end <= clip_start or other.start >= clip_end:
            continue
        return False
    return True


def extract_soundscapes(filenames: list[str]) -> None:
    existing = {path.name for path in EXTRACTED.glob("*.flac")}
    if all(filename in existing for filename in filenames):
        return
    with zipfile.ZipFile(ZIP_PATH) as archive:
        name_by_base = {Path(name).name: name for name in archive.namelist() if name.endswith(".flac")}
        for filename in filenames:
            target = EXTRACTED / filename
            if target.exists():
                continue
            member = name_by_base.get(filename)
            if member is None:
                raise RuntimeError(f"{filename} not found inside {ZIP_PATH}")
            with archive.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    print(f"Extracted/verified {len(filenames)} source soundscapes", flush=True)


def build_singles(candidates: list[Candidate], species: dict[str, dict[str, str]]) -> list[dict]:
    rows: list[dict] = []
    for idx, candidate in enumerate(candidates, start=1):
        code = candidate.annotation.code
        meta = species[code]
        clip_id = f"single_{idx:03d}_{slug(meta['Common Name'])}_{code}"
        output = SINGLES / f"{clip_id}.wav"
        source = EXTRACTED / candidate.annotation.filename
        if not valid_duration(output, CLIP_SECONDS):
            run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{candidate.clip_start:.3f}",
                    "-t",
                    f"{CLIP_SECONDS:.3f}",
                    "-i",
                    str(source),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "48000",
                    "-af",
                    "loudnorm=I=-23:TP=-2:LRA=11",
                    str(output),
                ]
            )
        if idx % 10 == 0:
            print(f"Prepared {idx} single-bird clips", flush=True)
        row = base_row(clip_id, "single", output)
        row.update(
            {
                "expected_common_names": meta["Common Name"],
                "expected_ebird_codes": code,
                "expected_scientific_names": meta["Scientific Name"],
                "gbif_bbox_count": meta.get("gbif_bbox_count", ""),
                "source_filename": candidate.annotation.filename,
                "source_start_s": f"{candidate.clip_start:.3f}",
                "source_end_s": f"{candidate.clip_end:.3f}",
                "components_json": json.dumps(
                    [
                        {
                            "offset_s": 0.0,
                            "common_name": meta["Common Name"],
                            "scientific_name": meta["Scientific Name"],
                            "ebird_code": code,
                            "source_filename": candidate.annotation.filename,
                            "source_start_s": round(candidate.clip_start, 3),
                            "source_end_s": round(candidate.clip_end, 3),
                        }
                    ],
                    sort_keys=True,
                ),
                "notes": "single-species window selected because no other annotated species overlapped this 10 s cut",
            }
        )
        rows.append(row)
    return rows


def build_mixes(single_rows: list[dict], species: dict[str, dict[str, str]]) -> list[dict]:
    random.seed(RANDOM_SEED)
    by_code: dict[str, list[dict]] = defaultdict(list)
    for row in single_rows:
        by_code[row["expected_ebird_codes"]].append(row)
    codes = sorted(by_code)
    if len(codes) < 3:
        return []

    rows: list[dict] = []
    for idx in range(1, TARGET_MIXES + 1):
        component_count = 2 if idx % 3 else 3
        chosen_codes = random.sample(codes, component_count)
        chosen = [random.choice(by_code[code]) for code in chosen_codes]
        offsets = [0.0, 2.8, 5.6][:component_count]
        clip_id = f"mix_{idx:03d}_{'_'.join(chosen_codes)}"
        output = MIXES / f"{clip_id}.wav"
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=48000:cl=mono:d={MIX_SECONDS}",
        ]
        for row in chosen:
            command.extend(["-i", row["path"]])
        filter_parts = []
        labels = ["[silence]"]
        filter_parts.append("[0:a]anull[silence]")
        for input_idx, (row, offset) in enumerate(zip(chosen, offsets)):
            delay_ms = int(offset * 1000)
            label = f"a{input_idx}"
            gain = 0.82 if input_idx == 0 else 0.72
            filter_parts.append(f"[{input_idx + 1}:a]adelay={delay_ms}|{delay_ms},volume={gain}[{label}]")
            labels.append(f"[{label}]")
        filter_parts.append(
            f"{''.join(labels)}amix=inputs={component_count + 1}:duration=longest:normalize=1,"
            f"atrim=0:{MIX_SECONDS}[out]"
        )
        command.extend(["-filter_complex", ";".join(filter_parts), "-map", "[out]", "-ac", "1", "-ar", "48000", str(output)])
        if not valid_duration(output, MIX_SECONDS):
            run_ffmpeg(command)
        if idx % 10 == 0:
            print(f"Prepared {idx} mixed-species clips", flush=True)

        common_names = [row["expected_common_names"] for row in chosen]
        scientific_names = [row["expected_scientific_names"] for row in chosen]
        components = []
        for row, offset in zip(chosen, offsets):
            single_component = json.loads(row["components_json"])[0]
            single_component["offset_s"] = offset
            single_component["single_clip_id"] = row["clip_id"]
            single_component["single_path"] = row["path"]
            components.append(single_component)
        row = base_row(clip_id, "mix", output)
        row.update(
            {
                "expected_common_names": "; ".join(common_names),
                "expected_ebird_codes": "; ".join(chosen_codes),
                "expected_scientific_names": "; ".join(scientific_names),
                "gbif_bbox_count": "; ".join(species[code].get("gbif_bbox_count", "") for code in chosen_codes),
                "source_filename": "; ".join(component["source_filename"] for component in components),
                "source_start_s": "",
                "source_end_s": "",
                "components_json": json.dumps(components, sort_keys=True),
                "notes": f"synthetic {component_count}-species overlap mixed from labeled single clips",
            }
        )
        rows.append(row)
    return rows


def base_row(clip_id: str, kind: str, output: Path) -> dict:
    return {
        "clip_id": clip_id,
        "kind": kind,
        "path": str(output.relative_to(ROOT)),
        "duration_s": f"{duration(output):.3f}" if output.exists() else "",
        "expected_common_names": "",
        "expected_ebird_codes": "",
        "expected_scientific_names": "",
        "gbif_bbox_count": "",
        "components_json": "",
        "source_dataset": SOURCE_DATASET,
        "source_doi": SOURCE_DOI,
        "source_url": SOURCE_URL,
        "source_license": SOURCE_LICENSE,
        "source_filename": "",
        "source_start_s": "",
        "source_end_s": "",
        "notes": "",
    }


def write_manifests(rows: list[dict]) -> None:
    columns = [
        "clip_id",
        "kind",
        "path",
        "duration_s",
        "expected_common_names",
        "expected_ebird_codes",
        "expected_scientific_names",
        "gbif_bbox_count",
        "components_json",
        "source_dataset",
        "source_doi",
        "source_url",
        "source_license",
        "source_filename",
        "source_start_s",
        "source_end_s",
        "notes",
    ]
    manifest_csv = MANIFESTS / "bird_playback_manifest.csv"
    with manifest_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    with (MANIFESTS / "bird_playback_manifest.json").open("w") as handle:
        json.dump(rows, handle, indent=2)
    shuffled = list(rows)
    random.seed(RANDOM_SEED)
    random.shuffle(shuffled)
    with (MANIFESTS / "playback_order.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["playback_index"] + columns)
        writer.writeheader()
        for idx, row in enumerate(shuffled, start=1):
            writer.writerow({"playback_index": idx, **row})


def write_summary(rows: list[dict], single_rows: list[dict], mix_rows: list[dict]) -> None:
    total_seconds = sum(float(row["duration_s"]) for row in rows if row["duration_s"])
    species = sorted({code for row in single_rows for code in row["expected_ebird_codes"].split("; ")})
    lines = [
        "# Bird Playback Test Library",
        "",
        f"Generated clips: {len(rows)}",
        f"Single-species clips: {len(single_rows)}",
        f"Mixed-species clips: {len(mix_rows)}",
        f"Total duration: {format_seconds(total_seconds)}",
        f"Species represented in singles: {len(species)}",
        "",
        "Use `manifests/playback_order.csv` when playing clips into the Pi microphone.",
        "The expected label(s) are in `expected_common_names`, `expected_ebird_codes`, and `components_json`.",
        "",
        "Source: A collection of fully-annotated soundscape recordings from the Southwestern Amazon Basin.",
        "DOI: 10.5281/zenodo.7079124",
        "License: CC BY 4.0",
        "",
        "Caveat: this is a strong neotropical validation set, not a final Pantanal site checklist.",
        "Regenerate or annotate a deployment-specific species list once BirdNET Analyzer is installed on the Pi.",
        "",
        "Species in single clips:",
    ]
    by_code = defaultdict(list)
    for row in single_rows:
        by_code[row["expected_ebird_codes"]].append(row["expected_common_names"])
    for code in species:
        lines.append(f"- {code}: {by_code[code][0]} ({len(by_code[code])} clips)")
    (BASE / "README.md").write_text("\n".join(lines) + "\n")


def run_ffmpeg(command: list[str]) -> None:
    proc = subprocess.run(command, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"ffmpeg exited {proc.returncode}").strip())


def duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(proc.stdout.strip())


def valid_duration(path: Path, expected: float, tolerance: float = 0.25) -> bool:
    if not path.exists():
        return False
    try:
        return abs(duration(path) - expected) <= tolerance
    except Exception:
        return False


def format_seconds(seconds: float) -> str:
    minutes = math.floor(seconds / 60)
    remain = seconds - minutes * 60
    return f"{minutes} min {remain:.1f} sec"


def slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_").replace("__", "_")


if __name__ == "__main__":
    main()
