import csv
from io import StringIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from juara_station.acoustic_indices import ACOUSTIC_INDEX_VERSION, AcousticIndexResult
from juara_station.csv_exporter import CsvExportOptions, export_day_csv, export_main_csv
from juara_station.storage import BirdCall, BirdCandidate, DataStore, SensorSample, SoundDetection


def test_interval_summary_exports_expected_csv(tmp_path: Path):
    store = DataStore(tmp_path / "station.sqlite3")
    start = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    store.insert_sensor_sample(
        SensorSample(
            start,
            temperature_c=25.0,
            humidity_pct=80.0,
            pressure_mmhg=760.0,
            lux=1000.0,
            co2_ppm=432.0,
            cpu_temp_c=40.0,
        )
    )
    store.upsert_audio_event(start, "recorded", "/tmp/audio.wav", start, end, ai_status="done")
    store.save_bird_calls(
        start,
        [
            BirdCall(
                0.0,
                3.0,
                (BirdCandidate("Hyacinth macaw", 0.75), BirdCandidate("Blue-and-yellow macaw", 0.12)),
            ),
            BirdCall(3.0, 6.0, (BirdCandidate("Hyacinth macaw", 0.70),)),
        ],
    )
    store.save_acoustic_indices(
        start,
        AcousticIndexResult(
            acoustic_duration_s=300.0,
            acoustic_sample_rate_hz=48000,
            acoustic_n_fft=1024,
            acoustic_hop_length=512,
            acoustic_fmin_hz=0.0,
            acoustic_fmax_hz=10000.0,
            acoustic_db_threshold=-50.0,
            acoustic_activity=0.1234,
            acoustic_aci=12.3456,
            acoustic_adi=1.2345,
            acoustic_aei=0.3456,
            acoustic_bioacoustic_index=4.5678,
            acoustic_ndsi=0.2345,
            acoustic_ndsi_anthrophony=1.0,
            acoustic_ndsi_biophony=1.6,
            acoustic_entropy_h=0.4567,
            acoustic_entropy_ht=0.7654,
            acoustic_entropy_hf=0.5967,
            acoustic_rms=0.0567,
        ),
    )
    store.save_sound_detections(
        start,
        "yamnet",
        [
            SoundDetection("Bird vocalization, bird call, bird song", 0.88, category="bird"),
            SoundDetection("Frog", 0.41, category="frog"),
        ],
    )
    photo_id = store.create_photo_event(start, start, start)
    store.update_photo_event(
        photo_id,
        status="kept",
        animal_name="Giant anteater",
        confidence=0.82,
        ambient_lux=1000.0,
        camera_exposure_us=4321,
        camera_analogue_gain=1.5,
        camera_digital_gain=1.02,
        camera_lux=120.0,
        camera_ae_locked=1,
        image_mean_luma=128.5,
        image_min_luma=4,
        image_max_luma=250,
        image_dark_pct=0.1,
        image_bright_pct=2.5,
    )
    store.upsert_interval_summary(start, end, start, "gps")
    no_detection_start = start + timedelta(minutes=5)
    no_detection_end = no_detection_start + timedelta(minutes=5)
    store.upsert_audio_event(
        no_detection_start,
        "recorded",
        "/tmp/audio-empty.wav",
        no_detection_start,
        no_detection_end,
        ai_status="done",
    )
    store.upsert_interval_summary(no_detection_start, no_detection_end, no_detection_start, "gps")
    event_time = start + timedelta(minutes=10)
    store.insert_system_event(event_time, "PI_RESTARTED", "rtc")

    csv_path = export_day_csv(store, tmp_path, start, ZoneInfo("America/Manaus"))
    text = csv_path.read_text()
    rows = list(csv.DictReader(StringIO(text)))

    assert csv_path.name == "juara_station.csv"
    assert "temperature_c_avg" in text
    assert "pressure_inhg_avg" in text
    assert "co2_ppm_avg" in text
    assert "co2_estimate_ppm_avg" not in text
    assert "pressure_mmhg_avg" not in text
    assert "audio_path" not in text
    assert "notes" not in text
    assert "system_event" in text
    assert "photos_taken" in text
    assert "animal_detections" not in text
    assert "bird_detections" not in text
    assert "2026-05-10T08:00:00-04:00" not in text
    assert "2026-05-10T08:00:00" in text
    assert "Call 1" in text
    assert "Call 90" in text
    assert "acoustic_adi" in text
    assert "Giant anteater(count: 1, Conf. 82.0%)" not in text
    header = list(rows[0].keys())
    assert header.index("co2_ppm_avg") == header.index("lux_avg") + 1
    assert header.index("photos_taken") == header.index("cpu_temp_c_avg") + 1
    assert header.index("acoustic_index_version") == header.index("bird_pielou_evenness") + 1
    assert rows[0]["pressure_inhg_avg"] == "29.921"
    assert rows[0]["co2_ppm_avg"] == "432.000"
    assert rows[0]["photos_taken"] == "1"
    assert rows[0]["bird_top_species"] == "Hyacinth macaw(Calls: 2, Conf: 72.5%)"
    assert rows[0]["bird_top_genus"] == "Anodorhynchus(Calls: 2, Support: 72.5%)"
    assert rows[0]["bird_top_family"] == "Psittacidae(Calls: 2, Support: 78.5%)"
    assert rows[0]["bird_top_group"] == "macaw(Calls: 2, Support: 78.5%)"
    assert rows[0]["acoustic_index_version"] == ACOUSTIC_INDEX_VERSION
    assert rows[0]["acoustic_duration_s"] == "300.000"
    assert rows[0]["acoustic_sample_rate_hz"] == "48000"
    assert rows[0]["acoustic_aci"] == "12.346"
    assert rows[0]["acoustic_adi"] == "1.234"
    assert rows[0]["acoustic_ndsi"] == "0.234"
    assert rows[0]["yamnet_top_label"] == "Bird vocalization, bird call, bird song"
    assert rows[0]["yamnet_bird_score"] == "0.880"
    assert rows[0]["yamnet_frog_score"] == "0.410"
    assert rows[0]["bird_calls_truncated"] == "0"
    assert rows[0]["Call 1"] == "Hyacinth macaw (75.0%)\nBlue-and-yellow macaw (12.0%)"
    assert rows[0]["Call 2"] == "Hyacinth macaw (70.0%)"
    assert rows[0]["Call 3"] == ""
    assert rows[0]["Call 90"] == ""
    assert rows[1]["photos_taken"] == "0"
    assert rows[1]["bird_calls_truncated"] == "0"
    assert rows[2]["system_event"] == "PI_RESTARTED"
    assert rows[2]["timestamp_source"] == "rtc"
    assert rows[2]["photos_taken"] == ""
    assert rows[2]["acoustic_adi"] == ""
    assert rows[2]["bird_calls_truncated"] == ""
    assert not (tmp_path / "juara_bird_calls.csv").exists()

    photo_rows = list(csv.DictReader(StringIO((tmp_path / "juara_photo_diagnostics.csv").read_text())))
    assert photo_rows[0]["ambient_lux"] == "1000.000"
    assert photo_rows[0]["camera_exposure_us"] == "4321"
    assert photo_rows[0]["camera_ae_locked"] == "1"
    assert photo_rows[0]["image_mean_luma"] == "128.500"
    assert photo_rows[0]["image_bright_pct"] == "2.500"


