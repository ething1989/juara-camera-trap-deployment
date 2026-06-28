#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import signal
import sys
import time


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def print_line(message: str) -> None:
    print(f"{timestamp()} {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch a PIR motion detector GPIO pin and print events immediately."
    )
    parser.add_argument("--pin", type=int, default=4, help="BCM GPIO pin to watch. Default: 4")
    parser.add_argument(
        "--active-low",
        action="store_true",
        help="Treat LOW as motion instead of HIGH.",
    )
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Exit automatically after this many seconds. Default: run until Ctrl-C.",
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
            bounce_time=0.02,
        )
    except Exception as exc:
        print(
            f"{timestamp()} ERROR cannot open GPIO{args.pin}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        print(
            "Stop juara-station.service while testing so the main station releases the motion pin.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    start = time.monotonic()
    deadline = start + args.seconds if args.seconds is not None else None
    event_count = 0
    last_motion = None

    def state_name() -> str:
        is_motion = not sensor.is_active if args.active_low else sensor.is_active
        return "MOTION" if is_motion else "clear"

    def motion() -> None:
        nonlocal event_count, last_motion
        now = time.monotonic()
        event_count += 1
        gap = "first trigger" if last_motion is None else f"{now - last_motion:.3f}s since last trigger"
        last_motion = now
        print_line(f"TRIGGER #{event_count} - {gap}")

    def clear() -> None:
        return

    if args.active_low:
        sensor.when_deactivated = motion
        sensor.when_activated = clear
    else:
        sensor.when_activated = motion
        sensor.when_deactivated = clear

    try:
        active_note = "LOW=motion" if args.active_low else "HIGH=motion"
        print_line(f"Watching GPIO{args.pin} ({active_note}); waiting for triggers. Press Ctrl-C to stop.")
        while not stop:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            time.sleep(0.05)
    finally:
        sensor.close()
        print_line(f"STOPPED GPIO{args.pin}; motions={event_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
