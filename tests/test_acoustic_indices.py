from pathlib import Path
import math
import wave

import pytest

from juara_station.acoustic_indices import ACOUSTIC_INDEX_VERSION, calculate_acoustic_indices


np = pytest.importorskip("numpy")


def write_tone_wav(path: Path, duration_seconds: float = 2.0, sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(duration_seconds * sample_rate), dtype=np.float32) / sample_rate
    signal = 0.25 * np.sin(2.0 * math.pi * 1500.0 * t) + 0.20 * np.sin(2.0 * math.pi * 3500.0 * t)
    samples = np.clip(signal * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def test_calculates_standard_acoustic_indices_from_wav(tmp_path: Path):
    audio_path = tmp_path / "tone.wav"
    write_tone_wav(audio_path)

    result = calculate_acoustic_indices(audio_path, n_fft=512, hop_length=256, fmax_hz=8000.0)

    assert result.acoustic_index_version == ACOUSTIC_INDEX_VERSION
    assert result.acoustic_index_error is None
    assert result.acoustic_sample_rate_hz == 16000
    assert result.acoustic_duration_s == pytest.approx(2.0)
    assert result.acoustic_n_fft == 512
    assert result.acoustic_hop_length == 256
    assert result.acoustic_aci is not None
    assert result.acoustic_aci >= 0.0
    assert result.acoustic_adi is not None
    assert result.acoustic_aei is not None
    assert result.acoustic_bioacoustic_index is not None
    assert result.acoustic_activity is not None
    assert 0.0 <= result.acoustic_activity <= 1.0
    assert result.acoustic_ndsi is not None
    assert -1.0 <= result.acoustic_ndsi <= 1.0
    assert result.acoustic_entropy_h is not None
    assert result.acoustic_rms is not None
    assert result.acoustic_rms > 0.0
