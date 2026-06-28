from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from threading import Lock
import time
import wave

from .config import BirdNetConfig, LocationConfig, SpeciesNetConfig
from .storage import BirdCall, BirdCandidate, BirdDetection, calls_to_detections


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImagePrediction:
    label: str | None
    confidence: float | None
    blank: bool
    raw: dict


@dataclass(frozen=True)
class BirdNetAudioJob:
    period_start: datetime
    audio_path: Path


class SpeciesNetTimeoutError(RuntimeError):
    pass


class SpeciesNetUnavailableError(RuntimeError):
    pass


class SpeciesNetRunner:
    def __init__(self, config: SpeciesNetConfig, location: LocationConfig):
        self.config = config
        self.location = location
        self._direct_classifier = None

    def analyze_photo(self, photo_path: Path, work_dir: Path) -> ImagePrediction:
        if self.config.classifier_only and self.config.direct_classifier:
            if self.config.isolated_process:
                return self._analyze_photo_isolated_direct_classifier(photo_path, work_dir)
            return self._analyze_photo_direct_classifier(photo_path)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=f"{photo_path.stem}_", dir=work_dir) as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                input_dir = temp_dir / "input"
                input_dir.mkdir()
                input_photo = input_dir / photo_path.name
                try:
                    os.symlink(photo_path.resolve(), input_photo)
                except OSError:
                    shutil.copy2(photo_path, input_photo)
                predictions_json = temp_dir / f"{photo_path.stem}_speciesnet.json"
                command = [
                    self.config.python or sys.executable,
                    "-m",
                    "speciesnet.scripts.run_model",
                    "--folders",
                    str(input_dir),
                    "--predictions_json",
                    str(predictions_json),
                    "--country",
                    self.location.country,
                    "--batch_size",
                    str(self.config.batch_size),
                    "--noprogress_bars",
                    "--bypass_prompts",
                ]
                if self.config.classifier_only:
                    command.append("--classifier_only")
                if self.config.model_path:
                    command.extend(["--model", str(Path(self.config.model_path).expanduser())])
                if self.config.target_species_txt:
                    command.extend(["--target_species_txt", str(Path(self.config.target_species_txt).expanduser())])
                if self.location.admin1_region:
                    command.extend(["--admin1_region", self.location.admin1_region])
                proc = _run_speciesnet_command(command, predictions_json, timeout=1800)
                if proc.returncode != 0 and not predictions_json.exists():
                    raise RuntimeError((proc.stderr or proc.stdout or f"SpeciesNet exited {proc.returncode}").strip())
                payload = json.loads(predictions_json.read_text())
                prediction = _find_speciesnet_prediction(payload, photo_path)
                if prediction is None:
                    raise RuntimeError(f"SpeciesNet produced no prediction for {photo_path}")
                label, confidence = _speciesnet_best_result(prediction, self.config)
                blank = _is_blank_prediction(prediction, self.config)
                if blank:
                    return ImagePrediction(None, confidence, True, prediction)
                return ImagePrediction(label, confidence, False, prediction)
        finally:
            if not self.config.keep_work_outputs:
                _remove_empty_parents(work_dir)

    def unload(self) -> None:
        self._direct_classifier = None

    def _analyze_photo_direct_classifier(self, photo_path: Path) -> ImagePrediction:
        prediction = self._direct_classification_prediction(photo_path)
        label, confidence = _speciesnet_best_result(prediction, self.config)
        blank = _is_blank_prediction(prediction, self.config)
        if blank:
            return ImagePrediction(None, confidence, True, prediction)
        return ImagePrediction(label, confidence, False, prediction)

    def _analyze_photo_isolated_direct_classifier(self, photo_path: Path, work_dir: Path) -> ImagePrediction:
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=f"{photo_path.stem}_worker_", dir=work_dir) as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                output_json = temp_dir / "speciesnet_worker_result.json"
                command = [
                    self.config.python or sys.executable,
                    "-m",
                    "juara_station.speciesnet_worker",
                    "--photo",
                    str(photo_path),
                    "--output-json",
                    str(output_json),
                    "--speciesnet-config-json",
                    json.dumps(asdict(self.config)),
                    "--location-config-json",
                    json.dumps(asdict(self.location)),
                ]
                proc = _run_speciesnet_worker_command(
                    command,
                    output_json,
                    timeout=max(30, self.config.subprocess_timeout_seconds),
                    threads=max(1, self.config.subprocess_threads),
                    nice=self.config.subprocess_nice,
                    memory_limit_mb=self.config.subprocess_memory_limit_mb,
                )
                payload = json.loads(output_json.read_text()) if output_json.exists() else None
                if proc.returncode != 0:
                    message = (
                        (payload or {}).get("error")
                        or proc.stderr
                        or proc.stdout
                        or f"SpeciesNet worker exited {proc.returncode}"
                    )
                    raise RuntimeError(str(message).strip())
                if payload is None:
                    raise RuntimeError("SpeciesNet worker produced no output JSON")
                if not payload.get("ok"):
                    raise RuntimeError(str(payload.get("error") or "SpeciesNet worker failed"))
                raw = payload.get("raw") or {}
                raw["isolated_worker"] = {
                    "returncode": proc.returncode,
                    "timeout_seconds": self.config.subprocess_timeout_seconds,
                    "threads": self.config.subprocess_threads,
                }
                return ImagePrediction(
                    payload.get("label"),
                    _as_float(payload.get("confidence")),
                    bool(payload.get("blank")),
                    raw,
                )
        finally:
            if not self.config.keep_work_outputs:
                _remove_empty_parents(work_dir)

    def _direct_classification_prediction(self, photo_path: Path) -> dict:
        if self.config.blank_precheck_enabled:
            precheck = _speciesnet_blank_precheck(photo_path)
            if precheck is not None and self.config.blank_precheck_skip_classifier:
                return precheck
        self._raise_if_classifier_memory_unavailable()
        if self._direct_classifier is None:
            self._direct_classifier = _LeanSpeciesNetClassifier(
                Path(self.config.model_path).expanduser() if self.config.model_path else None,
                target_species_txt=(
                    Path(self.config.target_species_txt).expanduser() if self.config.target_species_txt else None
                ),
            )
        classifier: _LeanSpeciesNetClassifier = self._direct_classifier
        fast_size = self.config.fast_input_size
        full_size = max(1, self.config.input_size)
        if fast_size is not None and 0 < fast_size < full_size:
            prediction = classifier.predict(photo_path, input_size=fast_size)
            if _speciesnet_fast_prediction_confident(prediction, self.config):
                if not self.config.keep_classifier_loaded:
                    self._direct_classifier = None
                return prediction
            full_prediction = classifier.predict(photo_path, input_size=full_size)
            full_prediction["fast_prediction"] = prediction
            prediction = full_prediction
        else:
            prediction = classifier.predict(photo_path, input_size=full_size)
        if not self.config.keep_classifier_loaded:
            self._direct_classifier = None
        return prediction

    def _raise_if_classifier_memory_unavailable(self) -> None:
        required_mb = self.config.min_classifier_available_memory_mb
        if required_mb <= 0:
            return
        available_mb = _available_memory_mb()
        if available_mb is not None and available_mb >= required_mb:
            return
        available_text = "unknown" if available_mb is None else f"{available_mb:.0f}"
        raise SpeciesNetUnavailableError(
            "SpeciesNet classifier memory guard: "
            f"available RAM is {available_text} MB, required is {required_mb} MB"
        )


