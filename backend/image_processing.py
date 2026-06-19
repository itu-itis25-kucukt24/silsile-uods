# -*- coding: utf-8 -*-
"""
image_processing.py
====================
Optik formun ham fotoğrafını temizleyip ikili (binary) bir matrise
dönüştüren görüntü işleme hattı (bkz. rapor bölüm 3.3) [16].

Üç sıralı adım uygulanır:
  1) Lüminesans dönüşümü ile gri tonlamaya geçiş (ITU-R BT.601)  [17]
  2) 5x5 Gauss konvolüsyonu ile gürültü temizleme               [16]
  3) Otsu adaptif eşikleme ile ikili (0/255) forma dönüştürme    [6]

Her adım hem OpenCV'nin optimize edilmiş C++ çekirdeğini kullanır hem de
denklemlerin (1), (2), (3) ile bire bir örtüştüğünü garanti eden açık
NumPy doğrulamasına izin verir (test dosyalarında kullanılır).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

import cv2
import numpy as np

# ITU-R BT.601 ağırlıklı dönüşüm katsayıları (Denklem 1).
# OpenCV'nin BGR kanal sıralamasına göre düzenlenmiştir: B, G, R.
_BT601_WEIGHTS_BGR = np.array([0.114, 0.587, 0.299], dtype=np.float64)  # [B, G, R]

# Gauss filtresi çekirdek boyutu (rapor bölüm 3.3.2).
_GAUSS_KERNEL_SIZE = (5, 5)

# Maksimum kabul edilen kenar uzunluğu (px). Çok büyük görüntüler hem
# işlemi yavaşlatır hem de kontur tespitinde gereksiz gürültü üretir.
_MAX_DIMENSION_PX = 2600


class ImageProcessingError(Exception):
    """Görüntü işleme hattındaki herhangi bir adımda oluşan hatalar için."""


@dataclass
class ProcessedImageBundle:
    """Görüntü işleme hattının ürettiği tüm ara ve son ürünleri taşır."""
    original_bgr: np.ndarray
    resized_bgr: np.ndarray
    grayscale: np.ndarray
    blurred: np.ndarray
    binary: np.ndarray
    otsu_threshold_value: float


def load_image(source: Union[str, Path, bytes, np.ndarray]) -> np.ndarray:
    """
    Dosya yolu, bayt dizisi ya da doğrudan bir NumPy dizisinden BGR
    formatında bir görüntü yükler. Bozuk/okunamayan dosyalarda anlaşılır
    bir hata fırlatır.
    """
    if isinstance(source, np.ndarray):
        image = source
    elif isinstance(source, bytes):
        arr = np.frombuffer(source, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    else:
        path = Path(source)
        if not path.exists():
            raise ImageProcessingError(f"Görüntü dosyası bulunamadı: {path}")
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)

    if image is None or image.size == 0:
        raise ImageProcessingError(
            "Görüntü okunamadı veya bozuk. Desteklenen formatlar: "
            "JPEG, PNG, BMP, WEBP."
        )
    if image.ndim != 3 or image.shape[2] != 3:
        raise ImageProcessingError(
            f"Beklenmeyen görüntü boyutu: {image.shape}. 3 kanallı (BGR) "
            "renkli görüntü bekleniyor."
        )
    return image


def resize_if_needed(image_bgr: np.ndarray, max_dim: int = _MAX_DIMENSION_PX) -> np.ndarray:
    """
    Görüntünün en uzun kenarı max_dim'i aşıyorsa, en-boy oranını koruyarak
    küçültür. Çok düşük çözünürlüklü görüntülerde büyütme YAPILMAZ; bu,
    yapay olarak üretilmiş piksel bilgisinin OMR kararlarını
    yanıltmasını önler.
    """
    h, w = image_bgr.shape[:2]
    longest_edge = max(h, w)
    if longest_edge <= max_dim:
        return image_bgr
    scale = max_dim / float(longest_edge)
    new_size = (int(round(w * scale)), int(round(h * scale)))
    return cv2.resize(image_bgr, new_size, interpolation=cv2.INTER_AREA)


def to_grayscale_bt601(image_bgr: np.ndarray) -> np.ndarray:
    """
    Adım 1 — Renk Bilgisini Atma (Denklem 1):
        Y = 0.299*R + 0.587*G + 0.114*B

    OpenCV görüntüleri BGR kanal sırasıyla tuttuğundan, ağırlıklar B, G, R
    sırasına göre uygulanır. Sonuç tek kanallı, 8 bit (0-255) bir
    görüntüdür. Hesaplama float64 hassasiyetinde yapılıp en sonda
    yuvarlanarak kümülatif yuvarlama hatası en aza indirilir.
    """
    if image_bgr.dtype != np.uint8:
        raise ImageProcessingError("Girdi görüntüsü 8 bit (uint8) olmalıdır.")

    img_f64 = image_bgr.astype(np.float64)
    # img_f64 şekli (H, W, 3) -> ağırlıklarla nokta çarpımı (H, W)
    gray_f64 = img_f64 @ _BT601_WEIGHTS_BGR
    gray_u8 = np.clip(np.round(gray_f64), 0, 255).astype(np.uint8)
    return gray_u8


def apply_gaussian_blur(gray: np.ndarray, kernel_size: Tuple[int, int] = _GAUSS_KERNEL_SIZE) -> np.ndarray:
    """
    Adım 2 — Gürültü Temizleme (Denklem 2):
        G(x,y) = (1 / 2*pi*sigma^2) * exp(-(x^2+y^2) / 2*sigma^2)

    sigmaX=0 verildiğinde OpenCV, sigma değerini çekirdek boyutundan
    standart formülle (0.3*((ksize-1)*0.5 - 1) + 0.8) otomatik hesaplar;
    bu, 5x5 çekirdek için literatürdeki tipik Gauss yumuşatmasına denktir.
    """
    if kernel_size[0] % 2 == 0 or kernel_size[1] % 2 == 0:
        raise ImageProcessingError("Gauss çekirdek boyutu tek sayı olmalıdır.")
    return cv2.GaussianBlur(gray, kernel_size, sigmaX=0)


def apply_otsu_threshold(blurred_gray: np.ndarray) -> Tuple[np.ndarray, float]:
    """
    Adım 3 — İkili Forma Dönüştürme (Denklem 3, Otsu 1979) [6]:
        t* = argmax_t [ w_bg(t) * w_fg(t) * (mu_bg(t) - mu_fg(t))^2 ]

    cv2.threshold, THRESH_OTSU bayrağıyla çağrıldığında bu optimum eşiği
    histogramdan otomatik hesaplar. THRESH_BINARY ile: piksel > eşik ise
    255 (beyaz/boş alan), aksi hâlde 0 (siyah/işaretli alan) olur. Bu
    kural, kâğıt (açık renk) arka plan ve mürekkep (koyu renk) işaret
    varsayımıyla tutarlıdır.

    Dönüş: (ikili_görüntü, hesaplanan_otsu_esik_degeri)
    """
    otsu_value, binary = cv2.threshold(
        blurred_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    return binary, float(otsu_value)


def process_image(source: Union[str, Path, bytes, np.ndarray]) -> ProcessedImageBundle:
    """
    Tüm görüntü işleme hattını (yükleme -> boyutlandırma -> gri tonlama ->
    Gauss -> Otsu) sırasıyla uygular ve tüm ara ürünleri içeren bir
    ProcessedImageBundle döndürür (bkz. Şekil 3).
    """
    original = load_image(source)
    resized = resize_if_needed(original)
    gray = to_grayscale_bt601(resized)
    blurred = apply_gaussian_blur(gray)
    binary, otsu_val = apply_otsu_threshold(blurred)

    return ProcessedImageBundle(
        original_bgr=original,
        resized_bgr=resized,
        grayscale=gray,
        blurred=blurred,
        binary=binary,
        otsu_threshold_value=otsu_val,
    )


def save_binary_image(binary: np.ndarray, output_path: Union[str, Path]) -> Path:
    """İkili görüntüyü PNG olarak (kayıpsız) diske yazar ve yolu döndürür."""
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    success = cv2.imwrite(str(out_path), binary)
    if not success:
        raise ImageProcessingError(f"Görüntü kaydedilemedi: {out_path}")
    return out_path


def black_pixel_ratio(binary_roi: np.ndarray) -> float:
    """
    Verilen ikili görüntü bölgesindeki (ROI) siyah (0 değerli, yani
    işaretli/mürekkepli) piksellerin oranını döndürür. OMR doluluk oranı
    hesaplamasının (Denklem 6) temel yapı taşıdır.
    """
    if binary_roi.size == 0:
        return 0.0
    black_count = int(np.count_nonzero(binary_roi == 0))
    return black_count / float(binary_roi.size)