def test_csv_keeps_strongest_ninety_calls_when_interval_is_busy(tmp_path: Path):
    store = DataStore(tmp_path / "station.sqlite3")
    start = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    calls = [
        BirdCall(float(index * 3), float(index * 3 + 3), (BirdCandidate(f"Bird {index:03d}", index / 100),))
        for index in range(1, 96)
    ]
    store.upsert_audio_event(start, "recorded", "/tmp/audio.wav", start, end, ai_status="done")
    store.save_bird_calls(start, calls)
    store.upsert_interval_summary(start, end, start, "gps")

    csv_path = export_day_csv(store, tmp_path, start, ZoneInfo("America/Manaus"))
    rows = list(csv.DictReader(StringIO(csv_path.read_text())))

    assert rows[0]["bird_calls_truncated"] == "5"
    assert rows[0]["Call 1"] == "Bird 006 (6.0%)"
    assert rows[0]["Call 90"] == "Bird 095 (95.0%)"


def test_csv_omits_pending_audio_intervals_until_ai_finishes(tmp_path: Path):
    store = DataStore(tmp_path / "station.sqlite3")
    done_start = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    pending_start = done_start + timedelta(minutes=5)
    event_time = done_start + timedelta(minutes=11)

    store.upsert_audio_event(
        done_start,
        "recorded",
        "/tmp/done.wav",
        done_start,
        done_start + timedelta(minutes=5),
        ai_status="done",
    )
    store.save_bird_calls(done_start, [BirdCall(0.0, 3.0, (BirdCandidate("Hyacinth macaw", 0.75),))])
    store.upsert_interval_summary(done_start, done_start + timedelta(minutes=5), done_start, "gps")
    store.upsert_audio_event(
        pending_start,
        "recorded",
        "/tmp/pending.wav",
        pending_start,
        pending_start + timedelta(minutes=5),
    )
    store.upsert_interval_summary(pending_start, pending_start + timedelta(minutes=5), pending_start, "gps")
    store.insert_system_event(event_time, "PI_RESTARTED", "rtc")

    csv_path = export_main_csv(store, tmp_path, timezone.utc, options=CsvExportOptions(filename="complete.csv"))
    rows = list(csv.DictReader(csv_path.open()))

    assert [row["timestamp"] for row in rows] == ["2026-05-10T12:00:00", "2026-05-10T12:11:00"]
    assert rows[0]["bird_top_species"] == "Hyacinth macaw(Calls: 1, Conf: 75.0%)"
    assert rows[1]["system_event"] == "PI_RESTARTED"
