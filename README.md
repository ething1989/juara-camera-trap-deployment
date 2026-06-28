# Juara Camera Trap Deployment

Clean Raspberry Pi Zero 2 W deployment source for the current June 2026 Juara motion camera trap.

This project is built around a durable SQLite journal plus CSV export. The Pi writes sensor samples, motion photo events, and BirdNET results into SQLite first, then atomically exports CSV files to the USB drive. Audio recordings are temporary work files outside the USB and are deleted after AI processing, so the station can recover after sudden power loss without filling the thumb drive.

## What It Does

- Logs one main CSV row per interval, default five minutes.
- Samples BME280, VEML7700, and Pi CPU temperature during the interval, then averages them.
- Records five-minute temporary audio WAV files with day/night quality modes, processes them with BirdNET, then deletes them.
- Runs BirdNET Analyzer with the deployment coordinates and week filter, then logs bird call counts, confidence, Shannon index, Simpson index, Pielou evenness, richness, total calls, total species, and top species.
- Logs up to 90 detailed bird call cells in the main CSV, with ranked candidate options stacked inside each call cell.
- Keeps the camera warm with Picamera2 so a PIR event on GPIO 26 can capture without `rpicam-still` startup delay.
- Turns GPIO 6 IR flash on immediately at night, targets the photo for 0.5 seconds after motion, and keeps flash on for 0.5 seconds after capture.
- Does not run image AI in the field build; captured photos are kept for later review.
- Keeps running when individual sensors, GPS, RTC, microphone, camera, or AI processing fail.

## Local Test

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/juara-station --mock --config configs/local.mock.toml once --duration 1 --simulate-motion
```

The mock run writes a CSV under `.local_run/usb/logs/`.

## Pi Install

Use Raspberry Pi OS Legacy 64-bit Lite for the deployment card. The 32-bit Trixie image can run the hardware logger, but the AI dependencies are much more likely to install cleanly on 64-bit Legacy/Bookworm.

After the new card boots and SSH works, install directly on the Pi:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/esmaby444/juara-camera-trap-deployment.git ~/juara-camera-trap-deployment
cd ~/juara-camera-trap-deployment
sudo scripts/install_june2026trap.sh
sudo reboot
```

After reboot:

```bash
sudo /home/$USER/juara-camera-trap-deployment/scripts/pi_preflight.sh
sudo systemctl status juara-station.service juara-ai-worker.service
```

The install script creates `/opt/juara-wildlife-station`, configures I2C/DS3231/GPSD, mounts the USB drive at `/mnt/juara_usb` using the `JUARA-CAM-1` label or the first detected USB partition, installs the station config at `/etc/juara-station.toml`, installs the services, disables apt daily update timers, and enables planned reboot checkpoints.

If you are deploying from a Mac to an SSH-accessible Pi instead:

```bash
PI_HOST=raspberrypi.local PI_USER=juara2026pi1 scripts/deploy_to_pi1.sh
```

Before field deployment, label or mount the USB drive at `/mnt/juara_usb`, then confirm `/etc/juara-station.toml` points at:

```toml
[storage]
root = "/mnt/juara_usb"
fallback_root = "/var/lib/juara-station"
recording_root = "/var/lib/juara-station/audio_recordings"
photos_subdir = "Photos"
```

If the USB is not writable, the service falls back to `/var/lib/juara-station` instead of crashing.

See `docs/current_camera_trap.md` for the exact deployed hardware pins, schedule, storage layout, and BirdNET settings.

## Model Choices

Bird audio uses BirdNET Analyzer because it supports latitude/longitude, week-of-year filtering, confidence thresholds, overlap control, and CSV output. This is a much better starting point than training a new bird model from scratch on a Pi Zero.

Image AI code remains in the repository as an optional experiment, but the deployment templates keep it disabled so the Pi only spends AI power on BirdNET.

See `docs/model_choices.md` for operational caveats.
