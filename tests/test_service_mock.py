from pathlib import Path
from datetime import datetime, timedelta, timezone
import csv
import os
import pytest
import wave
from zoneinfo import ZoneInfo

from juara_station.config import (
    BirdNetConfig,
    CameraConfig,
    SpeciesNetConfig,
    StationConfig,
    StorageConfig,
    ScheduleConfig,
    TimeConfig,
    load_config,
)
from juara_station.ai import BirdNetRunner, MockBirdNetRunner, MockSpeciesNetRunner, SpeciesNetRunner
from juara_station.audio import MockAudioRecorder, RecordingResult
from juara_station.camera import CaptureResult, Flash, MockCamera
from juara_station.paths import resolve_paths
from juara_station.sensors import MockSensorSuite
from juara_station.service import (
    StationService,
    _cap_camera_scan_candidates,
    _day_camera_scan_score,
    _hour_in_window,
    _parse_camera_scan_candidates,
    next_scheduled_capture,
)
from juara_station.storage import BirdCall, BirdCandidate
from juara_station.timekeeper import CoordinateFix, TimeReading
import juara_station.service as service_module


def test_mock_interval_creates_csv_audio_and_photo(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(
            interval_seconds=5,
            sensor_sample_seconds=1,
            photo_capture_disabled_start_hour=24,
            photo_capture_disabled_end_hour=25,
        ),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)

    csv_path = service.run_interval(duration_seconds=1, simulate_motion=True)

    assert csv_path.exists()
    assert not (tmp_path / "usb" / "media" / "audio").exists()
    assert not list((tmp_path / "usb" / "media" / "audio").glob("**/*.wav"))
    assert not list(service.paths.recordings_dir.glob("**/*.wav"))
    assert list((tmp_path / "usb" / "media" / "photos").glob("**/*.jpg"))
    assert "Hyacinth macaw" in csv_path.read_text() or "Pauraque" in csv_path.read_text()
    env_path = tmp_path / "usb" / "logs" / "juara_environment_samples.csv"
    env_rows = list(csv.DictReader(env_path.open()))
    assert env_rows
    assert env_rows[0]["timestamp_utc"]
    assert env_rows[0]["timestamp_source"]
    assert env_rows[0]["system_timestamp_utc"]
    assert env_rows[0]["temperature_c"]
    assert env_rows[0]["humidity_pct"]
    assert env_rows[0]["lux"]


def test_pi1_config_uses_day_auto_exposure_without_scan():
    config = load_config(Path("configs/station.pi1.example.toml"))

    assert config.camera.day_scan_enabled is False
    assert config.camera.day_scan_interval_seconds == 1800
    assert config.camera.day_scan_start_hour == 6
    assert config.camera.day_scan_end_hour == 19
    assert config.camera.max_exposure_us == 250000
    assert config.camera.day.auto_exposure is True
    assert config.camera.day.exposure_value == 4.0


def test_day_camera_scan_helpers_pick_useful_indoor_candidate():
    assert _hour_in_window(6, 6, 19)
    assert _hour_in_window(18, 6, 19)
    assert not _hour_in_window(19, 6, 19)
    candidates = _parse_camera_scan_candidates(["5000:2.0", "10000:4.0", "bad", "0:1"])
    assert [(mode.exposure_us, mode.analogue_gain) for mode in candidates] == [(5000, 2.0), (10000, 4.0)]
    capped = _cap_camera_scan_candidates(_parse_camera_scan_candidates(["500000:4.0", "250000:4.0"]), 250000)
    assert [(mode.exposure_us, mode.analogue_gain) for mode in capped] == [(250000, 4.0)]

    dim_score = _day_camera_scan_score(candidates[0], 24.5, 10.0, 0.0, 0.0, 85.0)
    usable_score = _day_camera_scan_score(candidates[1], 73.6, 20.0, 0.0, 0.0, 85.0)

    assert usable_score < dim_score


