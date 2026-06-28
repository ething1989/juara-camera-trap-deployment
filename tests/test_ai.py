from pathlib import Path
import json
import os
import subprocess
import sys
import time

from juara_station.ai import (
    SpeciesNetRunner,
    SpeciesNetUnavailableError,
    _is_blank_prediction,
    _speciesnet_best_result,
    birdnet_week,
    parse_birdnet_calls,
    parse_birdnet_csv,
    _run_speciesnet_command,
)
import juara_station.ai as ai_module
from juara_station.config import LocationConfig, SpeciesNetConfig
from datetime import datetime, timezone


def test_birdnet_week_uses_four_weeks_per_month():
    assert birdnet_week(datetime(2026, 5, 1, tzinfo=timezone.utc)) == 17
    assert birdnet_week(datetime(2026, 5, 31, tzinfo=timezone.utc)) == 20


def test_parse_birdnet_csv_groups_calls(tmp_path: Path):
    csv_path = tmp_path / "detections.csv"
    csv_path.write_text(
        "Start (s),End (s),Scientific name,Common name,Confidence\n"
        "0,3,Anodorhynchus hyacinthinus,Hyacinth macaw,0.8\n"
        "0,3,Ara ararauna,Blue-and-yellow macaw,0.12\n"
        "3,6,Anodorhynchus hyacinthinus,Hyacinth macaw,0.6\n"
        "6,9,Furnarius rufus,Rufous hornero,0.5\n"
        "9,12,Noise bird,Noise bird,0.11\n"
    )

    detections = parse_birdnet_csv(csv_path)
    calls = parse_birdnet_calls(csv_path)

    assert detections[0].species == "Hyacinth macaw"
    assert detections[0].calls == 2
    assert detections[0].avg_confidence == 0.7
    assert detections[1].species == "Rufous hornero"
    assert len(calls) == 3
    assert calls[0].candidates[0].species == "Hyacinth macaw"
    assert calls[0].candidates[1].species == "Blue-and-yellow macaw"
    assert calls[0].candidates[1].confidence == 0.12


def test_speciesnet_uses_specific_classifier_label_when_ensemble_is_generic():
    prediction = {
        "prediction": "f2efdae9-efb8-48fb-8a91-eccf79ab4ffb;no cv result;no cv result;no cv result;no cv result;no cv result;no cv result",
        "prediction_score": 0.52,
        "classifications": {
            "classes": [
                "uuid;mammalia;rodentia;cuniculidae;cuniculus;paca;spotted paca",
                "f1856211-cfb7-4a5b-9158-c0f72fd09ee6;;;;;;blank",
            ],
            "scores": [0.52, 0.34],
        },
        "detections": [{"label": "animal", "conf": 0.408}],
    }

    label, confidence = _speciesnet_best_result(prediction, SpeciesNetConfig())

    assert label == "spotted paca"
    assert confidence == 0.52
    assert not _is_blank_prediction(prediction, SpeciesNetConfig())


def test_speciesnet_deletes_weak_generic_prediction_as_blank():
    prediction = {
        "prediction": "f2efdae9-efb8-48fb-8a91-eccf79ab4ffb;no cv result;no cv result;no cv result;no cv result;no cv result;no cv result",
        "prediction_score": 0.3589,
        "classifications": {
            "classes": [
                "f1856211-cfb7-4a5b-9158-c0f72fd09ee6;;;;;;blank",
                "uuid;reptilia;squamata;;;;lizards and snakes",
                "uuid;reptilia;squamata;teiidae;tupinambis;teguixin;black tegu",
            ],
            "scores": [0.3589, 0.2449, 0.1588],
        },
        "detections": [{"label": "animal", "conf": 0.1164}],
    }

    assert _is_blank_prediction(prediction, SpeciesNetConfig())


def test_speciesnet_replaces_broad_animal_label_with_classifier_species():
    prediction = {
        "prediction": "uuid;mammalia;;;;;animal",
        "prediction_score": 0.825,
        "classifications": {
            "classes": [
                "uuid;mammalia;carnivora;procyonidae;procyon;cancrivorus;crab-eating raccoon",
                "uuid;mammalia;carnivora;mustelidae;melogale;moschata;small-toothed ferret badger",
            ],
            "scores": [0.533, 0.066],
        },
        "detections": [{"label": "animal", "conf": 0.825}],
    }

    label, confidence = _speciesnet_best_result(prediction, SpeciesNetConfig())

    assert label == "crab-eating raccoon"
    assert confidence == 0.533


def test_speciesnet_prefers_regional_target_scores_when_available():
    prediction = {
        "prediction": "uuid;mammalia;artiodactyla;bovidae;bos;taurus;domestic cattle",
        "prediction_score": 0.41,
        "classifications": {
            "classes": [
                "uuid;mammalia;artiodactyla;bovidae;bos;taurus;domestic cattle",
                "f1856211-cfb7-4a5b-9158-c0f72fd09ee6;;;;;;blank",
            ],
            "scores": [0.41, 0.2],
            "target_classes": [
                "9b7fea59-cfa3-499c-a420-bcf36277dcd8;mammalia;carnivora;felidae;panthera;onca;jaguar"
            ],
            "target_scores": [0.31],
        },
    }

    label, confidence = _speciesnet_best_result(prediction, SpeciesNetConfig())

    assert label == "jaguar"
    assert confidence == 0.31


