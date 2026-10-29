"""
Второй «взгляд» на картинку: лёгкий ИИ-классификатор NSFW (Marqo/nsfw-image-detection-384, выгруженный в ONNX).

В отличие от NudeNet (ищет конкретные части тела) он оценивает картинку целиком, поэтому заметно полезнее на
рисунках и артах, где NudeNet теряется. Не знает «какая именно часть» — отдаёт одну вероятность «NSFW».

Модель опциональна: если файлов marqo_nsfw_384.onnx и marqo_nsfw_384.json нет, бот просто работает без неё
(только NudeNet). Файлы делает tools/export_marqo_onnx.py (запускается один раз на ПК) и кладутся рядом с bot.py
(или в DATA_DIR).
"""
import json
import os

from . import config
from .logging_setup import logger

MODEL_BASENAME = "marqo_nsfw_384"
KEY = "MARQO_NSFW"          # ключ в словаре оценок (рядом с классами NudeNet)

_session = None
_meta = None
_nsfw_idx = None
_tried = False


def model_paths():
    """(путь к .onnx, путь к .json) или None, если файлов нет. Ищем в DATA_DIR, затем рядом с кодом."""
    for d in (config.DATA_DIR, config.BASE_DIR):
        onnx_path = os.path.join(d, MODEL_BASENAME + ".onnx")
        json_path = os.path.join(d, MODEL_BASENAME + ".json")
        if os.path.exists(onnx_path) and os.path.exists(json_path):
            return onnx_path, json_path
    return None


def model_found():
    return model_paths() is not None


def _load():
    global _session, _meta, _nsfw_idx, _tried
    if _tried:
        return _session is not None
    _tried = True
    paths = model_paths()
    if not paths:
        logger.info("ИИ-классификатор (Marqo): файлы модели не найдены — работает только NudeNet")
        return False
    try:
        import onnxruntime as ort

        with open(paths[1], encoding="utf-8") as f:
            meta = json.load(f)
        labels = [str(x).upper() for x in meta.get("labels", [])]
        if "NSFW" not in labels:
            raise ValueError(f"в marqo_nsfw_384.json нет класса NSFW (есть: {labels})")
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False   # не раздуваем память под "про запас"
        opts.intra_op_num_threads = 1       # на слабом хостинге всё равно 0.25 vCPU
        opts.inter_op_num_threads = 1
        opts.enable_mem_pattern = False
        session = ort.InferenceSession(paths[0], sess_options=opts, providers=["CPUExecutionProvider"])
        _meta, _nsfw_idx, _session = meta, labels.index("NSFW"), session
        logger.info("ИИ-классификатор (Marqo): модель загружена (%s)", os.path.basename(paths[0]))
        return True
    except Exception as e:
        logger.error("ИИ-классификатор (Marqo): не удалось загрузить модель: %s", e)
        _session = None
        return False


def available():
    return _load()


def _preprocess(arr):
    """RGBA-массив (прозрачность уже на белом фоне, как делает nsfw._to_rgba_array) -> тензор 1x3xHxW."""
    import numpy as np
    from PIL import Image

    _, h, w = _meta["input_size"]
    resample = {"bicubic": Image.BICUBIC, "bilinear": Image.BILINEAR, "nearest": Image.NEAREST,
                "lanczos": Image.LANCZOS}.get(str(_meta.get("interpolation", "bicubic")).lower(), Image.BICUBIC)
    # Растягиваем ВЕСЬ кадр до квадрата (а не обрезаем центр, как при обучении): так ничего по краям не теряется.
    im = Image.fromarray(arr[..., :3]).resize((w, h), resample)
    x = np.asarray(im, dtype=np.float32) / 255.0
    mean = np.asarray(_meta["mean"], dtype=np.float32)
    std = np.asarray(_meta["std"], dtype=np.float32)
    x = (x - mean) / std
    return np.ascontiguousarray(x.transpose(2, 0, 1)[None])


def classify(arr):
    """Вероятность «NSFW» (0..1) для картинки или None, если модели нет / она сломалась."""
    if not _load():
        return None
    try:
        import numpy as np

        logits = _session.run(None, {_session.get_inputs()[0].name: _preprocess(arr)})[0][0].astype("float64")
        e = np.exp(logits - logits.max())
        return round(float(e[_nsfw_idx] / e.sum()), 3)
    except Exception as e:
        logger.error("ИИ-классификатор (Marqo): ошибка проверки: %s", e)
        return None
