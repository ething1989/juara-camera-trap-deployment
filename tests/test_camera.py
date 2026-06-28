from datetime import timezone
from pathlib import Path
import time

from juara_station.camera import CaptureResult, PiCamera2Camera
from juara_station.config import CameraConfig, CameraModeConfig
from juara_station.storage import utc_now


def test_picamera_capture_waits_until_target_and_restarts_after_failure(tmp_path: Path, monkeypatch):
    camera = PiCamera2Camera(CameraConfig())
    calls = []
    attempts = []

    def fake_start():
        calls.append("start")
        camera._picam2 = object()

    def fake_close():
        calls.append("close")
        camera._picam2 = None

    def fake_apply_mode(mode: CameraModeConfig):
        calls.append(("mode", mode.exposure_us))

    def fake_capture_once(path: Path):
        attempts.append(time.monotonic())
        raise RuntimeError("frontend timeout")

    monkeypatch.setattr(camera, "start", fake_start)
    monkeypatch.setattr(camera, "close", fake_close)
    monkeypatch.setattr(camera, "apply_mode", fake_apply_mode)
    monkeypatch.setattr(camera, "_capture_once", fake_capture_once)

    target = time.monotonic_ns() + 120_000_000
    result = camera.capture_at(tmp_path / "photo.jpg", target, CameraModeConfig(exposure_us=2000))

    assert result.status == "error"
    assert "camera restarted" in (result.error or "")
    assert len(attempts) == 1
    assert attempts[0] >= target / 1_000_000_000
    assert calls == ["start", ("mode", 2000), "close", "start"]


def test_picamera_clamps_exposure_to_configured_maximum():
    controls = []
    camera = PiCamera2Camera(CameraConfig(max_exposure_us=250000))
    camera._picam2 = type("FakePicam", (), {"set_controls": controls.append})()

    camera.apply_mode(CameraModeConfig(exposure_us=500000, analogue_gain=3.0))

    assert controls == [{"AeEnable": False, "ExposureTime": 250000, "AnalogueGain": 3.0}]


def test_picamera_auto_exposure_uses_configured_shutter_ceiling():
    controls = []
    camera = PiCamera2Camera(CameraConfig(max_exposure_us=250000))
    camera._picam2 = type("FakePicam", (), {"set_controls": controls.append})()

    camera.apply_mode(CameraModeConfig(auto_exposure=True, exposure_value=4.0))

    assert controls == [{"AeEnable": True, "FrameDurationLimits": (100, 250000), "ExposureValue": 4.0}]


def test_picamera_auto_exposure_clamps_to_camera_control_range():
    controls = []
    fake_picam = type(
        "FakePicam",
        (),
        {
            "camera_controls": {
                "ExposureTime": (1, 66666, 20000),
                "FrameDurationLimits": (33333, 120000, 33333),
            },
            "set_controls": controls.append,
        },
    )()
    camera = PiCamera2Camera(CameraConfig(max_exposure_us=250000))
    camera._picam2 = fake_picam

    camera.apply_mode(CameraModeConfig(auto_exposure=True, exposure_value=4.0))

    assert controls == [{"AeEnable": True, "FrameDurationLimits": (33333, 66666), "ExposureValue": 4.0}]


def test_picamera_auto_exposure_ignores_manual_defaults():
    controls = []
    camera = PiCamera2Camera(CameraConfig(max_exposure_us=250000))
    camera._picam2 = type("FakePicam", (), {"set_controls": controls.append})()

    camera.apply_mode(CameraModeConfig(exposure_us=2000, analogue_gain=1.0, auto_exposure=True, exposure_value=4.0))

    assert controls == [{"AeEnable": True, "FrameDurationLimits": (100, 250000), "ExposureValue": 4.0}]