def test_speciesnet_runs_on_isolated_single_photo_folder(tmp_path: Path, monkeypatch):
    photo_dir = tmp_path / "photos"
    photo_dir.mkdir()
    photo = photo_dir / "target.jpg"
    photo.write_bytes(b"jpg")
    (photo_dir / "other.jpg").write_bytes(b"other")
    commands = []

    def fake_run(command, predictions_json, timeout):  # noqa: ARG001
        commands.append(command)
        input_dir = Path(command[command.index("--folders") + 1])
        assert input_dir != photo_dir
        assert sorted(path.name for path in input_dir.iterdir()) == ["target.jpg"]
        input_photo = input_dir / photo.name
        if input_photo.is_symlink():
            assert Path(os.readlink(input_photo)).is_absolute()
        predictions_json.write_text(
            json.dumps(
                {
                    "predictions": [
                        {
                            "filepath": str(input_dir / photo.name),
                            "prediction": "uuid;mammalia;carnivora;felidae;leopardus;pardalis;ocelot",
                            "prediction_score": 0.91,
                            "detections": [{"label": "animal", "conf": 0.95}],
                        }
                    ]
                }
            )
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ai_module, "_run_speciesnet_command", fake_run)

    model_dir = tmp_path / "speciesnet-model"
    target_species = tmp_path / "targets.txt"
    runner = SpeciesNetRunner(
        SpeciesNetConfig(
            model_path=str(model_dir),
            target_species_txt=str(target_species),
            classifier_only=True,
            direct_classifier=False,
            batch_size=1,
        ),
        LocationConfig(country="BRA", admin1_region="BR-MT"),
    )
    prediction = runner.analyze_photo(photo, tmp_path / "work")

    assert prediction.label == "ocelot"
    assert prediction.confidence == 0.91
    assert commands
    command = commands[0]
    assert command[command.index("--model") + 1] == str(model_dir)
    assert command[command.index("--target_species_txt") + 1] == str(target_species)
    assert command[command.index("--batch_size") + 1] == "1"
    assert "--classifier_only" in command
    assert "--noprogress_bars" in command
    assert not (tmp_path / "work").exists()


def test_speciesnet_direct_classifier_runs_in_worker_process(tmp_path: Path, monkeypatch):
    photo = tmp_path / "target.jpg"
    photo.write_bytes(b"jpg")
    commands = []

    def fake_worker(command, output_json, timeout, threads, nice=0, memory_limit_mb=0):
        commands.append((command, output_json, timeout, threads, nice, memory_limit_mb))
        output_json.write_text(
            json.dumps(
                {
                    "ok": True,
                    "label": "jaguar",
                    "confidence": 0.997,
                    "blank": False,
                    "raw": {
                        "prediction": "uuid;mammalia;carnivora;felidae;panthera;onca;jaguar",
                        "prediction_score": 0.997,
                    },
                }
            )
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ai_module, "_run_speciesnet_worker_command", fake_worker)
    runner = SpeciesNetRunner(
        SpeciesNetConfig(
            classifier_only=True,
            direct_classifier=True,
            isolated_process=True,
            subprocess_timeout_seconds=321,
            subprocess_threads=2,
            keep_work_outputs=False,
        ),
        LocationConfig(country="BRA", admin1_region="BR-MT"),
    )

    prediction = runner.analyze_photo(photo, tmp_path / "work")

    assert prediction.label == "jaguar"
    assert prediction.confidence == 0.997
    assert prediction.blank is False
    assert prediction.raw["isolated_worker"]["timeout_seconds"] == 321
    command, output_json, timeout, threads, nice, memory_limit_mb = commands[0]
    assert command[:3] == [sys.executable, "-m", "juara_station.speciesnet_worker"]
    assert command[command.index("--photo") + 1] == str(photo)
    assert output_json.name == "speciesnet_worker_result.json"
    assert timeout == 321
    assert threads == 2
    assert nice == 15
    assert memory_limit_mb == 384
    assert not (tmp_path / "work").exists()


def test_speciesnet_memory_guard_skips_classifier_before_torch_load(tmp_path: Path):
    photo = tmp_path / "target.jpg"
    photo.write_bytes(b"jpg")
    runner = SpeciesNetRunner(
        SpeciesNetConfig(
            classifier_only=True,
            direct_classifier=True,
            isolated_process=False,
            blank_precheck_enabled=False,
            min_classifier_available_memory_mb=999999,
        ),
        LocationConfig(country="BRA", admin1_region="BR-MT"),
    )

    try:
        runner.analyze_photo(photo, tmp_path / "work")
    except SpeciesNetUnavailableError as exc:
        assert "memory guard" in str(exc)
    else:
        raise AssertionError("SpeciesNet classifier should have been skipped by the memory guard")


def test_speciesnet_command_stops_after_predictions_json(tmp_path: Path):
    predictions_json = tmp_path / "predictions.json"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import sys, time; "
            "Path(sys.argv[1]).write_text('{\"predictions\": []}'); "
            "time.sleep(30)"
        ),
        str(predictions_json),
    ]

    start = time.monotonic()
    proc = _run_speciesnet_command(command, predictions_json, timeout=60)

    assert proc.returncode == 0
    assert time.monotonic() - start < 10
