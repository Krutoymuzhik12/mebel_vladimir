"""DINOv2 ViT-S/14 → L2-нормированный вектор 384D.

Обкатанный пайплайн из demo_for_mebel / mebel_Iliya:
- resize 336×336 (patch 14)
- опционально rembg → объект на белом фоне
"""

from __future__ import annotations

import numpy as np
import torch
from PIL import Image

from app.vision.config import CUT_BG

_model = None

try:
    from rembg import remove as _rembg_remove
except ImportError:
    _rembg_remove = None

_MEAN = np.array([0.485, 0.456, 0.406], dtype="float32")
_STD = np.array([0.229, 0.224, 0.225], dtype="float32")


def _load_model():
    global _model
    if _model is None:
        _model = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14")
        _model.eval()
    return _model


def cut_background(img: Image.Image) -> Image.Image:
    if _rembg_remove is None:
        return img
    try:
        w, h = img.size
        if w * h < 40_000:
            return img
        rgba = _rembg_remove(img.convert("RGB"))
        white = Image.new("RGB", rgba.size, (255, 255, 255))
        white.paste(rgba, mask=rgba.split()[-1])
        return white
    except Exception:
        return img


def _preprocess(img: Image.Image) -> torch.Tensor:
    img = img.convert("RGB").resize((336, 336), Image.BICUBIC)
    arr = np.asarray(img).astype("float32") / 255.0
    arr = (arr - _MEAN) / _STD
    return torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)


def embed(img: Image.Image, cut_bg: bool = CUT_BG) -> np.ndarray:
    model = _load_model()
    if cut_bg:
        img = cut_background(img)
    with torch.no_grad():
        vec = model(_preprocess(img)).squeeze(0).numpy()
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec
