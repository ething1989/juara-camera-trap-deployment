#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import signal
import sys
import time


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(message: str) -> None:
    print(f"{timestamp()} {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print one line every time the PIR motion detector trips."
    )
    parser.add_argument(
        "--pin",
        type=int,
        default=26,
        help="BCM GPIO pin to watch. Default: 26.",
    )
    parser.add_argument(
        "--active-low",
        action="store_true",
        help="Treat LOW as tripped instead of HIGH.",
    )
    parser.add_argument(
        "--bounce",
        type=float,
        default=0.02,
        help="Debounce time in seconds. Default: 0.02.",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Run for this many seconds, then exit. Default: run until Ctrl-C.",
    )
    parser.add_argument(
        "--show-clear",
        action="store_true",
        help="Also print when the detector returns to clear.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stop = False

    def request_stop(signum, frame) -> None:  # noqa: ARG001
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        from gpiozero import DigitalInputDevice
    except Exception as exc:
        print(f"{timestamp()} ERROR gpiozero is not available: {exc}", file=sys.stderr, flush=True)
        return 2

    try:
        sensor = DigitalInputDevice(
            args.pin,
            pull_up=False,
            bounce_time=max(0.0, args.bounce),
        )
    except Exception as exc:
        print(
            f"{timestamp()} ERROR cannot open GPIO{args.pin}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        print(
            "Stop station services first: sudo systemctl stop juara-station.service juara-ai-worker.service",
            file=sys.stderr,
            flush=True,
        )
        return 2

    count = 0
    last_trip: float | None = None

    def tripped() -> None:
        nonlocal count, last_trip
        now = time.monotonic()
        count += 1
        gap = "first trigger" if last_trip is None else f"{now - last_trip:.3f}s since last trigger"
        last_trip = now
        log(f"TRIPPED #{count} ({gap})")

    def clear() -> None:
        if args.show_clear:
            log("clear")

    if args.active_low:
        sensor.when_deactivated = tripped
        sensor.when_activated = clear
        active_note = "LOW=tripped"
    else:
        sensor.when_activated = tripped
        sensor.when_deactivated = clear
        active_note = "HIGH=tripped"

    deadline = time.monotonic() + args.seconds if args.seconds is not None else None
    log(f"watching GPIO{args.pin} ({active_note}); press Ctrl-C to stop")

    try:
        while not stop:
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    finally:
        sensor.close()
        log(f"stopped; triggers={count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