def test_scheduled_capture_keeps_photo_without_speciesnet(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(photo_capture_disabled_start_hour=24, photo_capture_disabled_end_hour=25),
        camera=CameraConfig(motion_enabled=False, scheduled_capture_times=["08:00", "16:00"]),
        speciesnet=SpeciesNetConfig(enabled=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)

    service._capture_scheduled_photo()

    assert list((tmp_path / "usb" / "media" / "survey_photos").glob("**/*.jpg"))
    rows = service.store.pending_photo_events()
    assert rows == []


class TrackingCamera(MockCamera):
    def __init__(self):
        self.close_count = 0
        self.start_count = 0

    def start(self) -> None:
        self.start_count += 1

    def close(self) -> None:
        self.close_count += 1


def test_scheduled_only_camera_closes_after_capture(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(
            motion_capture_delay_seconds=0,
            photo_capture_disabled_start_hour=24,
            photo_capture_disabled_end_hour=25,
        ),
        camera=CameraConfig(motion_enabled=False, scheduled_capture_times=["08:00", "16:00"]),
        speciesnet=SpeciesNetConfig(enabled=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    camera = TrackingCamera()
    service.camera = camera

    service._capture_scheduled_photo()

    assert camera.close_count == 1


def test_motion_camera_stays_warm_after_capture(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(
            motion_capture_delay_seconds=0,
            photo_capture_disabled_start_hour=24,
            photo_capture_disabled_end_hour=25,
        ),
        camera=CameraConfig(motion_enabled=True),
        speciesnet=SpeciesNetConfig(enabled=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    camera = TrackingCamera()
    service.camera = camera

    service._capture_motion_photo()

    assert camera.close_count == 0


def test_warm_motion_camera_is_periodically_refreshed(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        camera=CameraConfig(motion_enabled=True, warm_restart_interval_seconds=300),
        speciesnet=SpeciesNetConfig(enabled=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    camera = TrackingCamera()
    service.camera = camera
    first = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    due = first + timedelta(minutes=5)

    service._sync_camera_window(first)
    service._sync_camera_window(first + timedelta(minutes=1))
    service._sync_camera_window(due)

    assert camera.start_count == 2
    assert camera.close_count == 2
    assert service._last_camera_warm_restart_at == due


def test_day_camera_scan_keeps_existing_warm_stream(tmp_path: Path, monkeypatch):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        camera=CameraConfig(motion_enabled=True, day_scan_enabled=True),
        speciesnet=SpeciesNetConfig(enabled=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    camera = TrackingCamera()
    service.camera = camera
    monkeypatch.setattr(service, "_scan_day_camera_candidates", lambda candidates: candidates[0])

    service._run_day_camera_scan(datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc))

    assert camera.start_count == 0
    assert camera.close_count == 0


class FailingCamera(MockCamera):
    def capture_at(self, path: Path, target_monotonic_ns: int, mode):  # noqa: ARG002
        return CaptureResult(path, datetime.now(timezone.utc), "error", "camera frontend timeout")


def test_camera_capture_error_is_logged_without_crashing(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(
            motion_capture_delay_seconds=0,
            photo_capture_disabled_start_hour=24,
            photo_capture_disabled_end_hour=25,
        ),
        camera=CameraConfig(motion_enabled=True),
        speciesnet=SpeciesNetConfig(enabled=True),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.camera = FailingCamera()

    service._capture_motion_photo()

    with service.store.connect() as conn:
        row = conn.execute("SELECT status, ai_status, error FROM photo_events").fetchone()
    assert row["status"] == "error"
    assert row["ai_status"] == "error"
    assert row["error"] == "camera frontend timeout"


class FailingSpeciesNet:
    def analyze_photo(self, photo_path: Path, work_dir: Path):  # noqa: ARG002
        raise RuntimeError("speciesnet crashed")


def test_image_ai_failure_marks_photo_for_retry(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(
            motion_capture_delay_seconds=0,
            photo_capture_disabled_start_hour=24,
            photo_capture_disabled_end_hour=25,
        ),
        camera=CameraConfig(motion_enabled=True),
        speciesnet=SpeciesNetConfig(enabled=True),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service._capture_motion_photo()
    service.speciesnet = FailingSpeciesNet()

    changed_days = service.process_image_backlog(now=datetime(2026, 5, 14, 12, 0, tzinfo=config.zoneinfo))

    with service.store.connect() as conn:
        row = conn.execute("SELECT status, ai_status, error FROM photo_events").fetchone()
    assert changed_days == set()
    assert row["status"] == "captured"
    assert row["ai_status"] == "retry"
    assert row["error"] == "speciesnet crashed"


class MemoryFailingSpeciesNet:
    def analyze_photo(self, photo_path: Path, work_dir: Path):  # noqa: ARG002
        raise RuntimeError("std::bad_alloc while loading SpeciesNet")


def test_image_ai_memory_failure_keeps_photo_without_retry(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(
            motion_capture_delay_seconds=0,
            photo_capture_disabled_start_hour=24,
            photo_capture_disabled_end_hour=25,
        ),
        camera=CameraConfig(motion_enabled=True),
        speciesnet=SpeciesNetConfig(enabled=True),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service._capture_photo(0.0, "motion", triggered_at=datetime(2026, 5, 14, 12, 0, tzinfo=config.zoneinfo))
    service.speciesnet = MemoryFailingSpeciesNet()

    changed_days = service.process_image_backlog(now=datetime(2026, 5, 14, 12, 0, tzinfo=config.zoneinfo))

    with service.store.connect() as conn:
        row = conn.execute("SELECT status, ai_status, error FROM photo_events").fetchone()
    assert changed_days == {datetime(2026, 5, 14, tzinfo=config.zoneinfo).date()}
    assert row["status"] == "kept"
    assert row["ai_status"] == "error"
    assert "std::bad_alloc" in row["error"]


def test_image_ai_closes_warm_camera_while_processing(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(
            motion_capture_delay_seconds=0,
            photo_capture_disabled_start_hour=24,
            photo_capture_disabled_end_hour=25,
        ),
        camera=CameraConfig(motion_enabled=True),
        speciesnet=SpeciesNetConfig(enabled=True),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    camera = TrackingCamera()
    service.camera = camera
    service._capture_motion_photo()

    service.process_image_backlog(now=datetime(2026, 5, 14, 12, 0, tzinfo=config.zoneinfo))

    assert camera.close_count == 1
    assert camera.start_count == 1


def test_photo_capture_is_blocked_during_disabled_window(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(motion_capture_delay_seconds=0),
        camera=CameraConfig(motion_enabled=True),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    blocked_at = datetime(2026, 5, 14, 20, 0, tzinfo=config.zoneinfo)

    service._capture_photo(0.0, "motion", triggered_at=blocked_at)

    assert not list((tmp_path / "usb" / "media" / "photos").glob("**/*.jpg"))
    with service.store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM photo_events").fetchone()["count"]
    assert count == 0


def test_unprocessed_photos_are_skipped_after_deadline(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(motion_capture_delay_seconds=0),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    before_deadline = datetime(2026, 5, 14, 18, 30, tzinfo=config.zoneinfo)
    service._capture_photo(0.0, "motion", triggered_at=before_deadline)

    service._skip_expired_photo_backlog(datetime(2026, 5, 15, 6, 0, tzinfo=config.zoneinfo))

    with service.store.connect() as conn:
        row = conn.execute("SELECT status, ai_status, error FROM photo_events").fetchone()
    assert row["status"] == "skipped_unprocessed"
    assert row["ai_status"] == "skipped"
    assert "6 AM" in row["error"]


def test_image_ai_processes_one_photo_per_run(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(motion_capture_delay_seconds=0),
        camera=CameraConfig(motion_enabled=True),
        speciesnet=SpeciesNetConfig(enabled=True, max_photos_per_run=1),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service._capture_photo(0.0, "motion", triggered_at=datetime(2026, 5, 14, 10, 0, tzinfo=config.zoneinfo))
    service._capture_photo(0.0, "motion", triggered_at=datetime(2026, 5, 14, 10, 1, tzinfo=config.zoneinfo))

    service.process_image_backlog(now=datetime(2026, 5, 14, 12, 0, tzinfo=config.zoneinfo))

    with service.store.connect() as conn:
        rows = conn.execute("SELECT ai_status FROM photo_events ORDER BY id").fetchall()
    assert [row["ai_status"] for row in rows] == ["done", "pending"]


def test_stale_pending_photo_is_marked_error(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(interval_seconds=300),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    stale_trigger = now - timedelta(minutes=20)
    period_start = stale_trigger.replace(minute=0, second=0, microsecond=0)
    service.store.create_photo_event(period_start, stale_trigger, stale_trigger + timedelta(seconds=0.5))

    service._mark_stale_pending_photos(now)

    with service.store.connect() as conn:
        row = conn.execute("SELECT status, ai_status, error FROM photo_events").fetchone()
    assert row["status"] == "error"
    assert row["ai_status"] == "error"
    assert "Capture did not finish" in row["error"]


def test_motion_watcher_retries_after_busy_start(tmp_path: Path, monkeypatch):
    starts = []

    class FlakyMotionWatcher:
        def __init__(self, pin, callback):
            self.pin = pin
            self.callback = callback

        def start(self):
            starts.append(self.pin)
            if len(starts) == 1:
                raise RuntimeError("GPIO busy")

        def close(self):
            return

    monkeypatch.setattr(service_module, "MotionWatcher", FlakyMotionWatcher)
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        camera=CameraConfig(motion_enabled=True),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.mock = False

    service._ensure_motion_watcher()
    assert service._motion_watcher is None

    service._ensure_motion_watcher()
    assert service._motion_watcher is not None
    assert starts == [4, 4]


class RecordingBirdNet:
    def __init__(self):
        self.calls = []

    def analyze_audio_batch(self, jobs, output_dir, week, night):
        self.calls.append((list(jobs), output_dir, week, night))
        return {job.period_start: [BirdCall(0.0, 3.0, (BirdCandidate(job.audio_path.stem, 0.5),))] for job in jobs}


def write_wav(path: Path, duration_seconds: float, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = b"\x00\x00" * int(duration_seconds * sample_rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)


def test_audio_backlog_batches_pending_recordings(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        birdnet=BirdNetConfig(batch_min_files=1, batch_max_files=2),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.birdnet = RecordingBirdNet()
    start = datetime(2026, 5, 11, 21, 0, tzinfo=timezone.utc)
    for index in range(3):
        audio_path = tmp_path / f"audio_{index}.wav"
        audio_path.write_bytes(b"wav")
        period_start = start + timedelta(minutes=5 * index)
        service.store.upsert_audio_event(
            period_start,
            "recorded",
            str(audio_path),
            period_start,
            period_start,
            error="old retry error",
        )

    changed_days = service.process_audio_backlog_rows(service.store.pending_audio_events())

    assert changed_days == {start.astimezone(config.zoneinfo).date()}
    assert [len(call[0]) for call in service.birdnet.calls] == [2, 1]
    assert service.store.pending_audio_events() == []
    assert not list(tmp_path.glob("audio_*.wav"))
    with service.store.connect() as conn:
        rows = conn.execute("SELECT error FROM audio_events").fetchall()
    assert [row["error"] for row in rows] == [None, None, None]


def test_station_service_leaves_ai_backlog_to_external_worker(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(interval_seconds=5, sensor_sample_seconds=1, sd_low_free_percent=0),
        birdnet=BirdNetConfig(run_in_station_service=False, process_inline=False),
        speciesnet=SpeciesNetConfig(run_in_station_service=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.mock = False

    service.run_interval(duration_seconds=1)

    assert service._audio_worker_thread is None
    assert service._image_worker_thread is None
    assert len(service.store.pending_audio_events()) == 1


def test_recording_error_is_not_left_pending_for_ai(tmp_path: Path):
    class ErrorRecorder:
        def record(self, output_path: Path, duration_seconds: int, night: bool):  # noqa: ARG002
            started = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
            return RecordingResult(output_path, started, started, "error", "arecord failed")

    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(interval_seconds=5, sensor_sample_seconds=1),
        speciesnet=SpeciesNetConfig(enabled=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.audio = ErrorRecorder()

    service.run_interval(duration_seconds=1)

    with service.store.connect() as conn:
        row = conn.execute("SELECT status, ai_status, error FROM audio_events").fetchone()
    assert row["status"] == "error"
    assert row["ai_status"] == "done"
    assert row["error"] == "arecord failed"
    assert service.store.pending_audio_events() == []


def test_ai_worker_once_processes_audio_then_image_backlogs(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(motion_capture_delay_seconds=0),
        birdnet=BirdNetConfig(batch_min_files=1, batch_max_files=2, run_in_station_service=False),
        speciesnet=SpeciesNetConfig(enabled=True, run_in_station_service=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.birdnet = RecordingBirdNet()
    zone = config.zoneinfo
    period_start = datetime(2026, 5, 14, 10, 0, tzinfo=zone)
    audio_path = tmp_path / "worker_audio.wav"
    audio_path.write_bytes(b"wav")
    service.store.upsert_audio_event(period_start, "recorded", str(audio_path), period_start, period_start)
    service._capture_photo(0.0, "motion", triggered_at=period_start)

    changed_days = service.run_ai_worker_once(now=period_start + timedelta(minutes=5), manage_camera=False)

    assert changed_days == {period_start.date()}
    assert service.store.pending_audio_events() == []
    assert service.store.pending_photo_events() == []
    assert not audio_path.exists()
    csv_path = tmp_path / "usb" / "logs" / "juara_station.csv"
    rows = list(csv.DictReader(csv_path.open()))
    assert "animal_detections" not in rows[0]
    assert rows[0]["photos_taken"] == "1"


def test_ai_only_service_uses_mock_hardware_but_real_ai_runners(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=False, ai_only=True)

    assert isinstance(service.sensors, MockSensorSuite)
    assert isinstance(service.audio, MockAudioRecorder)
    assert isinstance(service.camera, MockCamera)
    assert isinstance(service.flash, Flash)
    assert isinstance(service.birdnet, BirdNetRunner)
    assert isinstance(service.speciesnet, SpeciesNetRunner)
    assert not isinstance(service.birdnet, MockBirdNetRunner)
    assert not isinstance(service.speciesnet, MockSpeciesNetRunner)


def test_audio_backlog_skips_missing_temp_recording(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        birdnet=BirdNetConfig(batch_min_files=1, batch_max_files=2),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.birdnet = RecordingBirdNet()
    start = datetime(2026, 5, 11, 21, 0, tzinfo=timezone.utc)
    missing_path = tmp_path / "missing.wav"
    service.store.upsert_audio_event(start, "recorded", str(missing_path), start, start)

    changed_days = service.process_audio_backlog_rows(service.store.pending_audio_events())

    assert changed_days == {start.astimezone(config.zoneinfo).date()}
    assert service.birdnet.calls == []
    assert service.store.pending_audio_events() == []
    with service.store.connect() as conn:
        row = conn.execute("SELECT status, ai_status, error FROM audio_events").fetchone()
    assert row["status"] == "missing_audio"
    assert row["ai_status"] == "done"
    assert "Missing audio recording" in row["error"]


def test_ai_worker_skips_pending_audio_during_cooldown(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        birdnet=BirdNetConfig(batch_min_files=1),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.birdnet = RecordingBirdNet()
    service._cooldown_active = True
    start = datetime(2026, 5, 11, 21, 0, tzinfo=timezone.utc)
    audio_path = tmp_path / "cooldown.wav"
    audio_path.write_bytes(b"wav")
    service.store.upsert_audio_event(start, "recorded", str(audio_path), start, start)

    changed_days = service.run_ai_worker_once(now=start + timedelta(minutes=5))

    assert changed_days == set()
    assert service.birdnet.calls == []
    assert service.store.pending_audio_events()


def test_stale_internal_audio_cleanup_preserves_pending_and_current_recordings(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(interval_seconds=300),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    recordings_dir = service.paths.recordings_dir / "2026-05-14"
    recordings_dir.mkdir(parents=True)

    old_orphan = recordings_dir / "old_orphan.wav"
    recent_orphan = recordings_dir / "recent_orphan.wav"
    done_file = recordings_dir / "done.wav"
    pending_file = recordings_dir / "pending.wav"
    for path in (old_orphan, recent_orphan, done_file, pending_file):
        path.write_bytes(b"wav")
    os.utime(old_orphan, (now.timestamp() - 3600, now.timestamp() - 3600))
    os.utime(recent_orphan, (now.timestamp() - 60, now.timestamp() - 60))

    done_start = now - timedelta(minutes=30)
    pending_start = now - timedelta(minutes=5)
    service.store.upsert_audio_event(done_start, "recorded", str(done_file), done_start, done_start, ai_status="done")
    service.store.upsert_audio_event(pending_start, "recorded", str(pending_file), pending_start, pending_start)

    removed = service._cleanup_stale_audio_files(now)

    assert removed == 2
    assert not old_orphan.exists()
    assert recent_orphan.exists()
    assert not done_file.exists()
    assert pending_file.exists()


def test_startup_logs_pi_restart_and_possible_power_loss(tmp_path: Path, monkeypatch):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    service.timekeeper.now = lambda fallback_step: TimeReading(now, "gps")  # noqa: ARG005
    monkeypatch.setattr(service_module, "_current_boot_id", lambda: "boot-2")
    service._startup_state_path().write_text('{"boot_id":"boot-1","started_at_utc":"2026-05-14T00:00:00+00:00"}')

    changed_days = service._record_startup_events()

    assert changed_days == {now.astimezone(config.zoneinfo).date()}
    with service.store.connect() as conn:
        rows = conn.execute("SELECT system_event, timestamp_source FROM intervals ORDER BY period_start_utc").fetchall()
    assert [(row["system_event"], row["timestamp_source"]) for row in rows] == [
        ("PI_RESTARTED;POSSIBLE_POWER_LOSS_RECOVERY", "gps"),
    ]


def test_dynamic_coordinates_accept_consistent_far_away_gps(tmp_path: Path):
    fallback_lat = -16.68260
    fallback_lon = -56.90453
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        time=TimeConfig(
            rtc_write_enabled=False,
            coordinate_enabled=True,
            coordinate_fix_count=10,
            fallback_latitude=fallback_lat,
            fallback_longitude=fallback_lon,
            coordinate_max_distance_from_fallback_km=1000.0,
        ),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    service.timekeeper.now = lambda fallback_step: TimeReading(now, "gps")  # noqa: ARG005
    service.timekeeper.read_gps_coordinates = lambda count, timeout: [  # noqa: ARG005
        CoordinateFix(42.3314, -83.0458, now) for _ in range(10)
    ]

    changed_days = service._prepare_dynamic_coordinates_and_species()

    assert changed_days == {now.astimezone(config.zoneinfo).date()}
    assert service._current_latitude == pytest.approx(42.3314)
    assert service._current_longitude == pytest.approx(-83.0458)
    with service.store.connect() as conn:
        row = conn.execute("SELECT system_event FROM intervals").fetchone()
    assert row["system_event"] == "GPS_COORDINATES"


def test_dynamic_coordinates_accept_field_gps_after_ten_good_fixes(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        time=TimeConfig(
            rtc_write_enabled=False,
            coordinate_enabled=True,
            coordinate_fix_count=10,
            fallback_latitude=-16.68260,
            fallback_longitude=-56.90453,
            coordinate_max_distance_from_fallback_km=1000.0,
        ),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    service.timekeeper.now = lambda fallback_step: TimeReading(now, "gps")  # noqa: ARG005
    service.timekeeper.read_gps_coordinates = lambda count, timeout: [  # noqa: ARG005
        CoordinateFix(-16.68250 + index * 0.000001, -56.90440, now) for index in range(10)
    ]

    service._prepare_dynamic_coordinates_and_species()

    assert service._coordinate_source == "gps"
    assert abs(service._current_latitude - -16.6824955) < 0.00001
    assert abs(service._current_longitude - -56.90440) < 0.00001
    assert service._read_coordinate_state() == pytest.approx((service._current_latitude, service._current_longitude))


def test_startup_audio_recovery_requeues_complete_orphans_and_drops_partial_files(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(interval_seconds=5),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    now = datetime(2026, 5, 14, 13, 0, tzinfo=zone)
    complete = service.paths.recordings_dir / "2026-05-14" / "20260514_120000.wav"
    partial = service.paths.recordings_dir / "2026-05-14" / "20260514_120500.wav"
    write_wav(complete, duration_seconds=5.0)
    write_wav(partial, duration_seconds=5.0)
    with partial.open("r+b") as handle:
        handle.truncate(44 + 8000 * 2)
    old_mtime = now.timestamp() - 600
    os.utime(complete, (old_mtime, old_mtime))
    os.utime(partial, (old_mtime, old_mtime))

    changed_days = service._recover_orphan_audio_recordings(now.astimezone(timezone.utc))

    assert changed_days == {now.date()}
    assert complete.exists()
    assert not partial.exists()
    with service.store.connect() as conn:
        rows = conn.execute("SELECT period_start_utc, status, ai_status, path FROM audio_events ORDER BY period_start_utc").fetchall()
    assert rows[0]["status"] == "recorded"
    assert rows[0]["ai_status"] == "pending"
    assert rows[0]["path"] == str(complete)
    assert rows[1]["status"] == "interrupted_power_loss"
    assert rows[1]["ai_status"] == "done"


def test_startup_audio_recovery_drops_partial_pending_rows(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(interval_seconds=5),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    now = datetime(2026, 5, 14, 13, 0, tzinfo=zone)
    partial_start = datetime(2026, 5, 14, 12, 10, tzinfo=zone).astimezone(timezone.utc)
    complete_start = datetime(2026, 5, 14, 12, 15, tzinfo=zone).astimezone(timezone.utc)
    partial = service.paths.recordings_dir / "2026-05-14" / "20260514_121000.wav"
    complete = service.paths.recordings_dir / "2026-05-14" / "20260514_121500.wav"
    write_wav(partial, duration_seconds=5.0)
    with partial.open("r+b") as handle:
        handle.truncate(44 + 8000 * 2)
    write_wav(complete, duration_seconds=5.0)
    old_mtime = now.timestamp() - 600
    os.utime(partial, (old_mtime, old_mtime))
    os.utime(complete, (old_mtime, old_mtime))
    service.store.upsert_audio_event(partial_start, "recorded", str(partial), partial_start, now)
    service.store.upsert_audio_event(complete_start, "recorded", str(complete), complete_start, now)

    changed_days = service._recover_interrupted_audio_events(now.astimezone(timezone.utc))

    assert changed_days == {now.date()}
    assert not partial.exists()
    assert complete.exists()
    with service.store.connect() as conn:
        rows = conn.execute(
            "SELECT period_start_utc, status, ai_status FROM audio_events ORDER BY period_start_utc"
        ).fetchall()
    assert rows[0]["status"] == "interrupted_power_loss"
    assert rows[0]["ai_status"] == "done"
    assert rows[1]["status"] == "recorded"
    assert rows[1]["ai_status"] == "pending"


def test_planned_reboot_cleanup_drops_current_partial_audio_and_marks_interval(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(interval_seconds=300),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    now = datetime(2026, 5, 14, 13, 0, tzinfo=zone)
    partial = service.paths.recordings_dir / "2026-05-14" / "20260514_120000.wav"
    write_wav(partial, duration_seconds=30.0)
    os.utime(partial, (now.timestamp(), now.timestamp()))

    changed_days = service.planned_reboot_cleanup(now.astimezone(timezone.utc))

    assert changed_days == {now.date()}
    assert not partial.exists()
    assert service._clean_shutdown_marker_path().exists()
    with service.store.connect() as conn:
        row = conn.execute(
            """
            SELECT audio_events.status, audio_events.ai_status, intervals.system_event
            FROM audio_events
            JOIN intervals USING (period_start_utc)
            """
        ).fetchone()
    assert row["status"] == "planned_reboot_partial"
    assert row["ai_status"] == "done"
    assert row["system_event"] == "PARTIALLY_PROCESSED"


def test_storage_can_keep_state_and_ai_work_off_usb(tmp_path: Path):
    config = StorageConfig(
        root=tmp_path / "usb",
        fallback_root=tmp_path / "fallback",
        state_root=tmp_path / "internal_state",
        work_root=tmp_path / "tmp_work",
    )
    paths = resolve_paths(config)

    assert paths.database_path == tmp_path / "internal_state" / "station.sqlite3"
    assert paths.ai_work_dir == tmp_path / "tmp_work"
    assert paths.recordings_dir == tmp_path / "tmp_work" / "audio_recordings"
    assert paths.logs_dir == tmp_path / "usb" / "logs"
    assert not paths.audio_dir.exists()


def test_june_storage_layout_keeps_csv_and_photos_at_usb_root(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(
            root=tmp_path / "usb",
            fallback_root=tmp_path / "fallback",
            state_root=tmp_path / "state",
            work_root=tmp_path / "work",
            logs_subdir=".",
            photos_subdir="Photos",
            photo_date_subdirs=False,
            csv_filename="2026junecameratrap.csv",
            csv_profile="june2026trap",
        ),
        schedule=ScheduleConfig(
            interval_seconds=5,
            sensor_sample_seconds=1,
            motion_capture_delay_seconds=0,
            photo_capture_disabled_start_hour=24,
            photo_capture_disabled_end_hour=25,
        ),
        speciesnet=SpeciesNetConfig(enabled=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)

    csv_path = service.run_interval(duration_seconds=1, simulate_motion=True)

    assert csv_path == tmp_path / "usb" / "2026junecameratrap.csv"
    photos = list((tmp_path / "usb" / "Photos").glob("*.jpg"))
    assert photos
    assert photos[0].parent == tmp_path / "usb" / "Photos"
    assert not (tmp_path / "usb" / "logs").exists()
    assert not (tmp_path / "usb" / "media" / "photos").exists()


def test_unmounted_mnt_usb_root_uses_fallback(tmp_path: Path):
    config = StorageConfig(root=Path("/mnt/not-mounted-juara/juara"), fallback_root=tmp_path / "fallback")

    paths = resolve_paths(config)

    assert paths.root == tmp_path / "fallback"
    assert paths.fallback_active


def test_required_unmounted_mnt_usb_root_raises(tmp_path: Path):
    config = StorageConfig(
        root=Path("/mnt/not-mounted-juara/juara"),
        fallback_root=tmp_path / "fallback",
        require_usb=True,
    )

    with pytest.raises(RuntimeError, match="mount is not active"):
        resolve_paths(config)


def test_legacy_usb_audio_folder_removed_on_startup(tmp_path: Path):
    legacy_audio = tmp_path / "usb" / "media" / "audio"
    legacy_audio.mkdir(parents=True)
    (legacy_audio / "old.wav").write_bytes(b"old")
    config = StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback")

    paths = resolve_paths(config)

    assert paths.recordings_dir == tmp_path / "fallback" / "audio_recordings"
    assert not legacy_audio.exists()


def test_audio_batch_waits_for_minimum_or_oldest_age(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        birdnet=BirdNetConfig(batch_min_files=6, batch_max_wait_seconds=3600),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    recent = now - timedelta(minutes=5)
    old = now - timedelta(hours=2)
    recent_path = tmp_path / "recent.wav"
    old_path = tmp_path / "old.wav"
    recent_path.write_bytes(b"wav")
    old_path.write_bytes(b"wav")

    service.store.upsert_audio_event(recent, "recorded", str(recent_path), recent, recent)
    assert service._audio_batch_ready(service.store.pending_audio_events(), now=now) is False

    service.store.upsert_audio_event(old, "recorded", str(old_path), old, old)
    assert service._audio_batch_ready(service.store.pending_audio_events(), now=now) is True


def test_audio_recording_pauses_from_1_to_3am(tmp_path: Path):
    class ExplodingRecorder:
        def record(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("recorder should not be called during the 1-3 AM pause")

    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(interval_seconds=5, sensor_sample_seconds=1),
        birdnet=BirdNetConfig(process_inline=False),
        speciesnet=SpeciesNetConfig(enabled=False),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.audio = ExplodingRecorder()
    zone = config.zoneinfo
    paused_at = datetime(2026, 5, 12, 1, 0, tzinfo=zone)
    service.timekeeper.now = lambda fallback_step: TimeReading(paused_at, "test")  # noqa: ARG005

    csv_path = service.run_interval(duration_seconds=1)

    assert csv_path.exists()
    assert not list(service.paths.recordings_dir.glob("**/*.wav"))
    with service.store.connect() as conn:
        row = conn.execute("SELECT status, ai_status, path FROM audio_events").fetchone()
    assert row["status"] == "recording_paused"
    assert row["ai_status"] == "done"
    assert row["path"] is None


def test_overnight_cleanup_purges_only_older_pending_audio_at_configured_cutoff(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(audio_backlog_purge_hour=3, audio_backlog_purge_minute=45),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    starts = [
        datetime(2026, 5, 12, 0, 55, tzinfo=zone),
        datetime(2026, 5, 12, 3, 40, tzinfo=zone),
        datetime(2026, 5, 12, 3, 45, tzinfo=zone),
    ]
    paths = []
    for index, period_start in enumerate(starts):
        audio_path = tmp_path / f"pending_{index}.wav"
        audio_path.write_bytes(b"wav")
        paths.append(audio_path)
        service.store.upsert_audio_event(period_start, "recorded", str(audio_path), period_start, period_start)

    changed_days = service._purge_audio_backlog_if_due(datetime(2026, 5, 12, 3, 44, tzinfo=zone))

    assert changed_days == set()
    assert all(path.exists() for path in paths)

    changed_days = service._purge_audio_backlog_if_due(datetime(2026, 5, 12, 3, 45, tzinfo=zone))

    assert changed_days == {starts[0].date()}
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert paths[2].exists()
    pending = service.store.pending_audio_events()
    assert [row["path"] for row in pending] == [str(paths[2])]
    with service.store.connect() as conn:
        rows = conn.execute("SELECT status, ai_status FROM audio_events ORDER BY period_start_utc").fetchall()
    assert [(row["status"], row["ai_status"]) for row in rows] == [
        ("purged_at_3am", "done"),
        ("purged_at_3am", "done"),
        ("recorded", "pending"),
    ]


def test_night_batch_waits_for_four_hour_boundary_even_when_many_files(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        birdnet=BirdNetConfig(batch_min_files=6, night_batch_enabled=True, night_batch_interval_seconds=4 * 60 * 60),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    start = datetime(2026, 5, 11, 18, 0, tzinfo=zone)
    for index in range(6):
        period_start = start + timedelta(minutes=5 * index)
        audio_path = tmp_path / f"night_{index}.wav"
        audio_path.write_bytes(b"wav")
        service.store.upsert_audio_event(period_start, "recorded", str(audio_path), period_start, period_start)

    assert service._audio_batch_ready(
        service.store.pending_audio_events(),
        now=datetime(2026, 5, 11, 21, 59, tzinfo=zone),
    ) is False
    assert service._audio_batch_ready(
        service.store.pending_audio_events(),
        now=datetime(2026, 5, 11, 22, 0, tzinfo=zone),
    ) is True


def test_night_batch_only_processes_rows_due_at_current_boundary(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        birdnet=BirdNetConfig(batch_min_files=6, night_batch_enabled=True, night_batch_interval_seconds=4 * 60 * 60),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    starts = [
        datetime(2026, 5, 11, 18, 0, tzinfo=zone),
        datetime(2026, 5, 11, 18, 5, tzinfo=zone),
        datetime(2026, 5, 11, 21, 55, tzinfo=zone),
        datetime(2026, 5, 11, 22, 0, tzinfo=zone),
    ]
    for index, period_start in enumerate(starts):
        audio_path = tmp_path / f"night_boundary_{index}.wav"
        audio_path.write_bytes(b"wav")
        service.store.upsert_audio_event(period_start, "recorded", str(audio_path), period_start, period_start)

    ready_rows = service._audio_rows_ready_for_processing(
        service.store.pending_audio_events(),
        now=datetime(2026, 5, 11, 22, 5, tzinfo=zone),
    )

    ready_starts = [row["period_start_utc"] for row in ready_rows]
    assert len(ready_starts) == 3
    assert starts[-1].astimezone(timezone.utc).isoformat() not in ready_starts


def test_four_hour_night_bank_stays_as_separate_five_minute_files(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        birdnet=BirdNetConfig(
            batch_min_files=6,
            batch_max_files=48,
            night_batch_enabled=True,
            night_batch_interval_seconds=4 * 60 * 60,
        ),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    service.birdnet = RecordingBirdNet()
    zone = config.zoneinfo
    starts = [datetime(2026, 5, 11, 18, 0, tzinfo=zone) + timedelta(minutes=5 * index) for index in range(48)]
    for index, period_start in enumerate(starts):
        audio_path = tmp_path / f"night_bank_{index:02d}.wav"
        audio_path.write_bytes(b"wav")
        service.store.upsert_audio_event(period_start, "recorded", str(audio_path), period_start, period_start)

    ready_rows = service._audio_rows_ready_for_processing(
        service.store.pending_audio_events(),
        now=datetime(2026, 5, 11, 22, 5, tzinfo=zone),
    )
    changed_days = service.process_audio_backlog_rows(ready_rows)

    assert changed_days == {starts[0].date()}
    assert len(service.birdnet.calls) == 1
    jobs, output_dir, _week, night = service.birdnet.calls[0]
    assert night is True
    assert output_dir.name.endswith("_night")
    assert len(jobs) == 48
    assert [job.audio_path.name for job in jobs] == [f"night_bank_{index:02d}.wav" for index in range(48)]
    assert not list(tmp_path.glob("night_bank_*.wav"))


def test_post_2am_reboot_scheduled_once_after_due_bank(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(post_audio_reboot_enabled=True, post_audio_reboot_hour=2),
        birdnet=BirdNetConfig(batch_min_files=6, night_batch_enabled=True, night_batch_interval_seconds=4 * 60 * 60),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    starts = [
        datetime(2026, 5, 11, 22, 0, tzinfo=zone),
        datetime(2026, 5, 12, 1, 55, tzinfo=zone),
        datetime(2026, 5, 12, 2, 0, tzinfo=zone),
    ]
    for index, period_start in enumerate(starts):
        audio_path = tmp_path / f"two_am_{index}.wav"
        audio_path.write_bytes(b"wav")
        service.store.upsert_audio_event(period_start, "recorded", str(audio_path), period_start, period_start)

    calls = []
    service._request_reboot_after_delay = calls.append
    now = datetime(2026, 5, 12, 2, 5, tzinfo=zone)
    ready_rows = service._audio_rows_ready_for_processing(service.store.pending_audio_events(), now=now)

    service._maybe_schedule_post_audio_reboot(ready_rows, now)
    service._maybe_schedule_post_audio_reboot(ready_rows, now)

    assert calls == [datetime(2026, 5, 12, 2, 0, tzinfo=zone)]


def test_post_audio_reboot_not_scheduled_for_10pm_bank(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        schedule=ScheduleConfig(post_audio_reboot_enabled=True, post_audio_reboot_hour=2),
        birdnet=BirdNetConfig(batch_min_files=6, night_batch_enabled=True, night_batch_interval_seconds=4 * 60 * 60),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    period_start = datetime(2026, 5, 11, 18, 0, tzinfo=zone)
    audio_path = tmp_path / "ten_pm.wav"
    audio_path.write_bytes(b"wav")
    service.store.upsert_audio_event(period_start, "recorded", str(audio_path), period_start, period_start)

    calls = []
    service._request_reboot_after_delay = calls.append
    now = datetime(2026, 5, 11, 22, 5, tzinfo=zone)
    ready_rows = service._audio_rows_ready_for_processing(service.store.pending_audio_events(), now=now)

    service._maybe_schedule_post_audio_reboot(ready_rows, now)

    assert calls == []


def test_night_start_and_end_flush_pending_bank(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        birdnet=BirdNetConfig(batch_min_files=6, night_batch_enabled=True, night_batch_interval_seconds=4 * 60 * 60),
        time=TimeConfig(rtc_write_enabled=False),
    )
    zone = config.zoneinfo
    service = StationService(config, resolve_paths(config.storage), mock=True)

    day_path = tmp_path / "day.wav"
    day_path.write_bytes(b"wav")
    day_period = datetime(2026, 5, 11, 17, 55, tzinfo=zone)
    service.store.upsert_audio_event(day_period, "recorded", str(day_path), day_period, day_period)
    assert service._audio_batch_ready(
        service.store.pending_audio_events(),
        now=datetime(2026, 5, 11, 18, 0, tzinfo=zone),
    ) is True

    service = StationService(
        config,
        resolve_paths(StorageConfig(root=tmp_path / "usb2", fallback_root=tmp_path / "fallback2")),
        mock=True,
    )
    night_path = tmp_path / "night.wav"
    night_path.write_bytes(b"wav")
    night_period = datetime(2026, 5, 12, 5, 55, tzinfo=zone)
    service.store.upsert_audio_event(night_period, "recorded", str(night_path), night_period, night_period)
    assert service._audio_batch_ready(
        service.store.pending_audio_events(),
        now=datetime(2026, 5, 12, 6, 0, tzinfo=zone),
    ) is True


def test_default_night_audio_processes_like_day_after_one_file(tmp_path: Path):
    config = StationConfig(
        storage=StorageConfig(root=tmp_path / "usb", fallback_root=tmp_path / "fallback"),
        time=TimeConfig(rtc_write_enabled=False),
    )
    service = StationService(config, resolve_paths(config.storage), mock=True)
    zone = config.zoneinfo
    period_start = datetime(2026, 5, 12, 2, 0, tzinfo=zone)
    audio_path = tmp_path / "night_immediate.wav"
    audio_path.write_bytes(b"wav")
    service.store.upsert_audio_event(period_start, "recorded", str(audio_path), period_start, period_start)

    rows = service.store.pending_audio_events()
    assert service._audio_batch_ready(rows, now=datetime(2026, 5, 12, 2, 5, tzinfo=zone)) is True
    assert service._audio_rows_ready_for_processing(rows, now=datetime(2026, 5, 12, 2, 5, tzinfo=zone)) == rows


def test_next_scheduled_capture_uses_next_local_time():
    zone = ZoneInfo("America/Cuiaba")
    now = datetime(2026, 5, 11, 7, 59, tzinfo=zone)
    assert next_scheduled_capture(now, ["08:00", "16:00"]) == datetime(2026, 5, 11, 8, 0, tzinfo=zone)

    now = datetime(2026, 5, 11, 16, 1, tzinfo=zone)
    assert next_scheduled_capture(now, ["08:00", "16:00"]) == datetime(2026, 5, 12, 8, 0, tzinfo=zone)


def test_daily_reboot_timer_runs_at_daily_checkpoints():
    timer = Path("systemd/juara-daily-reboot.timer").read_text()

    assert "OnCalendar=*-*-* 00:00:00" in timer
    assert "OnCalendar=*-*-* 04:00:00" in timer
    assert "OnCalendar=*-*-* 12:00:00" in timer
    assert "OnCalendar=*-*-* 20:00:00" in timer