class _LeanSpeciesNetClassifier:
    DEFAULT_MODEL = Path("/home/juara2026pi1/.cache/kagglehub/models/google/speciesnet/pyTorch/v4.0.2a/1")
    MAX_CROP_RATIO = 0.3
    MAX_CROP_SIZE = 400

    def __init__(self, model_path: Path | None, target_species_txt: Path | None = None) -> None:
        import torch

        self.torch = torch
        self.device = "cpu"
        self.model_path = model_path or self.DEFAULT_MODEL
        info_path = self.model_path / "info.json"
        if not info_path.exists():
            raise RuntimeError(
                f"SpeciesNet direct classifier needs a local model_path; missing {info_path}. "
                "Run install_pi.sh to pre-stage the model before field deployment."
            )
        self.model_info = json.loads(info_path.read_text())
        try:
            configured_threads = int(os.environ.get("JUARA_SPECIESNET_THREADS", "2"))
            torch.set_num_threads(max(1, min(4, configured_threads)))
            torch.set_num_interop_threads(1)
        except (RuntimeError, ValueError):
            pass
        start = time.monotonic()
        classifier_path = self.model_path / self.model_info["classifier"]
        load_kwargs = {"map_location": self.device, "weights_only": False}
        try:
            self.model = torch.load(classifier_path, mmap=True, **load_kwargs)
        except (TypeError, RuntimeError, ValueError):
            self.model = torch.load(classifier_path, **load_kwargs)
        self.model.eval()
        for param in self.model.parameters():
            param.requires_grad = False
        self.labels = [
            line.strip() for line in (self.model_path / self.model_info["classifier_labels"]).read_text().splitlines()
        ]
        self.target_labels: list[str] = []
        self.target_idx: list[int] = []
        if target_species_txt and target_species_txt.exists():
            labels_to_idx = {label: idx for idx, label in enumerate(self.labels)}
            self.target_labels = [
                line.strip()
                for line in target_species_txt.read_text().splitlines()
                if line.strip() in labels_to_idx
            ]
            self.target_idx = [labels_to_idx[label] for label in self.target_labels]
        LOGGER.info(
            "Loaded lean SpeciesNet classifier in %.1fs with %s labels and %s target labels",
            time.monotonic() - start,
            len(self.labels),
            len(self.target_labels),
        )

    def predict(self, photo_path: Path, input_size: int) -> dict:
        import numpy as np
        from PIL import Image, ImageOps

        try:
            image = Image.open(photo_path)
            image.load()
            image = ImageOps.exif_transpose(image.convert("RGB"))
        except Exception as exc:
            LOGGER.warning("Unable to load SpeciesNet image %s: %s", photo_path, exc)
            return {"filepath": str(photo_path), "failures": ["CLASSIFIER"], "error": str(exc)}

        original_width, original_height = image.size
        if self.model_info.get("type") == "full_image":
            target_height = max(
                int(image.height * (1.0 - self.MAX_CROP_RATIO)),
                image.height - self.MAX_CROP_SIZE,
            )
            top = max(0, (image.height - target_height) // 2)
            image = image.crop((0, top, image.width, top + target_height))
        if input_size > 0:
            image = image.resize((input_size, input_size), Image.Resampling.BILINEAR)

        batch_arr = np.asarray(image, dtype=np.float32) / 255.0
        batch_tensor = self.torch.from_numpy(np.stack([batch_arr], axis=0)).to(self.device)
        start = time.monotonic()
        with self.torch.inference_mode():
            logits = self.model(batch_tensor).cpu()[0]
            scores = self.torch.softmax(logits, dim=-1)
            top_scores, top_indices = self.torch.topk(scores, k=5, dim=-1)
        elapsed = time.monotonic() - start

        classes = [self.labels[int(idx)] for idx in top_indices.numpy()]
        score_values = [float(score) for score in top_scores.numpy()]
        classifications: dict[str, list] = {
            "classes": classes,
            "scores": score_values,
        }
        if self.target_idx:
            target_scores = [float(scores[int(idx)]) for idx in self.target_idx]
            ranked_targets = sorted(
                zip(self.target_labels, target_scores),
                key=lambda item: item[1],
                reverse=True,
            )
            classifications["target_classes"] = [label for label, _ in ranked_targets]
            classifications["target_scores"] = [score for _, score in ranked_targets]

        LOGGER.info("Lean SpeciesNet inference finished in %.1fs at %spx for %s", elapsed, input_size, photo_path)
        return {
            "filepath": str(photo_path),
            "prediction": classes[0] if classes else None,
            "prediction_score": score_values[0] if score_values else None,
            "classifications": classifications,
            "inference_input_size": input_size,
            "original_width": original_width,
            "original_height": original_height,
        }


class MockSpeciesNetRunner(SpeciesNetRunner):
    def __init__(self) -> None:
        super().__init__(SpeciesNetConfig(enabled=False), LocationConfig())

    def analyze_photo(self, photo_path: Path, work_dir: Path) -> ImagePrediction:
        name = photo_path.name.lower()
        if "blank" in name:
            return ImagePrediction(None, 0.96, True, {"prediction": "blank", "prediction_score": 0.96})
        return ImagePrediction("Giant anteater", 0.823, False, {"prediction": "Giant anteater", "prediction_score": 0.823})


def _run_speciesnet_command(command: list[str], predictions_json: Path, timeout: int) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    json_seen_at: float | None = None
    while True:
        returncode = proc.poll()
        if returncode is not None:
            stdout, stderr = proc.communicate()
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        if predictions_json.exists() and predictions_json.stat().st_size > 0:
            if json_seen_at is None:
                json_seen_at = time.monotonic()
            elif time.monotonic() - json_seen_at >= 3:
                proc.terminate()
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                return subprocess.CompletedProcess(command, 0, stdout, stderr)

        if time.monotonic() >= deadline:
            proc.kill()
            stdout, stderr = proc.communicate()
            return subprocess.CompletedProcess(
                command,
                124,
                stdout,
                stderr or f"SpeciesNet timed out after {timeout} seconds",
            )

        time.sleep(1)


def _speciesnet_worker_preexec(nice: int, memory_limit_mb: int, timeout: int):
    def apply_limits() -> None:
        try:
            if nice:
                os.nice(max(-20, min(19, int(nice))))
        except OSError:
            pass
        try:
            import resource

            cpu_seconds = max(60, int(timeout) + 15)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            if memory_limit_mb and memory_limit_mb > 0:
                memory_bytes = int(memory_limit_mb) * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except (ImportError, OSError, ValueError):
            pass

    return apply_limits


def _run_speciesnet_worker_command(
    command: list[str],
    output_json: Path,
    timeout: int,
    threads: int,
    nice: int = 15,
    memory_limit_mb: int = 0,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    thread_text = str(max(1, threads))
    env.update(
        {
            "JUARA_SPECIESNET_THREADS": thread_text,
            "OMP_NUM_THREADS": thread_text,
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=(os.name == "posix"),
        preexec_fn=_speciesnet_worker_preexec(nice, memory_limit_mb, timeout) if os.name == "posix" else None,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        stdout, stderr = proc.communicate()
        output_json.unlink(missing_ok=True)
        raise SpeciesNetTimeoutError(f"SpeciesNet worker timed out after {timeout} seconds")


class BirdNetRunner:
    def __init__(self, config: BirdNetConfig, location: LocationConfig):
        self.config = config
        self.location = location
        self._inprocess_ready = False
        self._analyze_lock = Lock()

    def analyze_audio(self, audio_path: Path, output_dir: Path, recorded_at: datetime, night: bool) -> list[BirdCall]:
        output_dir.mkdir(parents=True, exist_ok=True)
        week = birdnet_week(recorded_at)
        try:
            with tempfile.TemporaryDirectory(prefix="juara-birdnet-single-") as temp_dir:
                input_path = Path(temp_dir) / f"{audio_path.stem}.wav"
                self._prepare_audio_input(audio_path, input_path)
                self._analyze_with_birdnet(input_path, output_dir, week, night, timeout=1800)
            csv_path = _latest_csv(output_dir)
            if csv_path is None:
                return []
            return parse_birdnet_calls(
                csv_path,
                min_confidence=self.config.min_confidence,
                candidate_min_confidence=self.config.candidate_min_confidence,
            )
        finally:
            if not self.config.keep_work_outputs:
                shutil.rmtree(output_dir, ignore_errors=True)

    def analyze_audio_batch(
        self, jobs: list[BirdNetAudioJob], output_dir: Path, week: int, night: bool
    ) -> dict[datetime, list[BirdCall]]:
        if not jobs:
            return {}
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="juara-birdnet-") as temp_dir:
                input_dir = Path(temp_dir) / "input"
                input_dir.mkdir()
                stems: dict[datetime, str] = {}
                for job in jobs:
                    stem = job.period_start.strftime("%Y%m%d_%H%M%S")
                    stems[job.period_start] = stem
                    input_path = input_dir / f"{stem}.wav"
                    self._prepare_audio_input(job.audio_path, input_path)

                timeout = max(1800, 900 * len(jobs))
                self._analyze_with_birdnet(input_dir, output_dir, week, night, timeout=timeout)

            detections = {}
            for period_start, stem in stems.items():
                csv_path = _csv_for_stem(output_dir, stem)
                detections[period_start] = (
                    parse_birdnet_calls(
                        csv_path,
                        min_confidence=self.config.min_confidence,
                        candidate_min_confidence=self.config.candidate_min_confidence,
                    )
                    if csv_path
                    else []
                )
            return detections
        finally:
            if not self.config.keep_work_outputs:
                shutil.rmtree(output_dir, ignore_errors=True)

    def prewarm(self, output_dir: Path, recorded_at: datetime, night: bool) -> None:
        if self.config.use_subprocess or self.config.python:
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="juara-birdnet-prewarm-") as temp_dir:
                input_path = Path(temp_dir) / "silence.wav"
                _write_silence_wav(input_path)
                self._analyze_with_birdnet(input_path, output_dir, birdnet_week(recorded_at), night, timeout=600)
        finally:
            if not self.config.keep_work_outputs:
                shutil.rmtree(output_dir, ignore_errors=True)

    def _prepare_audio_input(self, source: Path, target: Path) -> None:
        gain_db = self.config.audio_gain_db
        if gain_db == 0:
            try:
                os.symlink(source.resolve(), target)
            except OSError:
                shutil.copy2(source, target)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.config.ffmpeg_command,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-af",
            f"volume={gain_db}dB,alimiter=limit=0.95",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-sample_fmt",
            "s16",
            str(target),
        ]
        proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"ffmpeg exited {proc.returncode}").strip())

    def _analyze_with_birdnet(self, input_path: Path, output_dir: Path, week: int, night: bool, timeout: int) -> None:
        if self.config.use_subprocess or self.config.python:
            command = self._command(input_path, output_dir, week, night)
            start = time.monotonic()
            proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
            elapsed = time.monotonic() - start
            LOGGER.info("BirdNET subprocess finished in %.1fs for %s", elapsed, input_path)
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or f"BirdNET exited {proc.returncode}").strip())
            return

        start = time.monotonic()
        with self._analyze_lock:
            try:
                self._analyze_inprocess(input_path, output_dir, week, night)
            except Exception:
                LOGGER.exception("In-process BirdNET failed for %s", input_path)
                raise
        LOGGER.info("BirdNET in-process finished in %.1fs for %s", time.monotonic() - start, input_path)

    def _analyze_inprocess(self, input_path: Path, output_dir: Path, week: int, night: bool) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self._patch_birdnet_runtime()
        from birdnet_analyzer.analyze.core import analyze as birdnet_analyze

        species_list_path = self.config.species_list_path
        birdnet_analyze(
            str(input_path),
            output=str(output_dir),
            lat=-1 if species_list_path else self.location.latitude,
            lon=-1 if species_list_path else self.location.longitude,
            week=week,
            slist=species_list_path,
            sf_thresh=self.config.sf_threshold,
            min_conf=min(self.config.min_confidence, self.config.candidate_min_confidence),
            sensitivity=self.config.sensitivity_night if night else self.config.sensitivity_day,
            overlap=self.config.overlap_night if night else self.config.overlap_day,
            rtype="csv",
            threads=self.config.workers,
            batch_size=self.config.batch_size,
        )

    def _patch_birdnet_runtime(self) -> None:
        if self._inprocess_ready or not self.config.fast_tflite:
            self._inprocess_ready = True
            return
        import numpy as np
        import birdnet_analyzer.config as birdnet_config
        import birdnet_analyzer.model as birdnet_model

        from tensorflow import lite as tflite

        def fast_load_interpreter(model_path, threads):
            return tflite.Interpreter(model_path=model_path, num_threads=threads)

        original_predict = birdnet_model.predict

        def fast_predict(sample):
            if birdnet_config.CUSTOM_CLASSIFIER is not None or birdnet_config.USE_PERCH:
                return original_predict(sample)
            birdnet_model.load_model()
            if birdnet_model.PBMODEL is not None:
                return birdnet_model.PBMODEL.basic(sample)["scores"]

            sample_array = np.asarray(sample, dtype="float32")
            batch_len = len(sample_array)
            model_input = sample_array

            desired_shape = list(model_input.shape)
            if getattr(birdnet_model, "_JUARA_INPUT_SHAPE", None) != desired_shape:
                birdnet_model.INTERPRETER.resize_tensor_input(birdnet_model.INPUT_LAYER_INDEX, desired_shape)
                birdnet_model.INTERPRETER.allocate_tensors()
                birdnet_model._JUARA_INPUT_SHAPE = desired_shape

            birdnet_model.INTERPRETER.set_tensor(
                birdnet_model.INPUT_LAYER_INDEX,
                model_input,
            )
            birdnet_model.INTERPRETER.invoke()
            return birdnet_model.INTERPRETER.get_tensor(birdnet_model.OUTPUT_LAYER_INDEX)[:batch_len]

        birdnet_model._load_interpreter = fast_load_interpreter
        birdnet_model.predict = fast_predict
        self._inprocess_ready = True

    def _command(self, input_path: Path, output_dir: Path, week: int, night: bool) -> list[str]:
        species_list_path = self.config.species_list_path
        command = [
            self.config.python or sys.executable,
            "-m",
            "birdnet_analyzer.analyze",
            str(input_path),
            "-o",
            str(output_dir),
            "--lat",
            str(-1 if species_list_path else self.location.latitude),
            "--lon",
            str(-1 if species_list_path else self.location.longitude),
            "--week",
            str(week),
            "--sf_thresh",
            str(self.config.sf_threshold),
            "--min_conf",
            str(min(self.config.min_confidence, self.config.candidate_min_confidence)),
            "--sensitivity",
            str(self.config.sensitivity_night if night else self.config.sensitivity_day),
            "--overlap",
            str(self.config.overlap_night if night else self.config.overlap_day),
            "--rtype",
            "csv",
            "-t",
            str(self.config.workers),
            "-b",
            str(self.config.batch_size),
        ]
        if species_list_path:
            command.extend(["--slist", species_list_path])
        return command


class MockBirdNetRunner(BirdNetRunner):
    def __init__(self) -> None:
        super().__init__(BirdNetConfig(enabled=False), LocationConfig())

    def analyze_audio(self, audio_path: Path, output_dir: Path, recorded_at: datetime, night: bool) -> list[BirdCall]:
        if night:
            return [
                BirdCall(
                    0.0,
                    3.0,
                    (BirdCandidate("Pauraque", 0.61), BirdCandidate("Little nightjar", 0.18)),
                )
            ]
        return [
            BirdCall(
                0.0,
                3.0,
                (BirdCandidate("Hyacinth macaw", 0.72), BirdCandidate("Blue-and-yellow macaw", 0.14)),
            ),
            BirdCall(3.0, 6.0, (BirdCandidate("Hyacinth macaw", 0.68),)),
            BirdCall(6.0, 9.0, (BirdCandidate("Rufous hornero", 0.66),)),
        ]

    def analyze_audio_batch(
        self, jobs: list[BirdNetAudioJob], output_dir: Path, week: int, night: bool
    ) -> dict[datetime, list[BirdCall]]:
        return {job.period_start: self.analyze_audio(job.audio_path, output_dir, job.period_start, night) for job in jobs}


def parse_birdnet_calls(
    csv_path: Path,
    min_confidence: float = 0.25,
    candidate_min_confidence: float = 0.10,
) -> list[BirdCall]:
    grouped: dict[tuple[float | None, float | None, int], list[BirdCandidate]] = defaultdict(list)
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row_index, row in enumerate(reader):
            species = _first_present(
                row,
                [
                    "Common name",
                    "Common Name",
                    "common_name",
                    "Species",
                    "species",
                    "Label",
                    "label",
                    "Scientific name",
                    "Scientific Name",
                    "scientific_name",
                ],
            )
            if not species:
                continue
            confidence = _as_float(
                _first_present(row, ["Confidence", "confidence", "Score", "score", "Common name confidence"])
            )
            if confidence is not None and confidence < candidate_min_confidence:
                continue
            grouped[_birdnet_call_key(row, row_index)].append(BirdCandidate(species, confidence))

    calls: list[BirdCall] = []
    for start_seconds, end_seconds, _row_index in grouped:
        candidates = tuple(
            sorted(
                grouped[(start_seconds, end_seconds, _row_index)],
                key=lambda item: (-(item.confidence if item.confidence is not None else -1.0), item.species),
            )
        )
        if not candidates:
            continue
        top_confidence = candidates[0].confidence
        if top_confidence is not None and top_confidence < min_confidence:
            continue
        calls.append(BirdCall(start_seconds, end_seconds, candidates))

    return sorted(
        calls,
        key=lambda call: (-(call.top_candidate.confidence if call.top_candidate and call.top_candidate.confidence else 0.0), call.start_seconds or 0.0),
    )


