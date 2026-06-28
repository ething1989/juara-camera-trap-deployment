#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import shutil
import subprocess
import time
import wave
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from juara_station.ai import BirdNetRunner, birdnet_week
from juara_station.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Run BirdNET speed/accuracy matrix on the Pi.")
    parser.add_argument("--config", default="/etc/juara-station.toml")
    parser.add_argument("--work", default="/tmp/juara_birdnet_matrix")
    parser.add_argument("--clean-input", default="/tmp/juara_clean_flat")
    parser.add_argument(
        "--captured-source",
        default="/mnt/juara_usb/juara/test_runs/playback_20260512_110641/birdnet_input/playback_20260512_110641.wav",
    )
    parser.add_argument("--captured-chunk-seconds", type=float, default=300.0)
    args = parser.parse_args()

    work = Path(args.work)
    clean_input = Path(args.clean_input)
    captured_source = Path(args.captured_source)
    captured_input = work / "captured_input"
    summary_path = work / "matrix_summary.jsonl"

    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    split_audio(captured_source, captured_input, args.captured_chunk_seconds)

    station_config = load_config(args.config)
    week = birdnet_week(datetime.now(timezone.utc))
    tests = [
        ("clean_conf025_ov0", clean_input, 0.25, 0.0),
        ("clean_conf015_ov0", clean_input, 0.15, 0.0),
        ("clean_conf010_ov0", clean_input, 0.10, 0.0),
        ("captured_conf025_ov0", captured_input, 0.25, 0.0),
        ("captured_conf015_ov0", captured_input, 0.15, 0.0),
        ("captured_conf010_ov0", captured_input, 0.10, 0.0),
        ("clean_conf015_ov15", clean_input, 0.15, 1.5),
    ]

    with summary_path.open("w") as summary:
        for name, input_path, min_conf, overlap in tests:
            birdnet_config = replace(
                station_config.birdnet,
                use_subprocess=False,
                fast_tflite=False,
                batch_size=1,
                min_confidence=min_conf,
                overlap_day=overlap,
                keep_work_outputs=True,
            )
            runner = BirdNetRunner(birdnet_config, station_config.location)
            output_dir = work / name
            output_dir.mkdir(parents=True, exist_ok=True)
            event = {
                "event": "start",
                "name": name,
                "input": str(input_path),
                "min_confidence": min_conf,
                "overlap": overlap,
                "week": week,
            }
            print(json.dumps(event), flush=True)
            summary.write(json.dumps(event) + "\n")
            summary.flush()

            start = time.monotonic()
            error = None
            try:
                runner._analyze_with_birdnet(input_path, output_dir, week, night=False, timeout=7200)
            except Exception as exc:
                error = repr(exc)
            elapsed = time.monotonic() - start
            result = {
                "event": "finish",
                "name": name,
                "seconds": round(elapsed, 3),
                "csv_count": len(list(output_dir.glob("*BirdNET.results.csv"))),
                "detection_rows": count_detection_rows(output_dir),
                "error": error,
            }
            print(json.dumps(result), flush=True)
            summary.write(json.dumps(result) + "\n")
            summary.flush()
            gc.collect()
    return 0


def split_audio(source: Path, output_dir: Path, chunk_seconds: float) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = wav_duration(source)
    chunks = []
    offset = 0.0
    index = 0
    while offset < duration:
        target = output_dir / f"captured_{index:03d}_{int(offset):05d}.wav"
        make_chunk(source, target, offset, min(chunk_seconds, duration - offset))
        chunks.append(target)
        offset += chunk_seconds
        index += 1
    return chunks


def make_chunk(source: Path, target: Path, offset: float, duration: float) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(offset),
        "-t",
        str(duration),
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "48000",
        "-sample_fmt",
        "s16",
        str(target),
    ]
    subprocess.run(command, check=True, timeout=180)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def count_detection_rows(output_dir: Path) -> int:
    total = 0
    for path in output_dir.glob("*BirdNET.results.csv"):
        with path.open() as handle:
            total += max(0, sum(1 for _line in handle) - 1)
    return total


if __name__ == "__main__":
    raise SystemExit(main())
