"""
Фильтр откровенного контента (порно) на базе NudeNet.

Модель маленькая (ONNX, ~12 МБ в самом пакете, ~40 мс на картинку на обычном CPU,
~130 МБ памяти) и работает локально — картинки никуда не отправляются.

Что важно понимать про возможности (без прикрас):
- Модель ищет ОТКРЫТЫЕ части тела на изображении. Контекст она не понимает:
  сцены в белье, текст на картинке, намёки и т.п. она не поймает.
- Нарисованное (аниме, хентай, мультяшные стикеры) определяется заметно хуже, чем фото.
- Анимированные стикеры (.tgs) проверяются по кадру-превью, видео/гифки — по нескольким кадрам.
- Ложные срабатывания возможны (скульптуры, медицинские фото, фото с пляжа) —
  поэтому в боте есть уровень строгости, порог и кнопка «это не порно» для админа.

Если nudenet не установлен (или не загрузился), модуль тихо сообщает available() == False,
а бот продолжает работать без этого фильтра.
"""

import io
import os
import tempfile

from . import nsfw_cls
from .logging_setup import logger

# Классы модели -> минимальный уровень строгости, начиная с которого они считаются нарушением.
#   уровень 1: только явные гениталии и анус
#   уровень 2: + открытая женская грудь
#   уровень 3: + открытые ягодицы
#   уровень 4: + закрытые зоны (бельё, купальник) — много ложных срабатываний, включать осознанно
CLASS_LEVELS = {
    "FEMALE_GENITALIA_EXPOSED": 1,
    "MALE_GENITALIA_EXPOSED": 1,
    "ANUS_EXPOSED": 1,
    "FEMALE_BREAST_EXPOSED": 2,
    "BUTTOCKS_EXPOSED": 3,
    "FEMALE_GENITALIA_COVERED": 4,
    "FEMALE_BREAST_COVERED": 4,
    "BUTTOCKS_COVERED": 4,
    nsfw_cls.KEY: 2,   # ИИ-классификатор "картинка целиком" (если модель положена рядом с ботом) — с уровня 2
}

# На рисунках (арты, хентай, стикеры) модель по гениталиям и анусу выдаёт заметно меньшую уверенность,
# чем по груди, и они не проходили общий порог. Поэтому порог для каждого класса = общий порог * множитель:
# для явных гениталий он ниже (ловим слабые срабатывания), для «закрытых» зон — выше (иначе попадёт любая одетая девушка).
CLASS_THRESHOLD_FACTOR = {
    "FEMALE_GENITALIA_EXPOSED": 0.6,
    "MALE_GENITALIA_EXPOSED": 0.6,
    "ANUS_EXPOSED": 0.6,
    "FEMALE_BREAST_EXPOSED": 0.85,
    "BUTTOCKS_EXPOSED": 1.0,
    "FEMALE_GENITALIA_COVERED": 1.5,
    "FEMALE_BREAST_COVERED": 1.5,
    "BUTTOCKS_COVERED": 1.5,
}

CLASS_RU = {
    "FEMALE_GENITALIA_EXPOSED": "женские гениталии",
    "MALE_GENITALIA_EXPOSED": "мужские гениталии",
    "ANUS_EXPOSED": "анус",
    "FEMALE_BREAST_EXPOSED": "открытая грудь",
    "BUTTOCKS_EXPOSED": "открытые ягодицы",
    "FEMALE_GENITALIA_COVERED": "бельё/закрытая зона (низ)",
    "FEMALE_BREAST_COVERED": "бельё/закрытая грудь",
    "BUTTOCKS_COVERED": "бельё/закрытые ягодицы",
    nsfw_cls.KEY: "ИИ-классификатор: откровенный контент",
}

# Версия способа проверки. Кэш хранит оценки по файлам; при смене алгоритма (части картинки, новые классы)
# старые записи с другой версией перепроверяются, а не считаются «чистыми» навсегда.
SCAN_VERSION = 3

MAX_SIDE = 640  # большие картинки уменьшаем: модель всё равно работает с 320 px

_detector = None
_load_failed = False


def _build_detector():
    """
    NudeNet по умолчанию создаёт сессию onnxruntime с потоком на КАЖДОЕ видимое ядро и «запасной» памятью.
    На хостинге с квотой 0.25 vCPU контейнер часто видит десятки ядер: бот раздувался за лимит памяти и тратил
    секунды на картинку (потоки мешают друг другу). Собираем детектор сами: один поток, без арены.
    Если внутреннее устройство NudeNet изменится — молча откатываемся на стандартный детектор.
    """
    import nudenet

    try:
        import cv2
        cv2.setNumThreads(1)
    except Exception:
        pass
    try:
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        det = nudenet.NudeDetector.__new__(nudenet.NudeDetector)
        det.onnx_session = ort.InferenceSession(
            os.path.join(os.path.dirname(nudenet.__file__), "320n.onnx"),
            sess_options=opts, providers=["CPUExecutionProvider"])
        det.input_width = det.input_height = 320
        det.input_name = det.onnx_session.get_inputs()[0].name
        return det
    except Exception as e:
        logger.warning("NudeNet: не удалось настроить лёгкий режим (%s) — использую стандартный", e)
        return nudenet.NudeDetector()


