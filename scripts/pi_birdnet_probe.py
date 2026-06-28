#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import wave
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from juara_station.ai import BirdNetAudioJob, BirdNetRunner, birdnet_week, parse_birdnet_csv
from juara_station.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Juara BirdNET warm/hot speed on the Pi.")
    parser.add_argument("--config", default="/etc/juara-station.toml")
    parser.add_argument("--source", default="/mnt/juara_usb/juara/test_runs/playback_20260512_110641/captured.wav")
    parser.add_argument(
        "--prepared-source",
        default="/mnt/juara_usb/juara/test_runs/playback_20260512_110641/birdnet_input/playback_20260512_110641.wav",
    )
    parser.add_argument("--work", default="/tmp/juara_birdnet_probe")
    parser.add_argument("--chunk-duration", type=float, default=30.0)
    parser.add_argument("--chunk-offsets", default="0,300,900")
    parser.add_argument("--overlap", type=float, default=0.0)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--fast-tflite", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--min-confidence", type=float, default=0.25)
    parser.add_argument("--full", action="store_true", help="Also run the prepared full playback file.")
    parser.add_argument("--full-chunk-seconds", type=float, default=300.0)
    args = parser.parse_args()

    source = Path(args.source)
    prepared_source = Path(args.prepared_source)
    work = Path(args.work)
    shutil.rmtree(work, ignore_errors=True)
    (work / "chunks").mkdir(parents=True, exist_ok=True)

    station_config = load_config(args.config)
    birdnet_config = replace(
        station_config.birdnet,
        use_subprocess=False,
        fast_tflite=args.fast_tflite,
        batch_size=args.batch_size,
        overlap_day=args.overlap,
        keep_work_outputs=True,
        min_confidence=args.min_confidence,
    )
    runner = BirdNetRunner(birdnet_config, station_config.location)
    recorded_at = datetime.now(timezone.utc)
    week = birdnet_week(recorded_at)

    print(
        json.dumps(
            {
                "event": "config",
                "source": str(source),
                "prepared_source": str(prepared_source),
                "work": str(work),
                "week": week,
                "overlap": birdnet_config.overlap_day,
                "batch_size": birdnet_config.batch_size,
                "fast_tflite": birdnet_config.fast_tflite,
                "use_subprocess": birdnet_config.use_subprocess,
            }
        ),
        flush=True,
    )

    offsets = [float(item.strip()) for item in args.chunk_offsets.split(",") if item.strip()]
    chunks = []
    for index, offset in enumerate(offsets, start=1):
        chunk = work / "chunks" / f"chunk_{index:02d}_{int(offset):04d}.wav"
        make_chunk(source, chunk, offset, args.chunk_duration)
        chunks.append(chunk)

    hot_results = []
    for index, chunk in enumerate(chunks, start=1):
        out = work / f"single_{index:02d}"
        start = time.monotonic()
        detections = runner.analyze_audio(chunk, out, recorded_at, night=False)
        elapsed = time.monotonic() - start
        result = {
            "event": "single",
            "index": index,
            "seconds": round(elapsed, 3),
            "audio_seconds": args.chunk_duration,
            "speed_ratio": round(elapsed / args.chunk_duration, 3),
            "calls": [call_to_dict(call) for call in detections[:8]],
        }
        hot_results.append(result)
        print(json.dumps(result), flush=True)

    batch_out = work / "batch"
    jobs = [
        BirdNetAudioJob(recorded_at.replace(minute=(recorded_at.minute + idx) % 60), chunk)
        for idx, chunk in enumerate(chunks)
    ]
    start = time.monotonic()
    batch_detections = runner.analyze_audio_batch(jobs, batch_out, week, night=False)
    elapsed = time.monotonic() - start
    print(
        json.dumps(
            {
                "event": "batch",
                "seconds": round(elapsed, 3),
                "files": len(chunks),
                "audio_seconds": round(args.chunk_duration * len(chunks), 3),
                "speed_ratio": round(elapsed / (args.chunk_duration * len(chunks)), 3),
                "calls_by_file": {key.isoformat(): [call_to_dict(call) for call in value[:8]] for key, value in batch_detections.items()},
            }
        ),
        flush=True,
    )

    if args.full and prepared_source.exists():
        full_input: Path
        if args.full_chunk_seconds > 0:
            full_input = work / "full_input"
            split_audio(prepared_source, full_input, args.full_chunk_seconds)
        else:
            full_input = prepared_source

        full_out = work / "full"
        start = time.monotonic()
        runner._analyze_with_birdnet(full_input, full_out, week, night=False, timeout=7200)
        elapsed = time.monotonic() - start
        csv_paths = sorted(full_out.glob("*.csv"))
        detection_rows = sum(max(0, count_csv_lines(path) - 1) for path in csv_paths)
        first_csv = next((path for path in csv_paths if path.name.endswith(".BirdNET.results.csv")), None)
        detections = parse_birdnet_csv(first_csv) if first_csv else []
        print(
            json.dumps(
                {
                    "event": "full",
                    "seconds": round(elapsed, 3),
                    "input": str(full_input),
                    "csv_dir": str(full_out),
                    "csv_count": len(csv_paths),
                    "detection_rows": detection_rows,
                    "detections": [d.__dict__ for d in detections[:20]],
                }
            ),
            flush=True,
        )

    return 0


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
    subprocess.run(command, check=True, timeout=120)


def split_audio(source: Path, output_dir: Path, chunk_seconds: float) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = wav_duration(source)
    chunks = []
    offset = 0.0
    index = 0
    while offset < duration:
        target = output_dir / f"full_{index:03d}_{int(offset):05d}.wav"
        make_chunk(source, target, offset, min(chunk_seconds, duration - offset))
        chunks.append(target)
        offset += chunk_seconds
        index += 1
    return chunks


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def count_csv_lines(path: Path) -> int:
    with path.open() as handle:
        return sum(1 for _line in handle)


def call_to_dict(call) -> dict:
    return {
        "start_seconds": call.start_seconds,
        "end_seconds": call.end_seconds,
        "candidates": [candidate.__dict__ for candidate in call.candidates],
    }


if __name__ == "__main__":
    raise SystemExit(main())