def parse_birdnet_csv(
    csv_path: Path,
    min_confidence: float = 0.25,
    candidate_min_confidence: float = 0.10,
) -> list[BirdDetection]:
    return calls_to_detections(parse_birdnet_calls(csv_path, min_confidence, candidate_min_confidence))


def _birdnet_call_key(row: dict[str, str], row_index: int) -> tuple[float | None, float | None, int]:
    start_seconds = _as_float(
        _first_present(row, ["Start (s)", "Start", "start", "Begin Time (s)", "Begin Time", "begin_time"])
    )
    end_seconds = _as_float(_first_present(row, ["End (s)", "End", "end", "End Time (s)", "End Time", "end_time"]))
    if start_seconds is None and end_seconds is None:
        return None, None, row_index
    return start_seconds, end_seconds, 0


def birdnet_week(value: datetime) -> int:
    local = value
    week_in_month = min(4, ((local.day - 1) // 7) + 1)
    return (local.month - 1) * 4 + week_in_month


def _write_silence_wav(path: Path, seconds: int = 3, sample_rate: int = 48000) -> None:
    frames = b"\x00\x00" * sample_rate * seconds
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)


def _latest_csv(output_dir: Path) -> Path | None:
    csvs = sorted(output_dir.glob("*BirdNET.results.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    if csvs:
        return csvs[0]
    csvs = sorted(output_dir.glob("*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    return csvs[0] if csvs else None


def _csv_for_stem(output_dir: Path, stem: str) -> Path | None:
    expected = output_dir / f"{stem}.BirdNET.results.csv"
    if expected.exists():
        return expected
    matches = sorted(output_dir.glob(f"{stem}*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _remove_empty_parents(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return


def _speciesnet_blank_precheck(photo_path: Path) -> dict | None:
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except Exception:
        return None

    try:
        image = Image.open(photo_path)
        image.load()
        image = ImageOps.exif_transpose(image.convert("RGB"))
    except Exception:
        return None

    try:
        resampling = Image.Resampling.BILINEAR
    except AttributeError:
        resampling = Image.BILINEAR
    image = image.resize((96, 96), resampling)
    arr = np.asarray(image, dtype=np.float32)
    if arr.size == 0:
        return None
    gray = (0.299 * arr[:, :, 0]) + (0.587 * arr[:, :, 1]) + (0.114 * arr[:, :, 2])
    mean = float(gray.mean())
    std = float(gray.std())
    edge_x = np.abs(np.diff(gray, axis=1)).mean() if gray.shape[1] > 1 else 0.0
    edge_y = np.abs(np.diff(gray, axis=0)).mean() if gray.shape[0] > 1 else 0.0
    edge = float((edge_x + edge_y) / 2.0)

    reason = None
    confidence = 0.0
    if mean <= 4.0 and std <= 3.0:
        reason = "near_black"
        confidence = 0.99
    elif mean >= 252.0 and std <= 2.0:
        reason = "near_white"
        confidence = 0.99
    elif std <= 2.0 and edge <= 0.75:
        reason = "flat_frame"
        confidence = 0.97

    if reason is None:
        return None
    return {
        "filepath": str(photo_path),
        "prediction": "blank",
        "prediction_score": confidence,
        "classifications": {"classes": ["blank"], "scores": [confidence]},
        "fast_blank_precheck": {
            "reason": reason,
            "mean_luma": mean,
            "std_luma": std,
            "edge_luma": edge,
        },
    }


def _available_memory_mb() -> float | None:
    meminfo = Path("/proc/meminfo")
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    return float(parts[1]) / 1024.0
    except OSError:
        return None
    return None


def _find_speciesnet_prediction(payload: dict, photo_path: Path) -> dict | None:
    predictions = payload.get("predictions", [])
    target_resolved = photo_path.resolve()
    for prediction in predictions:
        candidate = Path(prediction.get("filepath", ""))
        try:
            if candidate.resolve() == target_resolved:
                return prediction
        except OSError:
            pass
        if candidate.name == photo_path.name:
            return prediction
    return predictions[0] if len(predictions) == 1 else None


def _speciesnet_label(value: str | None) -> str | None:
    if not value:
        return None
    if ";" in value:
        return value.split(";")[-1].strip() or value
    return value.strip()


def _speciesnet_best_result(prediction: dict, config: SpeciesNetConfig) -> tuple[str | None, float | None]:
    label = _speciesnet_label(prediction.get("prediction"))
    confidence = _as_float(prediction.get("prediction_score"))
    class_label, class_confidence = _speciesnet_top_informative_classification(prediction, config.animal_min_confidence)
    if class_label is not None:
        return class_label, class_confidence
    return label, confidence


def _speciesnet_fast_prediction_confident(prediction: dict, config: SpeciesNetConfig) -> bool:
    if prediction.get("failures"):
        return False
    label, confidence = _speciesnet_best_result(prediction, config)
    confidence = confidence or 0.0
    if _is_blank_prediction(prediction, config):
        return confidence >= config.fast_accept_min_confidence
    if label is None:
        return False
    return confidence >= config.fast_accept_min_confidence


def _speciesnet_top_informative_classification(prediction: dict, min_confidence: float) -> tuple[str | None, float | None]:
    classifications = prediction.get("classifications") or {}
    target_classes = classifications.get("target_classes") or []
    target_scores = classifications.get("target_scores") or []
    for value, score_value in zip(target_classes, target_scores):
        score = _as_float(score_value) or 0.0
        label = _speciesnet_label(value)
        if score >= min_confidence and _speciesnet_is_informative_label(label):
            return label, score
    if target_classes or target_scores:
        return None, None
    classes = classifications.get("classes") or []
    scores = classifications.get("scores") or []
    for value, score_value in zip(classes, scores):
        score = _as_float(score_value) or 0.0
        label = _speciesnet_label(value)
        if score >= min_confidence and _speciesnet_is_informative_label(label):
            return label, score
    return None, None


def _speciesnet_is_informative_label(label: str | None) -> bool:
    if not label:
        return False
    normalized = label.strip().lower()
    if not normalized:
        return False
    generic_labels = {
        "animal",
        "blank",
        "bird",
        "empty",
        "human",
        "lizards and snakes",
        "mammal",
        "no cv result",
        "person",
        "reptile",
        "unknown",
        "vehicle",
    }
    if normalized in generic_labels:
        return False
    return not normalized.startswith("unknown ")


def _is_blank_prediction(prediction: dict, config: SpeciesNetConfig) -> bool:
    label = str(prediction.get("prediction") or "").lower()
    score = _as_float(prediction.get("prediction_score")) or 0.0
    if any(token in label for token in ("blank", "empty", "vehicle", "human")) and score >= config.blank_min_confidence:
        return True
    detections = prediction.get("detections") or []
    animal_scores = [
        _as_float(det.get("conf")) or 0.0
        for det in detections
        if str(det.get("label") or det.get("category") or "").lower() in ("animal", "1")
    ]
    weak_animal_detection = not animal_scores or max(animal_scores) < config.animal_min_confidence
    if not weak_animal_detection:
        return False
    _, class_confidence = _speciesnet_top_informative_classification(prediction, config.animal_min_confidence)
    if class_confidence is not None:
        return False
    return score >= config.blank_min_confidence or not _speciesnet_is_informative_label(_speciesnet_label(prediction.get("prediction")))


def _first_present(row: dict[str, str], keys: list[str]) -> str | None:
    lowered = {key.lower(): value for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value:
            return value.strip()
        value = lowered.get(key.lower())
        if value:
            return value.strip()
    return None


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