def release_memory():
    """Вернуть системе освобождённую память (glibc держит её «про запас»). Вызывается после проверки картинок."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass   # не Linux / нет glibc — не страшно


def _get_detector():
    global _detector, _load_failed
    if _detector is not None:
        return _detector
    if _load_failed:
        return None
    try:
        _detector = _build_detector()
        logger.info("Фильтр порно: модель NudeNet загружена")
    except Exception as e:
        _load_failed = True
        logger.warning("Фильтр порно отключён — не удалось загрузить NudeNet: %s", e)
        return None
    return _detector


def available():
    return _get_detector() is not None


def _to_rgba_array(raw):
    """Байты картинки (jpeg/png/webp...) -> RGBA-массив. Прозрачность кладём на белый фон."""
    import numpy as np
    from PIL import Image

    im = Image.open(io.BytesIO(raw))
    # Для JPEG просим декодер сразу отдать уменьшенную версию (в 2/4/8 раз) — иначе большое
    # фото целиком раскрывается в память (4000x3000 = ~36 МБ только пикселей, а с копиями втрое больше).
    # Для остальных форматов draft() ничего не делает.
    im.draft("RGB", (MAX_SIDE, MAX_SIDE))
    im.load()
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE))
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    return np.array(bg)


def _collect(detections, acc):
    for d in detections:
        cls, score = d["class"], float(d["score"])
        if cls in CLASS_LEVELS and score > acc.get(cls, 0.0):
            acc[cls] = round(score, 3)


# Если уже полный кадр дал уверенное попадание — дальше можно не искать (экономим процессор).
_EARLY_STOP_SCORE = 0.8


def _tiles(arr):
    """4 перекрывающихся куска (по 60% ширины/высоты). Модель сжимает картинку до 320 px, и мелкие
    детали на большой картинке теряются; на куске та же деталь крупнее и находится заметно лучше."""
    import numpy as np

    h, w = arr.shape[:2]
    if min(h, w) < 160:
        return []
    th, tw = int(h * 0.6), int(w * 0.6)
    return [
        np.ascontiguousarray(arr[y:y + th, x:x + tw])
        for x, y in ((0, 0), (w - tw, 0), (0, h - th), (w - tw, h - th))
    ]


def scan_array(det, arr, acc, deep=True):
    """Полный кадр + (если ничего уверенного не нашли) 4 куска. Результат складывает в acc."""
    _collect(det.detect(arr), acc)
    if deep and max(acc.values(), default=0.0) < _EARLY_STOP_SCORE:
        for tile in _tiles(arr):
            _collect(det.detect(tile), acc)


def scan_image_bytes(raw, classify=True):
    """-> {класс: макс. уверенность} по релевантным классам, или None, если фильтр недоступен."""
    det = _get_detector()
    if det is None:
        return None
    acc = {}
    arr = _to_rgba_array(raw)
    scan_array(det, arr, acc)
    # Второй взгляд (классификатор целой картинки) — если NudeNet уже уверен, не тратим время
    if classify and max(acc.values(), default=0.0) < _EARLY_STOP_SCORE:
        p = nsfw_cls.classify(arr)
        if p is not None:
            acc[nsfw_cls.KEY] = p
    del arr
    release_memory()
    return acc


def scan_video_bytes(raw, suffix=".mp4", frames=3):
    """Берёт несколько кадров из видео/гифки (10%, 50%, 90% длины) и объединяет оценки."""
    det = _get_detector()
    if det is None:
        return None

    import cv2

    fd, path = tempfile.mkstemp(suffix=suffix)
    cap = None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 1:
            idxs = sorted({max(0, int(total * p)) for p in (0.1, 0.5, 0.9)})[:frames]
        else:
            idxs = [0]
        acc, got = {}, 0
        for i in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            got += 1
            scan_array(det, cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA), acc, deep=False)
        return acc if got else None
    finally:
        if cap is not None:
            cap.release()
        try:
            os.unlink(path)
        except OSError:
            pass


def class_threshold(cls, threshold):
    return threshold * CLASS_THRESHOLD_FACTOR.get(cls, 1.0)


def verdict(scores, level, threshold, use_classifier=True):
    """
    -> (нарушение: bool, класс-причина, уверенность)
    Нарушение — если какой-то класс разрешённого уровнем набора набрал свой порог
    (общий порог * множитель класса, см. CLASS_THRESHOLD_FACTOR).
    """
    best_cls, best_score = None, 0.0
    for cls, score in (scores or {}).items():
        need = CLASS_LEVELS.get(cls)
        if need is None or need > level:
            continue
        if cls == nsfw_cls.KEY and not use_classifier:
            continue
        if score >= class_threshold(cls, threshold) and score > best_score:
            best_cls, best_score = cls, score
    return best_cls is not None, best_cls, best_score
