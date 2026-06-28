from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import logging
import shutil
import subprocess
import time

from .config import CameraConfig, CameraModeConfig
from .storage import utc_now


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureResult:
    path: Path | None
    captured_at: datetime
    status: str
    error: str | None = None


class Flash:
    def on(self) -> None:
        pass

    def off(self) -> None:
        pass

    def close(self) -> None:
        self.off()


class GpioFlash(Flash):
    def __init__(self, pin: int):
        from gpiozero import LED

        self._led = LED(pin)

    def on(self) -> None:
        self._led.on()

    def off(self) -> None:
        self._led.off()

    def close(self) -> None:
        self._led.close()


class Camera:
    def start(self) -> None:
        pass

    def apply_mode(self, mode: CameraModeConfig) -> None:
        pass

    def capture_at(self, path: Path, target_monotonic_ns: int, mode: CameraModeConfig) -> CaptureResult:
        raise NotImplementedError

    def restart(self) -> None:
        self.close()
        self.start()

    def close(self) -> None:
        pass


class PiCamera2Camera(Camera):
    def __init__(self, config: CameraConfig):
        self.config = config
        self._picam2 = None

    def start(self) -> None:
        if self._picam2 is not None:
            return
        from picamera2 import Picamera2

        picam2 = Picamera2()
        warm_config = picam2.create_preview_configuration(
            main={"size": (self.config.width, self.config.height), "format": "RGB888"},
            raw=None,
            buffer_count=4,
            queue=False,
        )
        picam2.configure(warm_config)
        picam2.start()
        time.sleep(2.0)
        self._picam2 = picam2

    def capture_at(self, path: Path, target_monotonic_ns: int, mode: CameraModeConfig) -> CaptureResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            started_before_trigger = self._picam2 is not None
            if not started_before_trigger:
                LOGGER.warning("Camera was cold at capture request; starting camera before photo")
            self.start()
            assert self._picam2 is not None
            self.apply_mode(mode)
            delay = max(0, (target_monotonic_ns - time.monotonic_ns()) / 1_000_000_000)
            if delay:
                time.sleep(delay)
            capture_start = time.monotonic()
            result = self._capture_once_with_timeout(path)
            LOGGER.info(
                "Camera capture completed warm=%s capture_call_seconds=%.3f path=%s",
                started_before_trigger,
                time.monotonic() - capture_start,
                path,
            )
            return result
        except Exception as exc:
            first_error = str(exc)
            LOGGER.warning("Camera capture failed; restarting camera for future triggers", exc_info=True)
            try:
                self.close()
                self.start()
            except Exception as restart_exc:
                self.close()
                return CaptureResult(
                    path,
                    utc_now(),
                    "error",
                    f"capture failed: {first_error}; restart after failure also failed: {restart_exc}",
                )
            return CaptureResult(path, utc_now(), "error", f"capture failed: {first_error}; camera restarted")

    def _capture_once_with_timeout(self, path: Path) -> CaptureResult:
        timeout = max(1.0, float(self.config.capture_timeout_seconds))
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="juara-camera-capture")
        future = executor.submit(self._capture_once, path)
        try:
            result = future.result(timeout=timeout)
            executor.shutdown(wait=False, cancel_futures=True)
            return result
        except FutureTimeoutError as exc:
            LOGGER.warning("Camera capture timed out after %.1fs; closing camera to recover", timeout)
            try:
                self.close()
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(f"camera capture timed out after {timeout:.1f}s") from exc
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            raise

    def _capture_once(self, path: Path) -> CaptureResult:
        assert self._picam2 is not None
        request = self._picam2.capture_request(flush=False)
        try:
            captured_at = utc_now()
            request.save("main", str(path))
            return CaptureResult(path, captured_at, "captured")
        finally:
            request.release()

    def apply_mode(self, mode: CameraModeConfig) -> None:
        assert self._picam2 is not None
        controls: dict[str, object] = {}
        max_exposure_us = self._effective_max_exposure_us()
        if mode.auto_exposure:
            controls["AeEnable"] = True
            controls["FrameDurationLimits"] = self._frame_duration_limits(max_exposure_us)
            if mode.exposure_value is not None:
                controls["ExposureValue"] = float(mode.exposure_value)
        else:
            if mode.exposure_us is not None or mode.analogue_gain is not None:
                controls["AeEnable"] = False
            if mode.exposure_us is not None:
                exposure_us = int(mode.exposure_us)
                if exposure_us > max_exposure_us:
                    LOGGER.warning(
                        "Clamping camera exposure from %sus to configured maximum %sus",
                        exposure_us,
                        max_exposure_us,
                    )
                    exposure_us = max_exposure_us
                controls["ExposureTime"] = exposure_us
            if mode.analogue_gain is not None:
                controls["AnalogueGain"] = float(mode.analogue_gain)
        if mode.denoise:
            noise_reduction = _noise_reduction_control(mode.denoise)
            if noise_reduction is not None:
                controls["NoiseReductionMode"] = noise_reduction
        if controls:
            self._picam2.set_controls(controls)

    def _effective_max_exposure_us(self) -> int:
        max_exposure_us = max(1, int(self.config.max_exposure_us))
        exposure_range = self._camera_control_range("ExposureTime")
        if exposure_range is not None:
            max_exposure_us = min(max_exposure_us, max(1, int(exposure_range[1])))
        return max_exposure_us

    def _frame_duration_limits(self, max_exposure_us: int) -> tuple[int, int]:
        frame_range = self._camera_control_range("FrameDurationLimits")
        if frame_range is None:
            return (100, max_exposure_us)
        minimum = max(1, int(frame_range[0]))
        maximum = min(max_exposure_us, int(frame_range[1]))
        if maximum < minimum:
            minimum = maximum
        return (minimum, maximum)

    def _camera_control_range(self, name: str):
        try:
            return self._picam2.camera_controls.get(name)
        except Exception:
            return None

    def close(self) -> None:
        if self._picam2 is not None:
            self._picam2.close()
            self._picam2 = None


