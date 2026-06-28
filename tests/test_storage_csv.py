import csv
from io import StringIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from juara_station.csv_exporter import export_day_csv
from juara_station.storage import BirdCall, BirdCandidate, DataStore, SensorSample


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
    store.upsert_audio_event(start, "recorded", "/tmp/audio.wav", start, end)
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
    photo_id = store.create_photo_event(start, start, start)
    store.update_photo_event(photo_id, status="kept", animal_name="Giant anteater", confidence=0.82)
    store.upsert_interval_summary(start, end, start, "gps")
    no_detection_start = start + timedelta(minutes=5)
    no_detection_end = no_detection_start + timedelta(minutes=5)
    store.upsert_audio_event(no_detection_start, "recorded", "/tmp/audio-empty.wav", no_detection_start, no_detection_end)
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
    assert "Giant anteater(count: 1, Conf. 82.0%)" not in text
    header = list(rows[0].keys())
    assert header.index("co2_ppm_avg") == header.index("lux_avg") + 1
    assert header.index("photos_taken") == header.index("cpu_temp_c_avg") + 1
    assert rows[0]["pressure_inhg_avg"] == "29.921"
    assert rows[0]["co2_ppm_avg"] == "432.000"
    assert rows[0]["photos_taken"] == "1"
    assert rows[0]["bird_top_species"] == "Hyacinth macaw(Calls: 2, Conf: 72.5%)"
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
    assert rows[2]["bird_calls_truncated"] == ""
    assert not (tmp_path / "juara_bird_calls.csv").exists()


def test_csv_keeps_strongest_ninety_calls_when_interval_is_busy(tmp_path: Path):
    store = DataStore(tmp_path / "station.sqlite3")
    start = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    calls = [
        BirdCall(float(index * 3), float(index * 3 + 3), (BirdCandidate(f"Bird {index:03d}", index / 100),))
        for index in range(1, 96)
    ]
    store.upsert_audio_event(start, "recorded", "/tmp/audio.wav", start, end)
    store.save_bird_calls(start, calls)
    store.upsert_interval_summary(start, end, start, "gps")

    csv_path = export_day_csv(store, tmp_path, start, ZoneInfo("America/Manaus"))
    rows = list(csv.DictReader(StringIO(csv_path.read_text())))

    assert rows[0]["bird_calls_truncated"] == "5"
    assert rows[0]["Call 1"] == "Bird 006 (6.0%)"
    assert rows[0]["Call 90"] == "Bird 095 (95.0%)"
