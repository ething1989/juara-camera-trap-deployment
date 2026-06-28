#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json
import sys

from juara_station.config import load_config


def _url_to_filename(url: str) -> str:
    filename = url.split("?", 1)[0]
    return filename.replace(":", "_").replace("/", "_")


def main() -> int:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/etc/juara-station.toml")
    config = load_config(config_path)
    if not config.speciesnet.model_path:
        print("speciesnet.model_path is not set; runtime may use the remote default model.")
        return 1
    model_path = Path(config.speciesnet.model_path).expanduser()
    if not model_path.exists():
        print(f"SpeciesNet model folder is missing: {model_path}")
        return 1

    info_path = model_path / "info.json"
    if not info_path.exists():
        print(f"SpeciesNet info.json is missing: {info_path}")
        return 1
    info = json.loads(info_path.read_text())
    detector = info["detector"]
    if detector.startswith(("http://", "https://")):
        detector = _url_to_filename(detector)

    required = [
        info["classifier"],
        info["classifier_labels"],
        detector,
        info["taxonomy"],
        info["geofence"],
        "info.json",
    ]
    missing = []
    print(f"SpeciesNet model folder: {model_path}")
    for name in required:
        path = model_path / name
        size = path.stat().st_size if path.exists() else 0
        print(f"{name}: {size} bytes")
        if size <= 0:
            missing.append(path)

    target_path = Path(config.speciesnet.target_species_txt or "").expanduser()
    if target_path:
        if not target_path.exists():
            print(f"Target species file missing: {target_path}")
            missing.append(target_path)
        else:
            labels = set((model_path / info["classifier_labels"]).read_text().splitlines())
            targets = [line for line in target_path.read_text().splitlines() if line.strip()]
            unknown = [line for line in targets if line not in labels]
            print(f"Target species: {len(targets)} labels, {len(unknown)} unknown")
            if unknown:
                missing.append(target_path)

    if missing:
        print("SpeciesNet cache is NOT ready.")
        return 1
    print("SpeciesNet cache is ready; runtime should not need downloads.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
