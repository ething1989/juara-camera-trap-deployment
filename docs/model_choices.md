# Model Choices

## Camera-Trap Photos

The field build keeps every captured photo and does not run image AI. SpeciesNet experiments are still present in the codebase for later work, but deployment configs leave `[speciesnet].enabled = false` because the Pi Zero 2 W was too constrained for reliable simultaneous BirdNET and photo inference.

Operational notes:

- The PIR camera trap keeps the Picamera2 stream warm during the daytime capture window.
- PIR photos remain enabled in the current deployment. Night captures use the flash timing and night camera settings from `configs/station.june2026trap.example.toml`.
- The CSV logs `photos_taken` for each five-minute interval; photos are reviewed later from the USB media folder.

## Bird Audio

Primary runner: BirdNET Analyzer (`birdnet-analyzer==2.4.0`).

Why:

- It supports `--lat`, `--lon`, and `--week`, so detections are location/time filtered.
- It outputs CSV and exposes sensitivity, overlap, confidence threshold, worker count, and species-frequency threshold.
- It is maintained for large scientific audio workflows.

Source:

- BirdNET Analyzer CLI docs: https://birdnet-team.github.io/BirdNET-Analyzer/usage/cli.html
- BirdNET Analyzer PyPI: https://pypi.org/project/birdnet-analyzer/

Operational notes:

- Day audio defaults to 48 kHz, 32-bit WAV capture before analysis.
- Night audio defaults to 24 kHz, 16-bit WAV and lower-overlap analysis to reduce power/CPU load.
- The station treats each BirdNET detection row as one call event. The deployment CSV keeps interval summaries plus up to 90 `Call #` cells per interval, with ranked candidate options above the configured threshold stacked inside each cell.
- The model should be calibrated against field recordings once the microphone gain and habitat noise are known.

## Camera Timing

The service uses Picamera2 rather than launching `rpicam-still` for every PIR event. Launching a camera command on demand causes the preview/warmup delay you noticed. Picamera2 can keep the camera started and capture a request with an explicit timestamp/flush behavior.

Sources:

- Raspberry Pi camera software docs: https://www.raspberrypi.com/documentation/computers/camera_software.html
- Picamera2 manual: https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf
