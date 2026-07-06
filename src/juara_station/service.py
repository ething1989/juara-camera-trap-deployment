from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
import csv
import json
import logging
import math
import os
import shutil
import shlex
import subprocess
import time
import wave

from .acoustic_indices import AcousticIndexResult, calculate_acoustic_indices
from .ai import (
    BirdNetAudioJob,
    BirdNetRunner,
    MockBirdNetRunner,
    MockSpeciesNetRunner,
    SpeciesNetRunner,
    SpeciesNetTimeoutError,
    SpeciesNetUnavailableError,
    birdnet_week,
)
from .audio import AudioRecorder, MockAudioRecorder
from .camera import MotionWatcher, create_camera, create_flash
from .config import CameraModeConfig, StationConfig, is_night
from .csv_exporter import CsvExportOptions, export_day_csv
from .geolocation import DEFAULT_GEOLOCATION_URLS, read_internet_location
from .paths import StationPaths, resolve_paths
from .sensors import MockSensorSuite, SensorSuite, read_cpu_temp
from .sound import MockYamNetRunner, YAMNET_SOURCE, YamNetRunner
from .species_pack import (
    write_active_species_list,
    write_birdnet_area_species_list,
    write_world_species_list,
)
from .storage import DataStore, SensorSample, from_iso, to_utc_iso, utc_now
from .timekeeper import TimeKeeper


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc

ENVIRONMENT_SAMPLE_FIELDS = (
    "timestamp_utc",
    "timestamp_source",
    "system_timestamp_utc",
    "monotonic_seconds",
    "boot_id",
    "temperature_c",
    "humidity_pct",
    "pressure_mmhg",
    "lux",
    "co2_ppm",
    "pm1_0_ug_m3",
    "pm2_5_ug_m3",
    "pm10_ug_m3",
    "particles_0_3_per_l",
    "particles_0_5_per_l",
    "cpu_temp_c",
    "errors",
)


class StationService:
    def __init__(self, config: StationConfig, paths: StationPaths, mock: bool = False, ai_only: bool = False):
        self.config = config
        self.paths = paths
        self.mock = mock
        self.ai_only = ai_only
        self.store = DataStore(paths.database_path)
        self.timekeeper = TimeKeeper(config.time, self.store)
        hardware_mock = mock or ai_only
        self.sensors = MockSensorSuite() if hardware_mock else SensorSuite(config.sensors)
        self.audio = MockAudioRecorder() if hardware_mock or not config.audio.enabled else AudioRecorder(config.audio)
        self.camera = create_camera(config.camera, mock=hardware_mock)
        self.flash = create_flash(config.camera, mock=hardware_mock)
        self.birdnet = MockBirdNetRunner() if mock else BirdNetRunner(config.birdnet, config.location)
        self.yamnet = MockYamNetRunner() if mock else YamNetRunner(config.yamnet)
        self.speciesnet = MockSpeciesNetRunner() if mock else SpeciesNetRunner(config.speciesnet, config.location)
        self._capture_lock = Lock()
        self._ai_lock = Lock()
        self._motion_watcher: MotionWatcher | None = None
        self._scheduled_stop = Event()
        self._scheduled_thread: Thread | None = None
        self._audio_worker_stop = Event()
        self._audio_worker_lock = Lock()
        self._audio_worker_thread: Thread | None = None
        self._image_worker_lock = Lock()
        self._image_worker_thread: Thread | None = None
        self._camera_scan_stop = Event()
        self._camera_scan_thread: Thread | None = None
        self._day_camera_mode_lock = Lock()
        self._day_camera_mode = config.camera.day
        self._coordinate_retry_lock = Lock()
        self._coordinate_retry_thread: Thread | None = None
        self._coordinate_retry_stop = Event()
        self._gps_coordinates_confirmed = False
        self._last_day_camera_scan_at: datetime | None = None
        self._last_camera_warm_restart_at: datetime | None = None
        self._birdnet_prewarm_started = False
        self._reboot_scheduled_for: date_type | None = None
        self._startup_delay_done = False
        self._fallback_storage_since: datetime | None = None
        self._recent_photo_triggers: list[float] = []
        self._photo_paused_until_monotonic: float | None = None
        self._cooldown_active = False
        self._cooldown_high_count = 0
        self._cooldown_resume_count = 0
        self._cooldown_just_entered = False
        self._current_latitude = config.time.fallback_latitude
        self._current_longitude = config.time.fallback_longitude
        self._coordinate_source = "fallback"
        self._boot_id = _current_boot_id()
        self._load_initial_coordinate_state()

    def _load_initial_coordinate_state(self) -> None:
        previous = self._read_coordinate_state()
        if previous is None:
            return
        self._current_latitude, self._current_longitude = previous
        self._coordinate_source = "past"

    def run_forever(self) -> None:
        self._sleep_startup_delay_once()
        self.paths.ensure()
        self._clear_cooldown_marker()
        startup_days = self._record_startup_events()
        startup_days.update(self._prepare_dynamic_coordinates_and_species())
        startup_days.update(self._recover_audio_recording_state(utc_now()))
        self._export_changed_days(startup_days)
        self._ensure_coordinate_retry_worker()
        self._cleanup_stale_audio_files(utc_now())
        self._sync_camera_window(utc_now(), force_refresh=True)
        self._ensure_motion_watcher()
        self._maybe_start_day_camera_scan_worker()
        if self.config.camera.enabled and self.config.camera.scheduled_capture_times and not self.mock:
            self._scheduled_thread = Thread(target=self._run_scheduled_captures, daemon=True)
            self._scheduled_thread.start()
        if (
            self.config.birdnet.enabled
            and self.config.birdnet.run_in_station_service
            and not self.config.birdnet.process_inline
            and not self.mock
        ):
            self._ensure_audio_worker()
            self._maybe_start_birdnet_prewarm()
        LOGGER.info("Juara station service started at %s", self.paths.root)
        try:
            while True:
                try:
                    self._ensure_motion_watcher()
                    self.run_interval()
                except Exception as exc:
                    LOGGER.exception("Station interval crashed; recording failure and continuing")
                    self._record_interval_crash(exc)
                    time.sleep(5)
        finally:
            self._scheduled_stop.set()
            self._audio_worker_stop.set()
            self._camera_scan_stop.set()
            self._coordinate_retry_stop.set()
            if self._motion_watcher:
                self._motion_watcher.close()
            if self._scheduled_thread:
                self._scheduled_thread.join(timeout=2)
            if self._audio_worker_thread:
                self._audio_worker_thread.join(timeout=2)
            if self._image_worker_thread:
                self._image_worker_thread.join(timeout=2)
            if self._camera_scan_thread:
                self._camera_scan_thread.join(timeout=2)
            if self._coordinate_retry_thread:
                self._coordinate_retry_thread.join(timeout=2)
            self.flash.close()
            self.camera.close()
            self._last_camera_warm_restart_at = None

    def _sleep_startup_delay_once(self) -> None:
        if self._startup_delay_done:
            return
        self._startup_delay_done = True
        delay = max(0, int(self.config.schedule.startup_delay_seconds))
        if delay <= 0 or self.mock or self.ai_only:
            return
        LOGGER.info("Waiting %s seconds before starting station hardware loops", delay)
        time.sleep(delay)

    def _maybe_switch_storage_root(self) -> None:
        if not self.paths.fallback_active:
            self._fallback_storage_since = None
            return
        try:
            refreshed = resolve_paths(self.config.storage)
        except Exception:
            return
        if refreshed.fallback_active:
            return
        if refreshed.database_path != self.paths.database_path:
            LOGGER.warning(
                "USB storage is available but live switch was skipped because database path would change: %s -> %s",
                self.paths.database_path,
                refreshed.database_path,
            )
            return
        self._copy_fallback_media_to_usb(refreshed)
        self.paths = refreshed
        self._fallback_storage_since = None
        LOGGER.warning("USB storage became available; station outputs switched to %s", self.paths.root)

    def _copy_fallback_media_to_usb(self, target_paths: StationPaths) -> None:
        self._copy_fallback_media_tree_to_usb(self.paths.photos_dir, target_paths.photos_dir)
        self._copy_fallback_media_tree_to_usb(self.paths.survey_photos_dir, target_paths.survey_photos_dir)

    def _copy_fallback_media_tree_to_usb(self, source_root: Path, target_root: Path) -> None:
        if not source_root.exists():
            return
        for source in source_root.glob("**/*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            target = target_root / relative
            if target.exists():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError:
                LOGGER.warning("Unable to copy fallback media file to USB: %s", source, exc_info=True)

    def _record_time_source_errors(self, period_start: datetime, reading) -> None:
        if reading.source == "estimated":
            if self.config.time.gps_enabled:
                self.store.add_interval_error(period_start, "GPS Connection", source="time")
            self.store.add_interval_error(period_start, "RTC Connection", source="time")

    def _check_usb_missing_watchdog(self, now: datetime, period_start: datetime) -> None:
        if not self.paths.fallback_active:
            self._fallback_storage_since = None
            return
        self.store.add_interval_error(period_start, "USB Missing", source="storage")
        reboot_after = max(0, int(self.config.schedule.usb_missing_reboot_seconds))
        if reboot_after <= 0 or self.mock or self.ai_only:
            return
        if self._fallback_storage_since is None:
            self._fallback_storage_since = now
            return
        missing_seconds = (now - self._fallback_storage_since).total_seconds()
        if missing_seconds < reboot_after:
            return
        LOGGER.warning("USB has been missing for %.0fs; rebooting to recover mount", missing_seconds)
        self._request_reboot("USB Missing watchdog")

    def _request_reboot(self, reason: str) -> None:
        if self.mock or self.ai_only:
            LOGGER.info("Mock/AI-only mode skipping reboot requested by %s", reason)
            return
        command = shlex.split(self.config.schedule.cooldown_reboot_command)
        if not command:
            LOGGER.warning("%s requested a reboot but no reboot command is configured", reason)
            return
        try:
            subprocess.run(command, check=True, timeout=30)
        except Exception:
            LOGGER.exception("%s reboot command failed", reason)

    def _record_interval_crash(self, exc: Exception) -> None:
        try:
            reading = self.timekeeper.now(fallback_step=timedelta(seconds=self.config.schedule.interval_seconds))
            timestamp = reading.timestamp
            timestamp_source = reading.source
        except Exception:
            timestamp = utc_now()
            timestamp_source = "system"
        period_start = floor_time(timestamp, self.config.schedule.interval_seconds)
        period_end = period_start + timedelta(seconds=self.config.schedule.interval_seconds)
        try:
            self.store.upsert_interval_summary(
                period_start,
                period_end,
                timestamp,
                timestamp_source,
                f"interval crash: {type(exc).__name__}",
            )
            self.store.set_interval_system_event(period_start, "STATION_INTERVAL_CRASH")
            self.store.add_interval_error(period_start, "Station Interval Crash", source="service", details=str(exc))
            self._export_day(period_start.astimezone(self.config.zoneinfo).date())
        except Exception:
            LOGGER.exception("Unable to record station interval crash")

    def run_interval(self, duration_seconds: int | None = None, simulate_motion: bool = False) -> Path:
        duration = duration_seconds or self.config.schedule.interval_seconds
        self._maybe_switch_storage_root()
        if self._cooldown_marker_exists():
            now = utc_now()
            period_start = floor_time(now, self.config.schedule.interval_seconds)
            return self._run_cooldown_interval(period_start, period_start + timedelta(seconds=duration), now, duration)
        reading = self.timekeeper.now(fallback_step=timedelta(seconds=self.config.schedule.interval_seconds))
        period_start = floor_time(reading.timestamp, self.config.schedule.interval_seconds)
        period_end = period_start + timedelta(seconds=self.config.schedule.interval_seconds)
        local_start = period_start.astimezone(self.config.zoneinfo)
        night = self._is_night(local_start)
        notes = "; ".join(filter(None, [reading.note, "fallback storage active" if self.paths.fallback_active else ""]))
        self._record_time_source_errors(period_start, reading)
        self._check_usb_missing_watchdog(reading.timestamp, period_start)
        self._sync_camera_window(reading.timestamp)
        changed_days = self._recover_audio_recording_state(reading.timestamp)
        self._cleanup_stale_audio_files(reading.timestamp)
        self._mark_stale_pending_photos(reading.timestamp)
        self._skip_expired_photo_backlog(reading.timestamp)
        changed_days.update(self._purge_audio_backlog_if_due(reading.timestamp))
        self._ensure_coordinate_retry_worker()
        if self.config.speciesnet.enabled and self.config.speciesnet.run_in_station_service and not self.mock:
            self._ensure_image_worker()

        if simulate_motion:
            self.handle_motion()

        audio_result = None
        audio_paused_reason = self._audio_paused_reason(local_start)
        if audio_paused_reason:
            self._sample_until(period_end, duration, reading.timestamp, reading.source)
            self.store.upsert_audio_event(
                period_start,
                "recording_paused",
                None,
                period_start,
                period_start,
                ai_status="done",
            )
            self.store.save_bird_calls(period_start, [])
            LOGGER.info("Skipping bird recording for %s: %s", local_start.isoformat(), audio_paused_reason)
        else:
            audio_path = self._audio_path(period_start)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.audio.record, audio_path, duration, night)
                self._sample_until(period_end, duration, reading.timestamp, reading.source)
                audio_result = future.result()

            self.store.upsert_audio_event(
                period_start,
                audio_result.status,
                str(audio_result.path) if audio_result.path else None,
                audio_result.started_at,
                audio_result.ended_at,
                ai_status="done" if audio_result.status != "recorded" else None,
                error=audio_result.error,
            )
            if audio_result.status != "recorded":
                self.store.add_interval_error(
                    period_start,
                    "Recording Failed",
                    source="audio",
                    details=audio_result.error,
                )
                if _audio_error_is_microphone_connection(audio_result.error):
                    self.store.add_interval_error(
                        period_start,
                        "Microphone Connection",
                        source="audio",
                        details=audio_result.error,
                    )
                LOGGER.warning(
                    "Audio recording failed for %s: %s",
                    period_start.isoformat(),
                    audio_result.error or "unknown error",
                )
                if audio_result.path:
                    self._delete_audio_after_ai(audio_result.path)

            if self.config.birdnet.enabled and audio_result.status == "recorded" and audio_result.path:
                if self.mock or self.config.birdnet.process_inline:
                    self.process_audio_event(period_start, audio_result.path, night)
                elif self.config.birdnet.run_in_station_service:
                    self._ensure_audio_worker()
            elif audio_result.status == "recorded" and audio_result.path:
                self._save_acoustic_indices(period_start, audio_result.path)

        if self.mock:
            changed_days.update(self.process_image_backlog(now=utc_now()))
        self.store.upsert_interval_summary(period_start, period_end, reading.timestamp, reading.source, notes or None)
        if self._cooldown_just_entered:
            self.store.set_interval_system_event(period_start, "PI_COOLDOWN")
            self._cooldown_just_entered = False
        changed_days.add(period_start.astimezone(self.config.zoneinfo).date())

        exported = None
        for day in sorted(changed_days):
            exported = self._export_day(day)
        assert exported is not None
        return exported

    def _run_cooldown_interval(
        self,
        period_start: datetime,
        period_end: datetime,
        timestamp: datetime,
        duration_seconds: int,
    ) -> Path:
        self._sample_until(period_end, duration_seconds, timestamp, "system")
        self.store.upsert_audio_event(
            period_start,
            "recording_paused",
            None,
            period_start,
            period_start,
            ai_status="done",
        )
        self.store.save_bird_calls(period_start, [])
        self.store.upsert_interval_summary(period_start, period_end, timestamp, "system")
        self.store.set_interval_system_event(period_start, "PI_COOLDOWN")
        day = period_start.astimezone(self.config.zoneinfo).date()
        return self._export_day(day)

    def _export_changed_days(self, changed_days: set[date_type]) -> None:
        for day in sorted(changed_days):
            self._export_day(day)

    def _export_day(self, day: date_type) -> Path:
        output_path = self.paths.logs_dir / self.config.storage.csv_filename
        try:
            return export_day_csv(
                self.store,
                self.paths.logs_dir,
                datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                self.config.zoneinfo,
                include_photos=self.config.camera.enabled,
                options=self._csv_export_options(),
            )
        except Exception as exc:
            LOGGER.exception("CSV export failed for %s", day)
            self.store.add_interval_error(
                floor_time(utc_now(), self.config.schedule.interval_seconds),
                "CSV Write Failed",
                source="storage",
                details=str(exc),
            )
            return output_path

    def _csv_export_options(self) -> CsvExportOptions:
        return CsvExportOptions(
            filename=self.config.storage.csv_filename,
            profile=self.config.storage.csv_profile,
            include_photos=self.config.camera.enabled,
            latitude=self._current_latitude,
            longitude=self._current_longitude,
            interval_seconds=self.config.schedule.interval_seconds,
            birdnet_species_list_path=self.config.birdnet.species_list_path,
            completed_only=True,
        )

    def _prepare_dynamic_coordinates_and_species(self, log_non_gps_event: bool = True) -> set[date_type]:
        changed_days: set[date_type] = set()
        if not self.config.time.coordinate_enabled:
            return changed_days

        try:
            reading = self.timekeeper.now(fallback_step=timedelta(seconds=0))
            now = reading.timestamp
            timestamp_source = reading.source
        except Exception:
            LOGGER.exception("Unable to timestamp coordinate selection; using system clock")
            now = utc_now()
            timestamp_source = "system"

        latitude, longitude, source, note = self._select_active_coordinates()
        self._current_latitude = latitude
        self._current_longitude = longitude
        self._coordinate_source = source
        self._gps_coordinates_confirmed = source == "gps"

        event = {
            "gps": "GPS_COORDINATES",
            "past": "PAST_COORDINATES",
            "internet": "INTERNET_COORDINATES",
            "fallback": "FALLBACK_COORDINATES",
        }.get(source, "FALLBACK_COORDINATES")
        if source == "gps" or log_non_gps_event:
            changed_days.add(self._log_interval_event(now, event, timestamp_source))
        LOGGER.warning(
            "Coordinate source selected: %s lat=%.5f lon=%.5f%s",
            source,
            latitude,
            longitude,
            f" ({note})" if note else "",
        )

        pack_root = self.config.time.species_pack_root
        output_path = self.config.time.active_species_list_path
        if output_path is None and self.config.birdnet.species_list_path:
            output_path = Path(self.config.birdnet.species_list_path)
        if pack_root is None or output_path is None:
            return changed_days
        if source != "gps" and not log_non_gps_event:
            return changed_days

        try:
            selection = self._rebuild_active_birdnet_species_list(pack_root, output_path, latitude, longitude, source)
            LOGGER.warning(
                "Active BirdNET species list ready from %s coordinates: %s species, source=%s",
                source,
                selection.species_count,
                selection.source,
            )
        except Exception as exc:
            LOGGER.exception("Dynamic BirdNET species-list rebuild failed")
            period_start = floor_time(now, self.config.schedule.interval_seconds)
            self.store.add_interval_error(period_start, "Species List Failed", source="birdnet", details=str(exc))
            changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
        return changed_days

    def _rebuild_active_birdnet_species_list(
        self,
        pack_root: Path | None,
        output_path: Path,
        latitude: float,
        longitude: float,
        coordinate_source: str,
    ):
        output = Path(output_path)
        if coordinate_source == "fallback":
            if pack_root is None:
                raise RuntimeError("No species pack configured for global fallback species list")
            return write_world_species_list(Path(pack_root), output)

        radius_km = max(1.0, float(self.config.time.species_area_radius_km))
        try:
            return write_birdnet_area_species_list(
                output,
                latitude,
                longitude,
                radius_km=radius_km,
                week=-1,
            )
        except Exception:
            LOGGER.exception(
                "BirdNET %.0f km area species-list rebuild failed; falling back to local species pack",
                radius_km,
            )
            if pack_root is None:
                raise
            return write_active_species_list(Path(pack_root), output, latitude, longitude)

    def _ensure_coordinate_retry_worker(self) -> None:
        if self.mock or self.ai_only:
            return
        if not self.config.time.coordinate_enabled or self._gps_coordinates_confirmed:
            return
        with self._coordinate_retry_lock:
            if self._coordinate_retry_thread and self._coordinate_retry_thread.is_alive():
                return
            self._coordinate_retry_stop.clear()
            self._coordinate_retry_thread = Thread(target=self._run_coordinate_retry_worker, daemon=True)
            self._coordinate_retry_thread.start()

    def _run_coordinate_retry_worker(self) -> None:
        retry_sleep = max(60, int(self.config.time.coordinate_retry_seconds))
        while not self._coordinate_retry_stop.is_set() and not self._gps_coordinates_confirmed:
            try:
                changed_days = self._retry_dynamic_coordinates_and_species()
                if changed_days:
                    self._export_changed_days(changed_days)
                if self._gps_coordinates_confirmed:
                    return
            except Exception:
                LOGGER.exception("Background GPS coordinate retry failed")
            if self._coordinate_retry_stop.wait(retry_sleep):
                return

    def _retry_dynamic_coordinates_and_species(self) -> set[date_type]:
        if self._gps_coordinates_confirmed:
            return set()
        changed_days = self._prepare_dynamic_coordinates_and_species(log_non_gps_event=False)
        if self._gps_coordinates_confirmed:
            LOGGER.warning("GPS coordinates accepted after startup retry")
        return changed_days

    def _select_active_coordinates(self) -> tuple[float, float, str, str]:
        fallback = (self.config.time.fallback_latitude, self.config.time.fallback_longitude)
        wanted = max(1, int(self.config.time.coordinate_fix_count))
        fixes = self.timekeeper.read_gps_coordinates(wanted, self.config.time.coordinate_retry_seconds)
        if len(fixes) >= wanted:
            filtered = _filter_coordinate_fixes(fixes, self.config.time.coordinate_outlier_meters)
            minimum_consistent = _minimum_consistent_fix_count(
                wanted,
                self.config.time.coordinate_min_consistent_fraction,
            )
            if len(filtered) >= minimum_consistent:
                latitude = sum(fix.latitude for fix in filtered) / len(filtered)
                longitude = sum(fix.longitude for fix in filtered) / len(filtered)
                self._write_coordinate_state(latitude, longitude, "gps")
                return latitude, longitude, "gps", f"{len(filtered)}/{len(fixes)} consistent GPS fixes kept"
            if filtered:
                LOGGER.warning(
                    "GPS coordinates were not consistent enough; kept %s/%s fixes after outlier filtering, need %s",
                    len(filtered),
                    len(fixes),
                    minimum_consistent,
                )

        previous = self._read_coordinate_state()
        if previous is not None:
            latitude, longitude = previous
            return latitude, longitude, "past", "GPS unavailable; using last accepted field coordinates"

        internet = self._read_internet_coordinates()
        if internet is not None:
            latitude, longitude, note = internet
            self._write_coordinate_state(latitude, longitude, "internet")
            return latitude, longitude, "internet", note

        return fallback[0], fallback[1], "fallback", "GPS unavailable; using backup deployment coordinates"

    def _read_internet_coordinates(self) -> tuple[float, float, str] | None:
        if not self.config.time.internet_coordinate_enabled:
            return None
        urls = self.config.time.internet_coordinate_urls or list(DEFAULT_GEOLOCATION_URLS)
        location = read_internet_location(urls, timeout_seconds=self.config.time.internet_coordinate_timeout_seconds)
        if location is None:
            return None
        label = f" ({location.label})" if location.label else ""
        return (
            location.latitude,
            location.longitude,
            f"GPS unavailable; using internet setup coordinates from {location.source_url}{label}",
        )

    def _coordinate_state_path(self) -> Path:
        return self.paths.state_dir / "active_coordinates.json"

    def _read_coordinate_state(self) -> tuple[float, float] | None:
        data = _read_json_file(self._coordinate_state_path())
        try:
            if str(data.get("source", "")).startswith("fallback"):
                return None
            return float(data["latitude"]), float(data["longitude"])
        except (KeyError, TypeError, ValueError):
            return None

    def _write_coordinate_state(self, latitude: float, longitude: float, source: str) -> None:
        try:
            path = self._coordinate_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "source": source,
                        "updated_at_utc": to_utc_iso(utc_now()),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
        except OSError:
            LOGGER.warning("Unable to persist active coordinate state", exc_info=True)

    def _record_startup_events(self) -> set[date_type]:
        changed_days: set[date_type] = set()
        try:
            reading = self.timekeeper.now(fallback_step=timedelta(seconds=0))
        except Exception:
            LOGGER.exception("Unable to read startup timestamp; using system clock")
            reading = None
        now = reading.timestamp if reading else utc_now()
        timestamp_source = reading.source if reading else "system"
        state_path = self._startup_state_path()
        clean_marker = self._clean_shutdown_marker_path()
        previous = _read_json_file(state_path)
        current_boot_id = _current_boot_id()
        clean_shutdown = clean_marker.exists()

        events: list[str] = []
        previous_boot_id = previous.get("boot_id") if isinstance(previous, dict) else None
        if previous_boot_id and previous_boot_id != current_boot_id:
            events.append("PI_RESTARTED")
            if not clean_shutdown:
                events.append("POSSIBLE_POWER_LOSS_RECOVERY")
        elif previous and not clean_shutdown:
            events.append("UNEXPECTED_STATION_RESTART_RECOVERY")
        elif previous:
            events.append("STATION_SERVICE_RESTARTED")
        else:
            events.append("STATION_STARTED")

        for event in events:
            changed_days.add(self._log_interval_event(now, event, timestamp_source))
            LOGGER.warning("System event logged: %s", event)

        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "boot_id": current_boot_id,
                        "started_at_utc": to_utc_iso(now),
                        "timestamp_source": timestamp_source,
                    },
                    indent=2,
                )
                + "\n"
            )
            clean_marker.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Unable to update station startup state", exc_info=True)
        return changed_days

    def _log_interval_event(self, timestamp: datetime, event: str, timestamp_source: str = "system") -> date_type:
        period_start = floor_time(timestamp, self.config.schedule.interval_seconds)
        period_end = period_start + timedelta(seconds=self.config.schedule.interval_seconds)
        self.store.upsert_interval_event(period_start, period_end, timestamp, timestamp_source, event)
        return period_start.astimezone(self.config.zoneinfo).date()

    def _startup_state_path(self) -> Path:
        return self.paths.state_dir / "station_start_state.json"

    def _clean_shutdown_marker_path(self) -> Path:
        return self.paths.state_dir / "clean_shutdown.marker"

    def _write_clean_shutdown_marker(self) -> None:
        try:
            marker = self._clean_shutdown_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(to_utc_iso(utc_now()) + "\n")
        except OSError:
            LOGGER.warning("Unable to write clean shutdown marker", exc_info=True)

    def _cooldown_marker_path(self) -> Path:
        return self.paths.state_dir / "cpu_cooldown.active"

    def _cooldown_marker_exists(self) -> bool:
        if self.mock:
            return self._cooldown_active
        return self._cooldown_active or self._cooldown_marker_path().exists()

    def _write_cooldown_marker(self) -> None:
        try:
            marker = self._cooldown_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(to_utc_iso(utc_now()) + "\n")
        except OSError:
            LOGGER.warning("Unable to write CPU cooldown marker", exc_info=True)

    def _clear_cooldown_marker(self) -> None:
        try:
            self._cooldown_marker_path().unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Unable to clear CPU cooldown marker", exc_info=True)

    def _stop_ai_worker_for_cooldown(self) -> None:
        if self.mock or self.ai_only:
            return
        commands = [
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "juara-ai-worker.service"],
            ["/usr/bin/sudo", "-n", "/bin/systemctl", "stop", "juara-ai-worker.service"],
        ]
        for command in commands:
            if not Path(command[2]).exists():
                continue
            try:
                proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
                if proc.returncode == 0:
                    LOGGER.warning("AI worker stopped immediately for CPU cooldown")
                    return
            except Exception:
                LOGGER.debug("AI worker cooldown stop command failed: %s", command, exc_info=True)
        LOGGER.warning("Unable to stop AI worker immediately for CPU cooldown; marker will stop the next cycle")

    def _mark_stale_pending_photos(self, now: datetime) -> None:
        stale_after = max(60, self.config.schedule.interval_seconds * 2)
        before = now - timedelta(seconds=stale_after)
        count = self.store.mark_stale_pending_photo_events(
            before,
            f"Capture did not finish within {stale_after} seconds; marked stale on service recovery",
        )
        if count:
            LOGGER.warning("Marked %s stale pending photo event(s) as camera errors", count)

    def _skip_expired_photo_backlog(self, now: datetime) -> None:
        cutoff = self._photo_processing_cutoff(now)
        count = self.store.skip_unprocessed_photo_events_before(
            cutoff,
            "Photo AI skipped after the 6 AM processing deadline",
        )
        if count:
            LOGGER.warning("Skipped %s unprocessed photo event(s) before %s", count, cutoff.isoformat())

    def _photo_processing_cutoff(self, now: datetime) -> datetime:
        local = now.astimezone(self.config.zoneinfo)
        deadline = datetime.combine(local.date(), datetime.min.time(), tzinfo=self.config.zoneinfo).replace(
            hour=self.config.schedule.photo_processing_deadline_hour
        )
        if local < deadline:
            deadline -= timedelta(days=1)
        return deadline.astimezone(UTC)

    def _ensure_motion_watcher(self) -> None:
        if self.mock or not (self.config.camera.enabled and self.config.camera.motion_enabled):
            return
        if self._motion_watcher is not None:
            return
        watcher = MotionWatcher(self.config.camera.pir_gpio, self.handle_motion)
        try:
            watcher.start()
            self._motion_watcher = watcher
            LOGGER.info("Motion detector watching GPIO%s", self.config.camera.pir_gpio)
        except Exception:
            try:
                watcher.close()
            except Exception:
                LOGGER.debug("Motion watcher cleanup after failed start failed", exc_info=True)
            LOGGER.exception("Motion detector unavailable; will retry on the next interval")

    def _save_acoustic_indices(self, period_start: datetime, audio_path: Path) -> None:
        try:
            indices = calculate_acoustic_indices(audio_path)
        except Exception as exc:
            LOGGER.warning("Acoustic index calculation failed for %s: %s", audio_path, exc, exc_info=True)
            indices = AcousticIndexResult.from_error(str(exc))
        self.store.save_acoustic_indices(period_start, indices)
        if self.config.yamnet.enabled or self.mock:
            try:
                summary = self.yamnet.analyze_audio(audio_path)
                self.store.save_sound_detections(period_start, YAMNET_SOURCE, summary.detections)
            except Exception as exc:
                LOGGER.warning("YAMNet analysis failed for %s: %s", audio_path, exc, exc_info=True)
                self.store.save_sound_analysis_error(period_start, YAMNET_SOURCE, str(exc))

    def process_audio_event(self, period_start: datetime, audio_path: Path, night: bool) -> None:
        if not audio_path.exists():
            self._mark_missing_audio(period_start, audio_path)
            return
        self._save_acoustic_indices(period_start, audio_path)
        output_dir = self.paths.ai_work_dir / "birdnet" / period_start.strftime("%Y%m%d_%H%M%S")
        try:
            calls = self.birdnet.analyze_audio(audio_path, output_dir, period_start, night)
            self.store.save_bird_calls(period_start, calls)
            self.store.upsert_audio_event(period_start, "recorded", str(audio_path), ai_status="done")
            self._delete_audio_after_ai(audio_path)
        except Exception as exc:
            LOGGER.exception("BirdNET failed for %s", audio_path)
            self.store.upsert_audio_event(period_start, "recorded", str(audio_path), ai_status="retry", error=str(exc))

    def process_audio_backlog(self) -> set:
        return self.process_audio_backlog_rows(self.store.pending_audio_events())

    def run_ai_worker_forever(self, sleep_seconds: int = 60) -> None:
        self.paths.ensure()
        self._export_changed_days(self._recover_audio_recording_state(utc_now()))
        self._cleanup_stale_audio_files(utc_now())
        sleep_seconds = max(5, sleep_seconds)
        LOGGER.info("Juara AI backlog worker started at %s", self.paths.root)
        while True:
            if self._cooldown_marker_exists():
                LOGGER.warning("AI backlog worker is paused by CPU cooldown marker")
                time.sleep(sleep_seconds)
                continue
            try:
                self.run_ai_worker_once()
            except Exception:
                LOGGER.exception("AI backlog worker cycle failed")
            time.sleep(sleep_seconds)

    def run_ai_worker_once(self, now: datetime | None = None, manage_camera: bool = False) -> set:
        now = now or utc_now()
        if self._cooldown_marker_exists():
            LOGGER.warning("Skipping AI worker cycle because CPU cooldown marker is active")
            return set()
        changed_days = set()
        changed_days.update(self._recover_audio_recording_state(now))
        self._cleanup_stale_audio_files(now)
        changed_days.update(self._purge_audio_backlog_if_due(now))
        if self.config.birdnet.enabled:
            rows = self.store.pending_audio_events()
            if rows and self._audio_batch_ready(rows, now=now):
                ready_rows = self._audio_rows_ready_for_processing(rows, now)
                if ready_rows:
                    changed_days.update(self.process_audio_backlog_rows(ready_rows))
                    self._maybe_schedule_post_audio_reboot(ready_rows, now)
        if self.config.speciesnet.enabled:
            changed_days.update(self.process_image_backlog(now=now, manage_camera=manage_camera))
        for day in sorted(changed_days):
            export_day_csv(
                self.store,
                self.paths.logs_dir,
                datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                self.config.zoneinfo,
                include_photos=self.config.camera.enabled,
                options=self._csv_export_options(),
            )
        return changed_days

    def planned_reboot_cleanup(self, now: datetime | None = None) -> set[date_type]:
        now = now or utc_now()
        self.paths.ensure()
        changed_days = self._recover_audio_recording_state(
            now,
            interrupted_status="planned_reboot_partial",
            force_current_files=True,
            system_event="PARTIALLY_PROCESSED",
        )
        self._cleanup_stale_audio_files(now)
        self._write_clean_shutdown_marker()
        self._export_changed_days(changed_days)
        LOGGER.warning("Planned reboot cleanup finished; changed_days=%s", len(changed_days))
        return changed_days

    def process_audio_backlog_rows(self, rows) -> set:
        changed_days = set()
        rows = self._drop_missing_audio_rows(rows, changed_days)
        if not rows:
            return changed_days
        with self._ai_lock:
            if self._cooldown_marker_exists():
                LOGGER.warning("Stopping BirdNET backlog before processing because CPU cooldown marker is active")
                return changed_days
            if hasattr(self.speciesnet, "unload"):
                self.speciesnet.unload()
            for group in self._audio_backlog_groups(rows):
                if self._cooldown_marker_exists():
                    LOGGER.warning("Stopping BirdNET backlog before next batch because CPU cooldown marker is active")
                    break
                for job, _row in group["jobs"]:
                    self._save_acoustic_indices(job.period_start, job.audio_path)
                try:
                    batch_detections = self.birdnet.analyze_audio_batch(
                        [job for job, _row in group["jobs"]],
                        group["output_dir"],
                        group["week"],
                        group["night"],
                    )
                    for job, row in group["jobs"]:
                        calls = batch_detections.get(job.period_start, [])
                        self.store.save_bird_calls(job.period_start, calls)
                        self.store.upsert_audio_event(
                            job.period_start,
                            "recorded",
                            str(job.audio_path),
                            ai_status="done",
                            error=None,
                        )
                        self.store.refresh_interval_summary(job.period_start, self.config.schedule.interval_seconds)
                        self._delete_audio_after_ai(job.audio_path)
                        changed_days.add(job.period_start.astimezone(self.config.zoneinfo).date())
                except Exception as exc:
                    LOGGER.exception("BirdNET batch failed")
                    for job, row in group["jobs"]:
                        self.store.upsert_audio_event(
                            job.period_start,
                            "recorded",
                            str(job.audio_path),
                            ai_status="retry",
                            error=str(exc),
                        )
        return changed_days

    def _drop_missing_audio_rows(self, rows, changed_days: set) -> list:
        ready_rows = []
        for row in rows:
            audio_path = Path(row["path"] or "")
            period_start = from_iso(row["period_start_utc"])
            if row["path"] and audio_path.exists():
                ready_rows.append(row)
                continue
            self._mark_missing_audio(period_start, audio_path)
            self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
            changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
        return ready_rows

    def _mark_missing_audio(self, period_start: datetime, audio_path: Path) -> None:
        LOGGER.warning("Skipping missing audio recording for %s: %s", period_start.isoformat(), audio_path)
        self.store.save_bird_calls(period_start, [])
        self.store.save_acoustic_indices(
            period_start,
            AcousticIndexResult.from_error(f"Missing audio recording: {audio_path}"),
        )
        self.store.upsert_audio_event(
            period_start,
            "missing_audio",
            str(audio_path) if str(audio_path) else None,
            ai_status="done",
            error=f"Missing audio recording: {audio_path}",
        )

    def _ensure_audio_worker(self) -> None:
        if self.config.birdnet.process_inline or not self.config.birdnet.enabled:
            return
        if not self.config.birdnet.run_in_station_service and not self.mock:
            return
        with self._audio_worker_lock:
            if self._audio_worker_thread and self._audio_worker_thread.is_alive():
                return
            self._audio_worker_stop.clear()
            self._audio_worker_thread = Thread(target=self._run_audio_backlog_worker, daemon=True)
            self._audio_worker_thread.start()

    def _ensure_image_worker(self) -> None:
        if not self.config.speciesnet.enabled:
            return
        if not self.config.speciesnet.run_in_station_service and not self.mock:
            return
        with self._image_worker_lock:
            if self._image_worker_thread and self._image_worker_thread.is_alive():
                return
            self._image_worker_thread = Thread(target=self._run_image_backlog_worker, daemon=True)
            self._image_worker_thread.start()

    def _run_image_backlog_worker(self) -> None:
        try:
            changed_days = self.process_image_backlog(now=utc_now())
            for day in sorted(changed_days):
                export_day_csv(
                    self.store,
                    self.paths.logs_dir,
                    datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                    self.config.zoneinfo,
                    include_photos=self.config.camera.enabled,
                    options=self._csv_export_options(),
                )
        except Exception:
            LOGGER.exception("Image AI backlog worker failed")

    def _maybe_start_birdnet_prewarm(self) -> None:
        if self.mock or not self.config.birdnet.enabled:
            return
        if not self.config.birdnet.prewarm_at_start:
            return
        if self.config.birdnet.use_subprocess or self.config.birdnet.python:
            return
        if self._birdnet_prewarm_started:
            return
        if self.store.pending_audio_events():
            return
        now = utc_now()
        self._birdnet_prewarm_started = True
        Thread(target=self._prewarm_birdnet, args=(now,), daemon=True).start()

    def _prewarm_birdnet(self, started_at: datetime) -> None:
        try:
            local = started_at.astimezone(self.config.zoneinfo)
            with self._ai_lock:
                if hasattr(self.speciesnet, "unload"):
                    self.speciesnet.unload()
                self.birdnet.prewarm(self.paths.ai_work_dir / "birdnet_prewarm", started_at, self._is_night(local))
            LOGGER.info("BirdNET prewarm finished")
            self._refresh_warm_camera("BirdNET startup", force=True)
        except Exception:
            LOGGER.exception("BirdNET prewarm failed; first real audio batch will retry normally")

    def _restart_warm_camera_after_ai(self, reason: str) -> None:
        self._refresh_warm_camera(reason, force=True)

    def _refresh_warm_camera(self, reason: str, force: bool = False, now: datetime | None = None) -> None:
        now = now or utc_now()
        if not self._camera_should_stay_warm(now):
            return
        if not self._capture_lock.acquire(blocking=False):
            return
        try:
            try:
                if force:
                    self.camera.restart()
                else:
                    self.camera.start()
                self.camera.apply_mode(self._current_day_camera_mode())
                self._last_camera_warm_restart_at = now
                LOGGER.info("Camera warm stream refreshed after %s", reason)
            except Exception:
                self._last_camera_warm_restart_at = None
                LOGGER.exception("Camera refresh after %s failed", reason)
        finally:
            self._capture_lock.release()

    def _run_audio_backlog_worker(self) -> None:
        try:
            while not self._audio_worker_stop.is_set():
                changed_days = self._purge_audio_backlog_if_due(utc_now())
                for day in sorted(changed_days):
                    export_day_csv(
                        self.store,
                        self.paths.logs_dir,
                        datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                        self.config.zoneinfo,
                        include_photos=self.config.camera.enabled,
                        options=self._csv_export_options(),
                    )
                rows = self.store.pending_audio_events()
                if not rows:
                    break
                now = utc_now()
                if not self._audio_batch_ready(rows, now=now):
                    if self._audio_worker_stop.wait(60):
                        break
                    continue
                ready_rows = self._audio_rows_ready_for_processing(rows, now)
                if not ready_rows:
                    if self._audio_worker_stop.wait(60):
                        break
                    continue
                changed_days = self.process_audio_backlog_rows(ready_rows)
                for day in sorted(changed_days):
                    export_day_csv(
                        self.store,
                        self.paths.logs_dir,
                        datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                        self.config.zoneinfo,
                        include_photos=self.config.camera.enabled,
                        options=self._csv_export_options(),
                    )
                self._maybe_schedule_post_audio_reboot(ready_rows, now)
        except Exception:
            LOGGER.exception("Audio AI backlog worker failed")

    def _recover_audio_recording_state(
        self,
        now: datetime,
        interrupted_status: str = "interrupted_power_loss",
        force_current_files: bool = False,
        system_event: str | None = None,
    ) -> set[date_type]:
        changed_days = self._recover_orphan_audio_recordings(
            now,
            interrupted_status=interrupted_status,
            force_current_files=force_current_files,
            system_event=system_event,
        )
        changed_days.update(
            self._recover_interrupted_audio_events(
                now,
                interrupted_status=interrupted_status,
                force_current_files=force_current_files,
                system_event=system_event,
            )
        )
        return changed_days

    def _recover_orphan_audio_recordings(
        self,
        now: datetime,
        interrupted_status: str = "interrupted_power_loss",
        force_current_files: bool = False,
        system_event: str | None = None,
    ) -> set[date_type]:
        changed_days: set[date_type] = set()
        if not self.paths.recordings_dir.exists():
            return changed_days
        current_file_grace_seconds = max(60, min(self.config.schedule.interval_seconds, 120))
        if force_current_files:
            current_file_grace_seconds = 0
        complete_threshold_seconds = max(1.0, self.config.schedule.interval_seconds * 0.90)
        for audio_path in sorted(self.paths.recordings_dir.glob("**/*.wav")):
            if self.store.audio_event_for_path(audio_path) is not None:
                continue
            try:
                age_seconds = now.timestamp() - audio_path.stat().st_mtime
            except OSError:
                continue
            if age_seconds < current_file_grace_seconds:
                continue
            period_start = self._period_start_from_audio_path(audio_path)
            if period_start is None:
                continue
            duration_seconds = _wav_duration_seconds(audio_path)
            if duration_seconds >= complete_threshold_seconds:
                ended_at = period_start + timedelta(seconds=duration_seconds)
                self.store.upsert_audio_event(
                    period_start,
                    "recorded",
                    str(audio_path),
                    period_start,
                    ended_at,
                    raw_json={"recovered_after_restart": True, "duration_seconds": duration_seconds},
                )
                self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
                changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
                LOGGER.warning(
                    "Recovered orphan audio recording after restart: %s duration=%.1fs",
                    audio_path,
                    duration_seconds,
                )
            else:
                self.store.upsert_audio_event(
                    period_start,
                    interrupted_status,
                    str(audio_path),
                    period_start,
                    now,
                    ai_status="done",
                    error=f"Audio recording interrupted before completion; duration {duration_seconds:.1f}s",
                )
                self.store.save_bird_calls(period_start, [])
                self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
                if system_event:
                    self.store.set_interval_system_event(period_start, system_event)
                self._delete_audio_after_ai(audio_path)
                changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
                LOGGER.warning(
                    "Deleted interrupted orphan audio recording after restart: %s duration=%.1fs",
                    audio_path,
                    duration_seconds,
                )
        return changed_days

    def _recover_interrupted_audio_events(
        self,
        now: datetime,
        interrupted_status: str = "interrupted_power_loss",
        force_current_files: bool = False,
        system_event: str | None = None,
    ) -> set[date_type]:
        changed_days: set[date_type] = set()
        current_file_grace_seconds = max(60, min(self.config.schedule.interval_seconds, 120))
        if force_current_files:
            current_file_grace_seconds = 0
        complete_threshold_seconds = max(1.0, self.config.schedule.interval_seconds * 0.90)
        for row in self.store.pending_audio_events():
            period_start = from_iso(row["period_start_utc"])
            audio_path = Path(row["path"] or "")
            if not row["path"] or not audio_path.exists():
                if (now - period_start).total_seconds() < self.config.schedule.interval_seconds + current_file_grace_seconds:
                    continue
                self._mark_missing_audio(period_start, audio_path)
                self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
                changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
                continue

            try:
                age_seconds = now.timestamp() - audio_path.stat().st_mtime
            except OSError:
                continue
            if age_seconds < current_file_grace_seconds:
                continue

            duration_seconds = _wav_duration_seconds(audio_path)
            if duration_seconds >= complete_threshold_seconds:
                continue

            self.store.upsert_audio_event(
                period_start,
                interrupted_status,
                str(audio_path),
                period_start,
                now,
                ai_status="done",
                error=f"Audio recording interrupted before completion; duration {duration_seconds:.1f}s",
            )
            self.store.save_bird_calls(period_start, [])
            self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
            if system_event:
                self.store.set_interval_system_event(period_start, system_event)
            self._delete_audio_after_ai(audio_path)
            changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
            LOGGER.warning(
                "Deleted interrupted pending audio recording after restart: %s duration=%.1fs",
                audio_path,
                duration_seconds,
            )
        return changed_days

    def _period_start_from_audio_path(self, audio_path: Path) -> datetime | None:
        try:
            local = datetime.strptime(audio_path.stem, "%Y%m%d_%H%M%S").replace(tzinfo=self.config.zoneinfo)
        except ValueError:
            return None
        return local.astimezone(UTC)

    def _cleanup_stale_audio_files(self, now: datetime) -> int:
        if not self.config.audio.delete_recordings_after_ai:
            return 0
        if not self.paths.recordings_dir.exists():
            return 0

        min_orphan_age_seconds = max(900, self.config.schedule.interval_seconds * 2)
        removed = 0
        for audio_path in sorted(self.paths.recordings_dir.glob("**/*.wav")):
            try:
                age_seconds = now.timestamp() - audio_path.stat().st_mtime
            except OSError:
                continue
            row = self.store.audio_event_for_path(audio_path)
            if row is None and age_seconds < min_orphan_age_seconds:
                continue
            if row is not None and row["status"] == "recorded" and row["ai_status"] in ("pending", "retry"):
                continue
            self._delete_audio_after_ai(audio_path)
            removed += 1

        for directory in sorted(self.paths.recordings_dir.glob("**/*"), reverse=True):
            if not directory.is_dir():
                continue
            try:
                directory.rmdir()
            except OSError:
                pass
        if removed:
            LOGGER.warning("Cleaned up %s stale internal audio recording(s)", removed)
        return removed

    def _purge_audio_backlog_if_due(self, now: datetime) -> set:
        local = now.astimezone(self.config.zoneinfo)
        purge_hour = self.config.schedule.audio_backlog_purge_hour % 24
        purge_minute = max(0, min(59, int(self.config.schedule.audio_backlog_purge_minute)))
        cutoff_local = datetime.combine(local.date(), datetime.min.time(), tzinfo=self.config.zoneinfo).replace(
            hour=purge_hour,
            minute=purge_minute,
        )
        if local < cutoff_local:
            return set()
        return self._purge_audio_backlog_before(cutoff_local.astimezone(UTC))

    def _purge_audio_backlog_before(self, cutoff: datetime) -> set:
        changed_days = set()
        rows = [row for row in self.store.pending_audio_events() if from_iso(row["period_start_utc"]) < cutoff]
        if not rows:
            return changed_days
        reason = "Unprocessed bird recording purged at the overnight AI catch-up cutoff"
        for row in rows:
            period_start = from_iso(row["period_start_utc"])
            audio_path = Path(row["path"] or "")
            if row["path"]:
                self._delete_audio_after_ai(audio_path)
            self.store.upsert_audio_event(
                period_start,
                "purged_at_3am",
                row["path"],
                ai_status="done",
                error=reason,
            )
            self.store.save_bird_calls(period_start, [])
            self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
            changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
        LOGGER.warning("Purged %s pending bird recording(s) older than %s", len(rows), cutoff.isoformat())
        return changed_days

    def _audio_batch_ready(self, rows, now: datetime | None = None) -> bool:
        if not rows:
            return False
        now = now or utc_now()
        if self.config.birdnet.night_batch_enabled:
            if any(self._audio_event_due_on_night_schedule(row, now) for row in rows):
                return True
            if self._is_night(now.astimezone(self.config.zoneinfo)):
                return False
        min_files = max(1, self.config.birdnet.batch_min_files)
        if len(rows) >= min_files:
            return True
        oldest = from_iso(rows[0]["period_start_utc"])
        max_wait = max(0, self.config.birdnet.batch_max_wait_seconds)
        return (now - oldest).total_seconds() >= max_wait

    def _audio_event_due_on_night_schedule(self, row, now: datetime) -> bool:
        period_start = from_iso(row["period_start_utc"])
        due_at = self._next_night_audio_flush_after(period_start)
        return due_at is not None and now.astimezone(self.config.zoneinfo) >= due_at

    def _delete_audio_after_ai(self, audio_path: Path) -> None:
        if not self.config.audio.delete_recordings_after_ai:
            return
        try:
            audio_path.unlink(missing_ok=True)
            audio_path.with_name(f"._{audio_path.name}").unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Unable to delete processed audio recording %s", audio_path, exc_info=True)

    def _audio_rows_ready_for_processing(self, rows, now: datetime):
        if not self.config.birdnet.night_batch_enabled:
            return rows
        due_rows = [row for row in rows if self._audio_event_due_on_night_schedule(row, now)]
        if due_rows:
            return due_rows
        if self._is_night(now.astimezone(self.config.zoneinfo)):
            return []
        return rows

    def _maybe_schedule_post_audio_reboot(self, rows, now: datetime) -> None:
        due_at = self._post_audio_reboot_due_at(rows, now)
        if due_at is None:
            return
        reboot_day = due_at.date()
        if self._reboot_scheduled_for == reboot_day:
            return
        self._reboot_scheduled_for = reboot_day
        self._request_reboot_after_delay(due_at)

    def _post_audio_reboot_due_at(self, rows, now: datetime) -> datetime | None:
        if not self.config.schedule.post_audio_reboot_enabled:
            return None
        now_local = now.astimezone(self.config.zoneinfo)
        candidates = []
        for row in rows:
            period_start = from_iso(row["period_start_utc"])
            due_at = self._next_night_audio_flush_after(period_start)
            if due_at is None:
                continue
            if due_at > now_local:
                continue
            if due_at.hour == self.config.schedule.post_audio_reboot_hour:
                candidates.append(due_at)
        return min(candidates) if candidates else None

    def _request_reboot_after_delay(self, due_at: datetime) -> None:
        delay_seconds = max(0, self.config.schedule.post_audio_reboot_delay_seconds)
        command = shlex.split(self.config.schedule.post_audio_reboot_command)
        if not command:
            LOGGER.warning("Post-audio reboot requested for %s but reboot command is empty", due_at.isoformat())
            return
        if self.mock:
            LOGGER.info("Mock mode skipping post-audio reboot for %s", due_at.isoformat())
            return
        LOGGER.warning(
            "%02d:00 audio bank finished and CSV exported; rebooting in %s seconds with: %s",
            due_at.hour,
            delay_seconds,
            " ".join(command),
        )
        if delay_seconds:
            time.sleep(delay_seconds)
        try:
            subprocess.run(command, check=True, timeout=30)
        except Exception:
            LOGGER.exception("Post-audio reboot command failed")

    def _next_night_audio_flush_after(self, value: datetime) -> datetime | None:
        interval = max(1, self.config.birdnet.night_batch_interval_seconds)
        local = value.astimezone(self.config.zoneinfo)
        local_day = local.date()
        for offset in (-1, 0, 1, 2):
            due = self._next_night_boundary_after(local, local_day + timedelta(days=offset), interval)
            if due is not None:
                return due
        return None

    def _next_night_boundary_after(
        self, local: datetime, window_start_day: date_type, interval_seconds: int
    ) -> datetime | None:
        zone = self.config.zoneinfo
        start = datetime.combine(window_start_day, datetime.min.time(), tzinfo=zone).replace(
            hour=self.config.schedule.night_start_hour
        )
        end_day = window_start_day
        if self.config.schedule.night_start_hour >= self.config.schedule.night_end_hour:
            end_day = window_start_day + timedelta(days=1)
        end = datetime.combine(end_day, datetime.min.time(), tzinfo=zone).replace(
            hour=self.config.schedule.night_end_hour
        )

        boundary = start
        step = timedelta(seconds=interval_seconds)
        while boundary <= end:
            if boundary > local:
                return boundary
            boundary += step
        return None

    def _audio_backlog_groups(self, rows):
        grouped = defaultdict(list)
        for row in rows:
            if not row["path"]:
                continue
            period_start = from_iso(row["period_start_utc"])
            local = period_start.astimezone(self.config.zoneinfo)
            key = (birdnet_week(period_start), self._is_night(local))
            grouped[key].append((BirdNetAudioJob(period_start, Path(row["path"])), row))

        max_files = max(1, self.config.birdnet.batch_max_files)
        for (week, night), jobs in grouped.items():
            for index in range(0, len(jobs), max_files):
                chunk = jobs[index : index + max_files]
                first_start = chunk[0][0].period_start
                last_start = chunk[-1][0].period_start
                suffix = "night" if night else "day"
                output_dir = (
                    self.paths.ai_work_dir
                    / "birdnet"
                    / f"batch_{first_start.strftime('%Y%m%d_%H%M%S')}_{last_start.strftime('%H%M%S')}_{suffix}"
                )
                yield {"week": week, "night": night, "jobs": chunk, "output_dir": output_dir}

    def process_image_backlog(self, now: datetime | None = None, manage_camera: bool = True) -> set:
        if not self.config.speciesnet.enabled:
            return set()
        now = now or utc_now()
        self._skip_expired_photo_backlog(now)
        if self.store.pending_audio_events():
            LOGGER.info("Skipping SpeciesNet image processing because audio AI is pending")
            return set()
        now_local = now.astimezone(self.config.zoneinfo)
        ready_rows = []
        for row in self.store.pending_photo_events():
            triggered = from_iso(row["triggered_at_utc"])
            triggered_local = triggered.astimezone(self.config.zoneinfo)
            if not self._photo_ai_deferred(triggered_local, now_local):
                ready_rows.append(row)
        max_photos = max(1, self.config.speciesnet.max_photos_per_run)
        ready_rows = ready_rows[:max_photos]
        if not ready_rows:
            return set()

        if not self._ai_lock.acquire(blocking=False):
            LOGGER.info("Skipping SpeciesNet image processing because BirdNET is busy")
            return set()
        changed_days = set()
        camera_paused = False
        try:
            if manage_camera and self._camera_should_stay_warm(now):
                if not self._capture_lock.acquire(blocking=False):
                    LOGGER.info("Skipping SpeciesNet image processing because the camera is busy")
                    return set()
                camera_paused = True
                try:
                    self.camera.close()
                    LOGGER.info("Camera warm stream closed before SpeciesNet image processing")
                except Exception:
                    LOGGER.exception("Camera close before SpeciesNet image processing failed")
                    self._capture_lock.release()
                    camera_paused = False
                    return set()
            for row in ready_rows:
                photo_path = Path(row["path"])
                period_start = from_iso(row["period_start_utc"])
                try:
                    prediction = self.speciesnet.analyze_photo(photo_path, self.paths.ai_work_dir / "speciesnet")
                    if prediction.blank:
                        if self.config.speciesnet.delete_blanks:
                            photo_path.unlink(missing_ok=True)
                        self.store.update_photo_event(
                            row["id"],
                            status="deleted_blank",
                            ai_status="done",
                            confidence=prediction.confidence,
                            raw_json=prediction.raw,
                        )
                    else:
                        self.store.update_photo_event(
                            row["id"],
                            status="kept",
                            ai_status="done",
                            animal_name=prediction.label,
                            confidence=prediction.confidence,
                            raw_json=prediction.raw,
                        )
                    self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
                    changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
                except Exception as exc:
                    LOGGER.exception("SpeciesNet failed for %s", photo_path)
                    if _speciesnet_failure_is_terminal(exc):
                        self.store.update_photo_event(row["id"], status="kept", ai_status="error", error=str(exc))
                        self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
                        changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
                    else:
                        self.store.update_photo_event(row["id"], ai_status="retry", error=str(exc))
        finally:
            if camera_paused:
                try:
                    self.camera.start()
                    LOGGER.info("Camera warm stream restarted after SpeciesNet image processing")
                except Exception:
                    LOGGER.exception("Camera restart after SpeciesNet image processing failed")
                finally:
                    self._capture_lock.release()
            if self.store.pending_audio_events() and hasattr(self.speciesnet, "unload"):
                self.speciesnet.unload()
            self._ai_lock.release()
        return changed_days

    def handle_motion(self) -> None:
        if self._cooldown_marker_exists():
            LOGGER.warning("Ignoring motion trigger because CPU cooldown mode is active")
            return
        Thread(target=self._capture_motion_photo, daemon=True).start()

    def handle_scheduled_capture(self, scheduled_at: datetime | None = None) -> None:
        if self._cooldown_marker_exists():
            LOGGER.warning("Ignoring scheduled capture because CPU cooldown mode is active")
            return
        Thread(target=self._capture_scheduled_photo, args=(scheduled_at,), daemon=True).start()

    def _run_scheduled_captures(self) -> None:
        times = self.config.camera.scheduled_capture_times
        while not self._scheduled_stop.is_set():
            try:
                reading = self.timekeeper.now(fallback_step=timedelta(seconds=60))
                now_utc = reading.timestamp
                now_local = now_utc.astimezone(self.config.zoneinfo)
                next_local = next_scheduled_capture(now_local, times)
                next_utc = next_local.astimezone(UTC)
                delay = max(0.0, (next_utc - now_utc).total_seconds())
                if self._scheduled_stop.wait(delay):
                    break
                self.handle_scheduled_capture(next_utc)
            except Exception:
                LOGGER.exception("Scheduled camera loop failed")
                self._scheduled_stop.wait(60)

    def _capture_motion_photo(self) -> None:
        triggered_at = utc_now()
        delay = self.config.schedule.motion_capture_delay_seconds
        if self._is_night(triggered_at.astimezone(self.config.zoneinfo)):
            delay = self.config.schedule.night_motion_capture_delay_seconds
        self._capture_photo(delay, "motion", triggered_at=triggered_at)

    def _capture_scheduled_photo(self, scheduled_at: datetime | None = None) -> None:
        self._capture_photo(0.0, "scheduled", triggered_at=scheduled_at)

    def _capture_photo(self, delay_seconds: float, source: str, triggered_at: datetime | None = None) -> None:
        triggered_at = triggered_at or utc_now()
        local_trigger = triggered_at.astimezone(self.config.zoneinfo)
        if self._photo_capture_disabled(local_trigger):
            LOGGER.info(
                "Ignoring %s photo trigger at %s because photo capture is disabled",
                source,
                local_trigger.isoformat(),
            )
            return
        if self._photo_rate_limited(source, local_trigger):
            return
        if not self._capture_lock.acquire(blocking=False):
            LOGGER.info("Ignoring %s photo trigger at %s because camera is busy", source, local_trigger.isoformat())
            return
        try:
            target_at = triggered_at + timedelta(seconds=delay_seconds)
            period_start = floor_time(triggered_at, self.config.schedule.interval_seconds)
            night = self._is_night(local_trigger)
            photo_id = self.store.create_photo_event(period_start, triggered_at, target_at)
            path = self._photo_path(triggered_at, photo_id, source=source)
            target_monotonic_ns = time.monotonic_ns() + int(delay_seconds * 1e9)
            LOGGER.info(
                "%s photo trigger id=%s trigger=%s target_delay_seconds=%.3f target=%s",
                source.capitalize(),
                photo_id,
                triggered_at.isoformat(),
                delay_seconds,
                target_at.isoformat(),
            )
            if night:
                try:
                    self.flash.on()
                except Exception:
                    LOGGER.exception("Flash activation failed")
            try:
                result = self.camera.capture_at(
                    path,
                    target_monotonic_ns,
                    self.config.camera.night if night else self._current_day_camera_mode(),
                )
                if night:
                    time.sleep(self.config.schedule.flash_after_capture_seconds)
                    try:
                        self.flash.off()
                    except Exception:
                        LOGGER.exception("Flash deactivation failed")
                if result.status == "captured" and result.path:
                    trigger_latency = (result.captured_at - triggered_at).total_seconds()
                    target_offset = (result.captured_at - target_at).total_seconds()
                    photo_diagnostics = self._photo_diagnostics(result)
                    LOGGER.info(
                        "%s photo captured id=%s trigger_to_capture_seconds=%.3f target_offset_seconds=%.3f path=%s",
                        source.capitalize(),
                        photo_id,
                        trigger_latency,
                        target_offset,
                        result.path,
                    )
                    next_status = "captured" if self.config.speciesnet.enabled else "kept"
                    next_ai_status = "pending" if self.config.speciesnet.enabled else "skipped"
                    self.store.update_photo_event(
                        photo_id,
                        status=next_status,
                        ai_status=next_ai_status,
                        captured_at_utc=result.captured_at,
                        path=str(result.path),
                        **photo_diagnostics,
                    )
                else:
                    self.store.update_photo_event(photo_id, status="error", ai_status="error", error=result.error)
                    self.store.add_interval_error(
                        period_start,
                        "Camera Failed",
                        source="camera",
                        details=result.error,
                    )
            except Exception as exc:
                LOGGER.exception("%s photo capture failed", source.capitalize())
                self.store.update_photo_event(photo_id, status="error", ai_status="error", error=str(exc))
                self.store.add_interval_error(
                    period_start,
                    "Camera Failed",
                    source="camera",
                    details=str(exc),
                )
        finally:
            try:
                try:
                    self.flash.off()
                except Exception:
                    LOGGER.exception("Flash cleanup failed")
                if not self._camera_should_stay_warm(utc_now()):
                    try:
                        self.camera.close()
                    except Exception:
                        LOGGER.exception("Camera close failed")
            finally:
                self._capture_lock.release()

    def _current_day_camera_mode(self) -> CameraModeConfig:
        with self._day_camera_mode_lock:
            return self._day_camera_mode

    def _photo_diagnostics(self, result) -> dict:
        diagnostics = {
            "ambient_lux": None,
            "camera_exposure_us": result.exposure_time_us,
            "camera_analogue_gain": result.analogue_gain,
            "camera_digital_gain": result.digital_gain,
            "camera_lux": result.camera_lux,
            "camera_ae_locked": None if result.ae_locked is None else int(result.ae_locked),
        }
        try:
            diagnostics["ambient_lux"] = self.store.latest_lux_before(result.captured_at)
        except Exception:
            LOGGER.exception("Unable to attach ambient lux to photo diagnostics")
        if result.path is not None:
            diagnostics.update(_photo_luma_stats(result.path))
        return diagnostics

    def _set_day_camera_mode(self, mode: CameraModeConfig) -> None:
        with self._day_camera_mode_lock:
            self._day_camera_mode = mode

    def _maybe_start_day_camera_scan_worker(self) -> None:
        if self.mock or self.ai_only:
            return
        if not (self.config.camera.enabled and self.config.camera.motion_enabled and self.config.camera.day_scan_enabled):
            return
        if self._camera_scan_thread and self._camera_scan_thread.is_alive():
            return
        self._camera_scan_stop.clear()
        self._camera_scan_thread = Thread(target=self._run_day_camera_scan_worker, daemon=True)
        self._camera_scan_thread.start()

    def _run_day_camera_scan_worker(self) -> None:
        LOGGER.info(
            "Day camera tuning scan enabled every %ss from %02d:00 to %02d:00",
            self.config.camera.day_scan_interval_seconds,
            self.config.camera.day_scan_start_hour,
            self.config.camera.day_scan_end_hour,
        )
        while not self._camera_scan_stop.is_set():
            now = utc_now()
            try:
                if self._day_camera_scan_due(now):
                    self._run_day_camera_scan(now)
            except Exception:
                LOGGER.exception("Day camera tuning scan failed")
            self._camera_scan_stop.wait(60)

    def _day_camera_scan_due(self, now: datetime) -> bool:
        local = now.astimezone(self.config.zoneinfo)
        if not _hour_in_window(
            local.hour,
            self.config.camera.day_scan_start_hour,
            self.config.camera.day_scan_end_hour,
        ):
            return False
        if self._last_day_camera_scan_at is None:
            return True
        interval = max(60, self.config.camera.day_scan_interval_seconds)
        return (now - self._last_day_camera_scan_at).total_seconds() >= interval

    def _run_day_camera_scan(self, now: datetime) -> None:
        if not self._camera_should_stay_warm(now):
            return
        if not self._capture_lock.acquire(blocking=False):
            LOGGER.info("Skipping day camera tuning scan because camera is busy")
            return
        try:
            candidates = _parse_camera_scan_candidates(self.config.camera.day_scan_candidates)
            candidates = _cap_camera_scan_candidates(candidates, self.config.camera.max_exposure_us)
            if not candidates:
                LOGGER.warning("Skipping day camera tuning scan because no valid candidates are configured")
                self._last_day_camera_scan_at = now
                return
            selected = self._scan_day_camera_candidates(candidates)
            if selected is not None:
                self._set_day_camera_mode(selected)
            self._last_day_camera_scan_at = now
            LOGGER.info("Day camera tuning scan finished; keeping existing warm stream open")
        finally:
            self._capture_lock.release()

    def _scan_day_camera_candidates(self, candidates: list[CameraModeConfig]) -> CameraModeConfig | None:
        try:
            from PIL import Image, ImageStat
        except Exception:
            LOGGER.exception("PIL is unavailable; cannot run day camera tuning scan")
            return None

        scan_dir = Path("/tmp/juara-camera-scan")
        scan_dir.mkdir(parents=True, exist_ok=True)
        best: tuple[float, CameraModeConfig, dict] | None = None
        for index, mode in enumerate(candidates, start=1):
            path = scan_dir / f"scan_{int(time.time())}_{index}.jpg"
            settle_path = scan_dir / f"scan_{int(time.time())}_{index}_settle.jpg"
            try:
                settle_result = self.camera.capture_at(settle_path, time.monotonic_ns(), mode)
                settle_path.unlink(missing_ok=True)
                settle_path.with_name(f"._{settle_path.name}").unlink(missing_ok=True)
                if settle_result.status != "captured":
                    LOGGER.warning("Day camera scan settle frame failed mode=%s error=%s", mode, settle_result.error)
                    continue
                time.sleep(0.05)
                result = self.camera.capture_at(path, time.monotonic_ns(), mode)
                if result.status != "captured" or not path.exists():
                    LOGGER.warning("Day camera scan candidate failed mode=%s error=%s", mode, result.error)
                    continue
                image = Image.open(path).convert("L")
                stat = ImageStat.Stat(image)
                mean = float(stat.mean[0])
                stddev = float(stat.stddev[0])
                extrema = image.getextrema()
                histogram = image.histogram()
                pixels = max(1, image.width * image.height)
                dark_pct = sum(histogram[:8]) * 100.0 / pixels
                bright_pct = sum(histogram[248:]) * 100.0 / pixels
                score = _day_camera_scan_score(
                    mode,
                    mean,
                    stddev,
                    dark_pct,
                    bright_pct,
                    self.config.camera.day_scan_target_luma,
                )
                metrics = {
                    "mean_luma": mean,
                    "stddev_luma": stddev,
                    "dark_pct": dark_pct,
                    "bright_pct": bright_pct,
                    "min_luma": extrema[0],
                    "max_luma": extrema[1],
                    "bytes": path.stat().st_size,
                    "score": score,
                }
                LOGGER.info(
                    "Day camera scan candidate exposure_us=%s gain=%s score=%.2f mean=%.1f dark=%.1f%% bright=%.1f%%",
                    mode.exposure_us,
                    mode.analogue_gain,
                    score,
                    mean,
                    dark_pct,
                    bright_pct,
                )
                if best is None or score < best[0]:
                    best = (score, mode, metrics)
            except Exception:
                LOGGER.exception("Day camera scan candidate crashed for mode=%s", mode)
            finally:
                path.unlink(missing_ok=True)
                path.with_name(f"._{path.name}").unlink(missing_ok=True)
                time.sleep(0.15)
        try:
            scan_dir.rmdir()
        except OSError:
            pass
        if best is None:
            LOGGER.warning("Day camera tuning scan found no usable candidate; keeping current mode=%s", self._current_day_camera_mode())
            return None
        _score, mode, metrics = best
        LOGGER.info(
            "Day camera tuning selected exposure_us=%s gain=%s mean=%.1f dark=%.1f%% bright=%.1f%% score=%.2f",
            mode.exposure_us,
            mode.analogue_gain,
            metrics["mean_luma"],
            metrics["dark_pct"],
            metrics["bright_pct"],
            metrics["score"],
        )
        return mode

    def _sample_until(
        self,
        period_end: datetime,
        duration_seconds: int,
        timestamp_start: datetime | None = None,
        timestamp_source: str = "system",
    ) -> None:
        deadline = time.monotonic() + duration_seconds
        timestamp_monotonic_start = time.monotonic()
        sample_every = max(1, self.config.schedule.sensor_sample_seconds)
        while True:
            try:
                raw_sample = self.sensors.sample()
                sample = self._sample_with_station_time(raw_sample, timestamp_start, timestamp_monotonic_start)
                self._append_environment_sample(sample, timestamp_source, raw_sample.sampled_at)
                self.store.insert_sensor_sample(sample)
                self._update_cooldown_counts(sample.cpu_temp_c)
                sample_period = floor_time(sample.sampled_at, self.config.schedule.interval_seconds)
                for error in sample.errors:
                    self.store.add_interval_error(sample_period, error, source="sensor")
            except Exception:
                LOGGER.exception("Sensor sample failed")
                self.store.add_interval_error(
                    floor_time(utc_now(), self.config.schedule.interval_seconds),
                    "Sensor Failed",
                    source="sensor",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(sample_every, remaining))

    def _sample_with_station_time(
        self,
        sample: SensorSample,
        timestamp_start: datetime | None,
        timestamp_monotonic_start: float,
    ) -> SensorSample:
        if timestamp_start is None:
            return sample
        elapsed = max(0.0, time.monotonic() - timestamp_monotonic_start)
        return replace(sample, sampled_at=timestamp_start + timedelta(seconds=elapsed))

    def _append_environment_sample(
        self,
        sample: SensorSample,
        timestamp_source: str,
        system_sampled_at: datetime,
    ) -> None:
        path = self.paths.logs_dir / self.config.storage.environment_csv_filename
        row = {
            "timestamp_utc": to_utc_iso(sample.sampled_at),
            "timestamp_source": timestamp_source,
            "system_timestamp_utc": to_utc_iso(system_sampled_at),
            "monotonic_seconds": f"{time.monotonic():.3f}",
            "boot_id": self._boot_id,
            "temperature_c": sample.temperature_c,
            "humidity_pct": sample.humidity_pct,
            "pressure_mmhg": sample.pressure_mmhg,
            "lux": sample.lux,
            "co2_ppm": sample.co2_ppm,
            "pm1_0_ug_m3": sample.pm1_0_ug_m3,
            "pm2_5_ug_m3": sample.pm2_5_ug_m3,
            "pm10_ug_m3": sample.pm10_ug_m3,
            "particles_0_3_per_l": sample.particles_0_3_per_l,
            "particles_0_5_per_l": sample.particles_0_5_per_l,
            "cpu_temp_c": sample.cpu_temp_c,
            "errors": "; ".join(sample.errors),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=ENVIRONMENT_SAMPLE_FIELDS)
                if needs_header:
                    writer.writeheader()
                writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            LOGGER.exception("Raw environmental sample CSV append failed")

    def _sample_cpu_only_until(self, duration_seconds: int) -> None:
        deadline = time.monotonic() + duration_seconds
        sample_every = max(1, self.config.schedule.sensor_sample_seconds)
        while True:
            sampled_at = utc_now()
            try:
                cpu_temp = read_cpu_temp()
                self.store.insert_sensor_sample(
                    SensorSample(
                        sampled_at=sampled_at,
                        cpu_temp_c=cpu_temp,
                    )
                )
                self._update_cooldown_counts(cpu_temp)
            except Exception:
                LOGGER.exception("CPU temperature sample failed during cooldown")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(sample_every, remaining))

    def _update_cooldown_counts(self, cpu_temp_c: float | None) -> None:
        if cpu_temp_c is None:
            return
        high = self.config.schedule.cooldown_high_temp_c
        resume = self.config.schedule.cooldown_resume_temp_c
        needed = max(1, int(self.config.schedule.cooldown_consecutive_readings))
        if self._cooldown_active:
            if cpu_temp_c < resume:
                self._cooldown_resume_count += 1
            else:
                self._cooldown_resume_count = 0
            if self._cooldown_resume_count >= needed:
                LOGGER.warning(
                    "CPU cooled below %.1f C for %s readings; rebooting to resume nominal operation",
                    resume,
                    needed,
                )
                self._request_reboot("CPU cooldown complete")
            return

        if cpu_temp_c >= high:
            self._cooldown_high_count += 1
        else:
            self._cooldown_high_count = 0
        if self._cooldown_high_count >= needed:
            self._cooldown_active = True
            self._cooldown_just_entered = True
            self._cooldown_resume_count = 0
            self._write_cooldown_marker()
            self._stop_ai_worker_for_cooldown()
            LOGGER.warning("CPU cooldown mode entered after %s readings at or above %.1f C", needed, high)

    def _is_night(self, value: datetime) -> bool:
        return is_night(value.hour, self.config.schedule.night_start_hour, self.config.schedule.night_end_hour)

    def _audio_recording_disabled(self, local: datetime) -> bool:
        return is_night(
            local.hour,
            self.config.schedule.audio_recording_disabled_start_hour,
            self.config.schedule.audio_recording_disabled_end_hour,
        )

    def _audio_paused_reason(self, local: datetime) -> str | None:
        if self._audio_recording_disabled(local):
            return "overnight AI catch-up window"
        if self._sd_free_space_low():
            return "SD card free space below threshold"
        return None

    def _sd_free_space_low(self) -> bool:
        if self.mock or self.ai_only:
            return False
        threshold = float(self.config.schedule.sd_low_free_percent)
        if threshold <= 0:
            return False
        try:
            usage = shutil.disk_usage(self.paths.recordings_dir)
        except OSError:
            try:
                usage = shutil.disk_usage(self.paths.fallback_root)
            except OSError:
                return False
        if usage.total <= 0:
            return False
        free_percent = usage.free * 100.0 / usage.total
        if free_percent >= threshold:
            return False
        LOGGER.warning("Pausing audio recording because SD free space is %.1f%% below %.1f%%", free_percent, threshold)
        return True

    def _camera_should_stay_warm(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        local = now.astimezone(self.config.zoneinfo)
        return (
            self.config.camera.enabled
            and self.config.camera.motion_enabled
            and not self._cooldown_marker_exists()
            and not self._photo_capture_disabled(local)
        )

    def _photo_capture_disabled(self, local: datetime) -> bool:
        if self.config.schedule.photo_capture_disabled_start_hour == self.config.schedule.photo_capture_disabled_end_hour:
            return False
        return is_night(
            local.hour,
            self.config.schedule.photo_capture_disabled_start_hour,
            self.config.schedule.photo_capture_disabled_end_hour,
        )

    def _photo_rate_limited(self, source: str, local_trigger: datetime) -> bool:
        limit = int(self.config.camera.max_photos_per_minute)
        if limit <= 0:
            return False
        now = time.monotonic()
        if self._photo_paused_until_monotonic is not None:
            if now < self._photo_paused_until_monotonic:
                LOGGER.warning(
                    "Ignoring %s photo trigger at %s because photo capture is paused after a burst",
                    source,
                    local_trigger.isoformat(),
                )
                return True
            self._photo_paused_until_monotonic = None

        cutoff = now - 60.0
        self._recent_photo_triggers = [value for value in self._recent_photo_triggers if value >= cutoff]
        if len(self._recent_photo_triggers) >= limit:
            pause_seconds = max(1, int(self.config.camera.photo_rate_pause_seconds))
            self._photo_paused_until_monotonic = now + pause_seconds
            LOGGER.warning(
                "Photo trigger burst hit %s photos/minute; pausing captures for %s seconds",
                limit,
                pause_seconds,
            )
            return True
        self._recent_photo_triggers.append(now)
        return False

    def _sync_camera_window(self, now: datetime, force_refresh: bool = False) -> None:
        if not (self.config.camera.enabled and self.config.camera.motion_enabled):
            return
        if not self._capture_lock.acquire(blocking=False):
            return
        try:
            if self._camera_should_stay_warm(now):
                try:
                    if force_refresh or self._camera_warm_refresh_due(now):
                        self.camera.restart()
                        self._last_camera_warm_restart_at = now
                        LOGGER.info("Camera warm stream refreshed during photo window")
                    elif self._last_camera_warm_restart_at is None:
                        self.camera.start()
                        self._last_camera_warm_restart_at = now
                    self.camera.apply_mode(self._current_day_camera_mode())
                except Exception:
                    self._last_camera_warm_restart_at = None
                    self.store.add_interval_error(
                        floor_time(now, self.config.schedule.interval_seconds),
                        "Camera Connection",
                        source="camera",
                    )
                    LOGGER.exception("Camera warmup failed during enabled photo window")
            else:
                try:
                    self.camera.close()
                    self._last_camera_warm_restart_at = None
                except Exception:
                    LOGGER.exception("Camera close failed during disabled photo window")
        finally:
            self._capture_lock.release()

    def _camera_warm_refresh_due(self, now: datetime) -> bool:
        interval = int(self.config.camera.warm_restart_interval_seconds)
        if interval <= 0:
            return self._last_camera_warm_restart_at is None
        if self._last_camera_warm_restart_at is None:
            return True
        return (now - self._last_camera_warm_restart_at).total_seconds() >= interval

    def _photo_ai_deferred(self, photo_local: datetime, now_local: datetime) -> bool:
        if not self.config.schedule.image_ai_defer_enabled:
            return False
        photo_in_defer_window = is_night(
            photo_local.hour,
            self.config.schedule.image_ai_defer_start_hour,
            self.config.schedule.image_ai_defer_end_hour,
        )
        now_in_defer_window = is_night(
            now_local.hour,
            self.config.schedule.image_ai_defer_start_hour,
            self.config.schedule.image_ai_defer_end_hour,
        )
        return photo_in_defer_window and now_in_defer_window

    def _audio_path(self, period_start: datetime) -> Path:
        local = period_start.astimezone(self.config.zoneinfo)
        return self.paths.recordings_dir / local.strftime("%Y-%m-%d") / f"{local.strftime('%Y%m%d_%H%M%S')}.wav"

    def _photo_path(self, triggered_at: datetime, photo_id: int, source: str = "motion") -> Path:
        local = triggered_at.astimezone(self.config.zoneinfo)
        prefix = "survey" if source == "scheduled" else "pic"
        filename = f"{local.strftime('%Y%m%d_%H%M%S')}_{prefix}{photo_id:03d}.jpg"
        photos_dir = self.paths.survey_photos_dir if source == "scheduled" else self.paths.photos_dir
        if self.config.storage.photo_date_subdirs:
            return photos_dir / local.strftime("%Y-%m-%d") / filename
        return photos_dir / filename


def floor_time(value: datetime, interval_seconds: int) -> datetime:
    value = value.astimezone(UTC)
    epoch = int(value.timestamp())
    floored = epoch - (epoch % interval_seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


def next_scheduled_capture(now_local: datetime, scheduled_times: list[str]) -> datetime:
    if now_local.tzinfo is None:
        raise ValueError("now_local must be timezone-aware")
    candidates = []
    for scheduled_time in scheduled_times:
        hour, minute = _parse_scheduled_time(scheduled_time)
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    if not candidates:
        raise ValueError("At least one scheduled capture time is required")
    return min(candidates)


def _hour_in_window(hour: int, start_hour: int, end_hour: int) -> bool:
    hour %= 24
    start_hour %= 24
    end_hour %= 24
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _parse_camera_scan_candidates(values: list[str]) -> list[CameraModeConfig]:
    candidates = []
    for value in values:
        try:
            exposure_text, gain_text = str(value).split(":", 1)
            exposure_us = int(float(exposure_text))
            analogue_gain = float(gain_text)
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid day camera scan candidate %r; expected exposure_us:gain", value)
            continue
        if exposure_us <= 0 or analogue_gain <= 0:
            LOGGER.warning("Ignoring invalid day camera scan candidate %r; values must be positive", value)
            continue
        candidates.append(CameraModeConfig(exposure_us=exposure_us, analogue_gain=analogue_gain))
    return candidates


def _cap_camera_scan_candidates(candidates: list[CameraModeConfig], max_exposure_us: int) -> list[CameraModeConfig]:
    max_exposure_us = max(1, int(max_exposure_us))
    capped = []
    seen: set[tuple[int | None, float | None, str | None]] = set()
    for mode in candidates:
        exposure_us = mode.exposure_us
        if exposure_us is not None and exposure_us > max_exposure_us:
            LOGGER.warning(
                "Clamping day camera scan candidate exposure from %sus to configured maximum %sus",
                exposure_us,
                max_exposure_us,
            )
            exposure_us = max_exposure_us
        next_mode = CameraModeConfig(exposure_us=exposure_us, analogue_gain=mode.analogue_gain, denoise=mode.denoise)
        key = (next_mode.exposure_us, next_mode.analogue_gain, next_mode.denoise)
        if key in seen:
            continue
        seen.add(key)
        capped.append(next_mode)
    return capped


def _day_camera_scan_score(
    mode: CameraModeConfig,
    mean_luma: float,
    stddev_luma: float,
    dark_pct: float,
    bright_pct: float,
    target_luma: float,
) -> float:
    exposure_us = float(mode.exposure_us or 0)
    gain = float(mode.analogue_gain or 1.0)
    brightness_penalty = abs(mean_luma - target_luma)
    clipping_penalty = (dark_pct * 1.2) + (bright_pct * 4.0)
    speed_penalty = (exposure_us / 1000.0) * 0.45
    gain_penalty = gain * 1.6
    contrast_bonus = min(25.0, stddev_luma) * 0.15
    return brightness_penalty + clipping_penalty + speed_penalty + gain_penalty - contrast_bonus


def _photo_luma_stats(path: Path) -> dict[str, float | int | None]:
    try:
        from PIL import Image, ImageStat
    except Exception:
        LOGGER.exception("PIL is unavailable; cannot compute photo diagnostics")
        return {}

    try:
        with Image.open(path) as image:
            gray = image.convert("L")
            stat = ImageStat.Stat(gray)
            histogram = gray.histogram()
            pixels = max(1, gray.width * gray.height)
            extrema = gray.getextrema()
        return {
            "image_mean_luma": float(stat.mean[0]),
            "image_min_luma": int(extrema[0]),
            "image_max_luma": int(extrema[1]),
            "image_dark_pct": sum(histogram[:8]) * 100.0 / pixels,
            "image_bright_pct": sum(histogram[248:]) * 100.0 / pixels,
        }
    except Exception:
        LOGGER.exception("Unable to compute photo diagnostics for %s", path)
        return {}


def _speciesnet_failure_is_terminal(exc: Exception) -> bool:
    if isinstance(exc, (SpeciesNetTimeoutError, SpeciesNetUnavailableError)):
        return True
    text = str(exc).lower()
    terminal_tokens = (
        "classifier memory guard",
        "std::bad_alloc",
        "memoryerror",
        "out of memory",
        "cannot allocate memory",
        "killed",
    )
    return any(token in text for token in terminal_tokens)


def _audio_error_is_microphone_connection(error: str | None) -> bool:
    if not error:
        return False
    text = error.lower()
    tokens = (
        "no such device",
        "cannot get card index",
        "audio open error",
        "unknown pcm",
        "device or resource busy",
        "no soundcards found",
    )
    return any(token in text for token in tokens)


def _parse_scheduled_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise ValueError(f"Invalid scheduled capture time: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid scheduled capture time: {value!r}")
    return hour, minute


def _filter_coordinate_fixes(fixes, outlier_meters: float) -> list:
    if len(fixes) < 3 or outlier_meters <= 0:
        return list(fixes)
    median_lat = _median([fix.latitude for fix in fixes])
    median_lon = _median([fix.longitude for fix in fixes])
    outlier_km = outlier_meters / 1000.0
    filtered = [
        fix
        for fix in fixes
        if _haversine_km(fix.latitude, fix.longitude, median_lat, median_lon) <= outlier_km
    ]
    return filtered


def _minimum_consistent_fix_count(wanted: int, fraction: float) -> int:
    wanted = max(1, int(wanted))
    try:
        fraction = float(fraction)
    except (TypeError, ValueError):
        fraction = 0.8
    fraction = max(0.0, min(1.0, fraction))
    return max(1, min(wanted, math.ceil(wanted * fraction)))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _wav_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            frame_rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            if frame_rate <= 0 or channels <= 0 or sample_width <= 0:
                return 0.0
            header_duration = float(handle.getnframes()) / float(frame_rate)
            data_bytes_on_disk = max(0, path.stat().st_size - 44)
            byte_rate = frame_rate * channels * sample_width
            disk_duration = data_bytes_on_disk / float(byte_rate)
            return min(header_duration, disk_duration)
    except (OSError, EOFError, wave.Error):
        return 0.0