class RpicamStillCamera(Camera):
    def __init__(self, config: CameraConfig):
        self.config = config
        self._command = shutil.which("rpicam-still") or shutil.which("libcamera-still")
        if self._command is None:
            raise RuntimeError("rpicam-still/libcamera-still is not installed")

    def start(self) -> None:
        return

    def capture_at(self, path: Path, target_monotonic_ns: int, mode: CameraModeConfig) -> CaptureResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        delay = max(0, (target_monotonic_ns - time.monotonic_ns()) / 1_000_000_000)
        if delay:
            time.sleep(delay)
        command = [
            self._command,
            "-n",
            "--immediate",
            "--width",
            str(self.config.width),
            "--height",
            str(self.config.height),
            "-o",
            str(path),
        ]
        if mode.exposure_us is not None and not mode.auto_exposure:
            command.extend(["--shutter", str(min(int(mode.exposure_us), self.config.max_exposure_us))])
        if mode.analogue_gain is not None and not mode.auto_exposure:
            command.extend(["--gain", str(float(mode.analogue_gain))])
        if mode.denoise:
            command.extend(["--denoise", mode.denoise])
        captured_at = utc_now()
        try:
            subprocess.run(command, check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            return CaptureResult(path, captured_at, "captured")
        except subprocess.CalledProcessError as exc:
            return CaptureResult(path, captured_at, "error", (exc.stderr or str(exc)).strip())
        except Exception as exc:
            return CaptureResult(path, captured_at, "error", str(exc))


class MockCamera(Camera):
    def start(self) -> None:
        return

    def capture_at(self, path: Path, target_monotonic_ns: int, mode: CameraModeConfig) -> CaptureResult:
        delay = max(0, (target_monotonic_ns - time.monotonic_ns()) / 1_000_000_000)
        if delay:
            time.sleep(delay)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_tiny_jpeg())
        return CaptureResult(path, utc_now(), "captured")


class MotionWatcher:
    def __init__(self, pin: int, callback):
        self.pin = pin
        self.callback = callback
        self._sensor = None

    def start(self) -> None:
        from gpiozero import MotionSensor

        self._sensor = MotionSensor(self.pin)
        self._sensor.when_motion = self.callback

    def close(self) -> None:
        if self._sensor is not None:
            self._sensor.close()


def create_camera(config: CameraConfig, mock: bool = False) -> Camera:
    if mock or not config.enabled:
        return MockCamera()
    if config.backend == "rpicam":
        return RpicamStillCamera(config)
    if config.backend == "picamera2":
        return PiCamera2Camera(config)
    raise ValueError(f"Unsupported camera backend: {config.backend}")


def create_flash(config: CameraConfig, mock: bool = False) -> Flash:
    if mock or not config.enabled:
        return Flash()
    try:
        return GpioFlash(config.flash_gpio)
    except Exception:
        LOGGER.exception("Flash GPIO unavailable; night captures will continue without flash")
        return Flash()


def _noise_reduction_control(name: str):
    try:
        from libcamera import controls

        enum = controls.draft.NoiseReductionModeEnum
        return {
            "off": enum.Off,
            "minimal": enum.Minimal,
            "fast": enum.Fast,
            "cdn_fast": enum.Fast,
            "high_quality": enum.HighQuality,
            "cdn_hq": enum.HighQuality,
        }.get(name)
    except Exception:
        return None


def _tiny_jpeg() -> bytes:
    return bytes.fromhex(
        "ffd8ffe000104a46494600010101006000600000ffdb004300"
        "0302020302020303030304030304050805050404050a07070608"
        "0c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b10161011131415"
        "15150c0f171816141812141514ffdb0043010304040504050905"
        "0509140d0b0d1414141414141414141414141414141414141414"
        "1414141414141414141414141414141414141414141414141414"
        "141414141414141414ffc0001108000100010301220002110103"
        "1101ffc400140001000000000000000000000000000000000000"
        "0008ffc400141001000000000000000000000000000000000000"
        "0000ffda000c03010002110311003f00b2c001ffd9"
    )
