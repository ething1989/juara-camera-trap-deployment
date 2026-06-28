from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from .ai import SpeciesNetRunner
from .config import LocationConfig, SpeciesNetConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated Juara SpeciesNet photo worker")
    parser.add_argument("--photo", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--speciesnet-config-json", required=True)
    parser.add_argument("--location-config-json", required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    output_json = Path(args.output_json)
    try:
        speciesnet_config = SpeciesNetConfig(**json.loads(args.speciesnet_config_json))
        speciesnet_config = _worker_speciesnet_config(speciesnet_config)
        location_config = LocationConfig(**json.loads(args.location_config_json))
        runner = SpeciesNetRunner(speciesnet_config, location_config)
        prediction = runner.analyze_photo(Path(args.photo), output_json.parent / "work")
        payload = {
            "ok": True,
            "label": prediction.label,
            "confidence": prediction.confidence,
            "blank": prediction.blank,
            "raw": prediction.raw,
        }
        output_json.write_text(json.dumps(payload))
        return 0
    except Exception as exc:
        logging.exception("SpeciesNet worker failed")
        output_json.write_text(json.dumps({"ok": False, "error": str(exc)}))
        return 1


def _worker_speciesnet_config(config: SpeciesNetConfig) -> SpeciesNetConfig:
    values = config.__dict__.copy()
    values["isolated_process"] = False
    values["keep_classifier_loaded"] = False
    return SpeciesNetConfig(**values)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
