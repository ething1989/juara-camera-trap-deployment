# Current Camera Trap Deployment

This repository is the clean install source for the current June 2026 motion camera trap.

## Hardware

- Raspberry Pi Zero 2 W on Raspberry Pi OS Legacy 64-bit Lite.
- PIR motion detector on BCM GPIO 26.
- IR flash output on BCM GPIO 6.
- Raspberry Pi camera using Picamera2.
- USB microphone using ALSA `arecord`.
- DS3231 RTC on I2C.
- GPS through `gpsd` / `gpspipe`.
- BME280 on I2C address `0x76`.
- VEML7700 on I2C address `0x10`.
- USB thumb drive mounted at `/mnt/juara_usb`.

## Storage Layout

- Main CSV: `/mnt/juara_usb/2026junecameratrap.csv`
- Photos: `/mnt/juara_usb/Photos`
- SQLite source-of-truth journal: `/var/lib/juara-station/state/station.sqlite3`
- Temporary audio recordings: `/var/lib/juara-station/audio_recordings`
- Temporary AI work: `/tmp/juara-ai-work`

Audio recordings are not saved to the USB. The station records temporary WAV files, processes them with BirdNET, exports the results to the CSV, and deletes the audio after processing or recovery cleanup.

## Camera Behavior

- The camera uses Picamera2 and stays warm so `rpicam-still` startup delay is avoided.
- Motion photos are triggered only by the PIR.
- Day mode uses auto exposure with exposure value `4.0`.
- Night mode is 6 PM to 6 AM.
- At night, the flash turns on immediately when motion trips, the station targets the photo for 0.5 seconds after the trigger, and the flash stays on 0.5 seconds after capture.
- Image AI is disabled. Photos are saved for later review.

## BirdNET Behavior

- BirdNET Analyzer version: `2.4.0`.
- Audio gain before analysis: `36 dB`.
- Minimum detection confidence: `0.25`.
- Candidate bird threshold for secondary/third options: `0.10`.
- Sensitivity: day `1.0`, night `0.8`.
- Chunk overlap: day `0.0`, night `0.0`.
- Workers: `1`.
- Batch size: `1`.
- Fast TFLite mode: enabled.
- Species-frequency threshold: `0.005`.
- Up to 90 call cells are exported in the main CSV.
- The included BirdNET species pack lets the Pi rebuild the active species list after GPS gives 10 consistent fixes.

## Scheduling And Recovery

- Main interval: 5 minutes.
- Sensor samples: every 20 seconds, averaged into the interval.
- Audio recording pause: midnight to 4 AM.
- Audio backlog purge: 3:45 AM.
- Planned reboots: midnight, 4 AM, noon, and 8 PM.
- Unexpected power loss is logged after the next boot.
- SQLite is the source of truth; CSV is rebuilt from SQLite exports.
- USB missing watchdog reboots the Pi if the USB stays missing for 10 minutes.

## Fresh Pi Install

On a new Pi after SSH works:

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
sudo journalctl -u juara-station.service -u juara-ai-worker.service -f
```

## Motion Detector Sensitivity Test

Stop the services so the test owns GPIO 26:

```bash
sudo systemctl stop juara-station.service juara-ai-worker.service
juara-motion-trips
```

Restart deployment mode:

```bash
sudo systemctl start juara-station.service juara-ai-worker.service
```
