"""Optional, isolated ONNX orientation inference for local images."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import stat
import subprocess
import sys
import warnings
from dataclasses import dataclass
from typing import Final

MODEL_SHA256: Final = "cffe911c1dff47fbfbbd90110aaab9c07134645c460d35b3ae8832079bea91ba"
MODEL_SIZE: Final = 80_578_424
CACHE_VERSION: Final = 1
CONFIDENCE_THRESHOLD: Final = 0.90
MARGIN_THRESHOLD: Final = 0.25
MAX_BATCH: Final = 4
MAX_IMAGE_BYTES: Final = 32 * 1024 * 1024
MAX_IMAGE_PIXELS: Final = 40_000_000
ANGLES: Final = (0, 90, 180, 270)


class OrientationError(RuntimeError):
    """Inference could not be performed reliably."""


@dataclass(frozen=True)
class Prediction:
    angle: int | None
    score: float


def _decision(logits: list[float]) -> Prediction:
    if len(logits) != 4 or not all(math.isfinite(value) for value in logits):
        raise OrientationError("invalid model logits")
    maximum = max(logits)
    weights = [math.exp(value - maximum) for value in logits]
    total = sum(weights)
    probabilities = [value / total for value in weights]
    ranking = sorted(range(4), key=probabilities.__getitem__, reverse=True)
    score = probabilities[ranking[0]]
    margin = score - probabilities[ranking[1]]
    angle = ANGLES[ranking[0]] if score >= CONFIDENCE_THRESHOLD and margin >= MARGIN_THRESHOLD else None
    return Prediction(angle, score)


def predict(model: str, sources: list[str], *, timeout: float = 8) -> list[Prediction | None]:
    """Predict clockwise corrections; None marks an unreadable image."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise OrientationError("timeout must be positive and finite")
    if not sources:
        return []
    try:
        completed = subprocess.run(
            [sys.executable, "-B", os.path.abspath(__file__), "--worker"],
            input=json.dumps([model, sources]), text=True, capture_output=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OrientationError(f"orientation worker failed: {exc}") from exc
    if completed.returncode:
        raise OrientationError(f"orientation worker failed: {completed.stderr.strip()[:1000]}")
    try:
        data = json.loads(completed.stdout)
        if not isinstance(data, list) or len(data) != len(sources):
            raise OrientationError("invalid orientation worker response")
        results = []
        for item in data:
            if item is None:
                results.append(None)
                continue
            if not isinstance(item, list) or len(item) != 2:
                raise OrientationError("invalid orientation worker response")
            angle, score = item
            if (angle is not None and (type(angle) is not int or angle not in ANGLES)) or (
                type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1
            ):
                raise OrientationError("invalid orientation worker response")
            results.append(Prediction(angle, float(score)))
        return results
    except (ValueError, TypeError, OverflowError) as exc:
        raise OrientationError("invalid orientation worker response") from exc


def _model_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as model:
            metadata = os.fstat(model.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != MODEL_SIZE:
                raise OrientationError("invalid model size")
            data = model.read(MODEL_SIZE + 1)
    except OSError as exc:
        raise OrientationError(f"cannot read model: {exc}") from exc
    if len(data) != MODEL_SIZE or hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise OrientationError("model checksum mismatch")
    return data


def _image_tensor(path: str):
    import numpy as np
    from PIL import Image, ImageOps

    try:
        with open(path, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_IMAGE_BYTES:
                return None
            data = source.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    return None
                image = ImageOps.exif_transpose(image)
                if image.mode in ("RGB", "L"):
                    image = image.convert("RGB")
                else:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    image = Image.alpha_composite(background, rgba).convert("RGB")
                image = image.resize((416, 416), Image.Resampling.BILINEAR).crop((16, 16, 400, 400))
                array = np.asarray(image, dtype=np.float32) / np.float32(255)
                array = (array - np.array((.485, .456, .406), dtype=np.float32)) / np.array((.229, .224, .225), dtype=np.float32)
                return np.ascontiguousarray(array.transpose(2, 0, 1))
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def _infer(session, sources: list[str]) -> list[Prediction | None]:
    import numpy as np

    results: list[Prediction | None] = [None] * len(sources)
    for start in range(0, len(sources), MAX_BATCH):
        images = []
        indices = []
        for index in range(start, min(start + MAX_BATCH, len(sources))):
            tensor = _image_tensor(sources[index])
            if tensor is not None:
                images.append(tensor)
                indices.append(index)
        if not images:
            continue
        output = session.run(["output"], {"input": np.stack(images)})
        if len(output) != 1 or np.shape(output[0]) != (len(images), 4):
            raise OrientationError("invalid model output shape")
        for index, row in zip(indices, output[0]):
            results[index] = _decision([float(value) for value in row])
    return results


def _worker() -> None:
    model, sources = json.load(sys.stdin)
    data = _model_bytes(model)
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(data, sess_options=options, providers=["CPUExecutionProvider"])
    results = _infer(session, sources)
    json.dump([None if result is None else [result.angle, result.score] for result in results], sys.stdout, allow_nan=False)


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    try:
        _worker()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
