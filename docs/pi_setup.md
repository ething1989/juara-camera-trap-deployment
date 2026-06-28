# Pi Setup Notes

## Recommended Image

Use Raspberry Pi OS Legacy 64-bit Lite for the field card. The 32-bit Trixie image boots and runs the logger, but BirdNET currently has a friendlier install path on 64-bit Legacy/Bookworm.

Raspberry Pi Imager settings:

- OS: Raspberry Pi OS Legacy 64-bit Lite.
- Hostname: `raspberrypi.local` or the unit-specific hostname.
- User: `june2026trap` for the current June 2026 trap, or the unit-specific user.
- SSH: enabled.
- Wi-Fi SSID: `NetworkPi`.
- Wi-Fi password: `raspberry314`.
- Locale/timezone can be set in Imager, but the installer also forces `America/Cuiaba`.

## Hardware Assumptions

- PIR motion detector: BCM GPIO 26, physical pin 37.
- IR flash modules: BCM GPIO 6, physical pin 31.
- DS3231 RTC: I2C.
- BME280: I2C address `0x76` by default.
- VEML7700: I2C address `0x10` by default.
- GPS: available through `gpsd` / `gpspipe`.
- Camera: Raspberry Pi camera stack through Picamera2/libcamera.
- Audio: Adafruit USB microphone through ALSA/`arecord`.

## First Boot Checklist

1. Confirm the Pi is reachable on `raspberrypi.local`.
2. Deploy the current project from this Mac:
   ```bash
   sudo apt-get update
   sudo apt-get install -y git
   git clone https://github.com/esmaby444/juara-camera-trap-deployment.git ~/juara-camera-trap-deployment
   cd ~/juara-camera-trap-deployment
   sudo scripts/install_june2026trap.sh
   ```
3. Reboot once after the first install so the DS3231 overlay is active:
   ```bash
   sudo reboot
   ```
4. Run the preflight report:
   ```bash
   sudo /home/juara2026pi1/juara-wildlife-station-src/scripts/pi_preflight.sh
   ```
5. Confirm I2C:
   ```bash
   /usr/sbin/i2cdetect -y 1
   ```
6. Confirm GPS:
   ```bash
   gpspipe -w -n 10
   ```
7. Confirm RTC:
   ```bash
   sudo hwclock --show --utc
   ```
8. Confirm audio device:
   ```bash
   arecord -l
   arecord -D default -f S16_LE -r 24000 -c 1 -d 5 /tmp/test.wav
   ```
9. Confirm camera:
   ```bash
   rpicam-still -n --immediate -o /tmp/test.jpg
   ```
10. Run a station smoke test:
   ```bash
   juara-station --config /etc/juara-station.toml doctor
   sudo systemctl start juara-station
   sudo journalctl -u juara-station -f
   ```

## Time Rules Implemented

- GPS + RTC drift under 1 minute: use GPS.
- GPS + RTC drift from 1 to under 5 minutes: use GPS and write RTC to GPS.
- GPS + RTC drift of 5+ minutes: use RTC, count the event.
- Three consecutive 5+ minute GPS/RTC drifts: write RTC to GPS and use GPS.
- GPS unavailable: use RTC.
- GPS and RTC unavailable: use the previous timestamp plus one interval.

## Power Behavior

- Night camera settings apply from 6 PM to 6 AM.
- PIR photos remain enabled in the current config; at night the flash turns on immediately, the capture target is 0.5 seconds after motion, and flash stays on 0.5 seconds after capture.
- Bird audio pauses from midnight to 4 AM; remaining backlog is purged around 3:45 AM instead of clogging the system.
- Night audio uses a lighter recording profile by default.
- The service runs with systemd restart behavior and planned reboot checkpoints at midnight, 4 AM, noon, and 8 PM.

## Station Variants

The current June 2026 camera trap uses `configs/station.june2026trap.example.toml`: PIR motion capture on GPIO 26, flash on GPIO 6, no image AI, 90 bird-call CSV columns, root-level USB CSV, `/mnt/juara_usb/Photos`, and BirdNET audio.

`juara2026pi1` legacy configs remain in the repo for reference but should not be the default for this camera-trap deployment.

`juara2026pi4` uses `configs/station.pi4.example.toml`: no PIR, no image AI, BirdNET audio enabled, and fixed camera captures at 08:00 and 16:00 local time.

The installer looks for a USB drive labeled `JUARA-CAM-1` by default, falls back to the first normal `/dev/sd*` USB partition if that label is missing, mounts it at `/mnt/juara_usb` with `x-systemd.automount`, writes the current camera-trap CSV at the USB root, and stores photos in `/mnt/juara_usb/Photos`. Override `USB_LABEL` during install/deploy if a station’s thumb drive has a different label.
