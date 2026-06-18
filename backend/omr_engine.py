# -*- coding: utf-8 -*-
"""
omr_engine.py
=============
Optik İşaret Tanıma (OMR) motoru — rapor bölüm 3.4'teki matematiksel
modeli uygular:

  Adım 4 — Kontur Tespiti ve Referans Noktaları (Denklem 4)         [18]
  Adım 5 — Izgara Hücresi Eşlemesi ve Cevap Kararı (Denklem 5,6,7)  [11]

Akış:
  1) İkili görüntüde 4 köşe referans karesi bulunur (find_reference_markers).
  2) Bu 4 nokta kullanılarak perspektif düzeltmesi yapılır (warp_to_canonical);
     sonuç, sabit boyutlu "kanonik" bir ikili görüntüdür.
  3) Kanonik görüntü üzerinde, config/omr_template.json içinde tanımlı
     ızgara parametreleriyle (x0, y0, Δx, Δy) her hücrenin doluluk oranı
     hesaplanır ve karar eşiği θd ile karşılaştırılır.

Izgara parametrelerinin dışsallaştırılması (JSON yapılandırma dosyası),
raporun 3.9.2 bölümünde belirtilen "farklı form formatlarına adaptasyon"
gereksinimini karşılar: soru sayısı, şık düzeni ya da form boyutu
değiştiğinde yazılımda tek satır kod değişikliği gerekmez.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np

from image_processing import black_pixel_ratio

# Boş bırakılmış alan için kullanılan özel durum kodu.
EMPTY_MARK = "BOS"
# Birden fazla seçenek işaretlenmiş alan için özel durum kodu.
INVALID_MARK = "GECERSIZ"


class OMRError(Exception):
    """OMR işleme hattındaki hatalar için temel istisna sınıfı."""


class ReferenceMarkerNotFoundError(OMRError):
    """4 köşe referans işaretçisinden biri veya birden fazlası bulunamadığında."""


# ============================================================================
# 1) YAPILANDIRMA YÜKLEME
# ============================================================================

@dataclass
class GridConfig:
    origin_x: int
    origin_y: int
    cell_width: int
    cell_height: int
    canvas_width: int
    canvas_height: int

    def cell_rect(self, row: int, col: int) -> Tuple[int, int, int, int]:
        """
        Denklem (5): x_{r,c} = x0 + c*Δx ,  y_{r,c} = y0 + r*Δy
        Hücrenin (x1, y1, x2, y2) piksel dikdörtgenini döndürür.
        """
        x1 = self.origin_x + col * self.cell_width
        y1 = self.origin_y + row * self.cell_height
        x2 = x1 + self.cell_width
        y2 = y1 + self.cell_height
        return x1, y1, x2, y2


@dataclass
class FieldConfig:
    name: str
    options: List[str]
    position_count: int
    layout: str  # "rows_are_positions" | "columns_are_positions"
    start_row: int
    start_col: int
    # None  -> as_string üretilmez (alan doğası gereği listedir, örn. cevaplar)
    # ""    -> seçenekler ARASINA hiçbir ayraç koymadan birleştirilir (örn. TC: "12345678901")
    # "X"   -> seçenekler "X" ayracıyla birleştirilir
    output_join: Optional[str] = None
    # True ise bu alanın ızgara bölgesi, redact_personal_fields() tarafından
    # IPFS'e/diske kalıcı olarak yazılmadan ÖNCE beyazla doldurulup gizlenir
    # (TC kimlik ve isim baloncukları, görsel olarak da kişisel veri
    # niteliği taşıdığından — bkz. omr_engine.redact_personal_fields).
    contains_personal_data: bool = False

    def __post_init__(self):
        if self.layout not in ("rows_are_positions", "columns_are_positions"):
            raise OMRError(
                f"Geçersiz layout değeri: {self.layout!r}. "
                "'rows_are_positions' veya 'columns_are_positions' olmalıdır."
            )
        if not self.options:
            raise OMRError(f"'{self.name}' alanı için seçenek listesi boş olamaz.")
        if self.position_count <= 0:
            raise OMRError(f"'{self.name}' alanı için position_count pozitif olmalıdır.")


@dataclass
class ReferenceMarkerConfig:
    threshold_fill_ratio: float = 0.55
    min_area_ratio: float = 0.0003
    max_area_ratio: float = 0.02
    aspect_ratio_tolerance: float = 0.35
    corner_search_ratio: float = 0.28  # her köşe için aranacak bölge oranı
    marker_size_px: int = 70   # kanonik uzayda işaretçi kare kenar uzunluğu
    margin_px: int = 45        # kanonik uzayda işaretçinin sayfa kenarına olan boşluğu

    def anchor_points_ordered(self, canvas_width: int, canvas_height: int) -> np.ndarray:
        """
        Referans işaretçilerinin KANONİK uzaydaki beklenen merkez
        koordinatlarını [sol-üst, sağ-üst, sağ-alt, sol-alt] sırasıyla
        döndürür. Bu, warp_to_canonical() için hedef (dst) noktalarıdır
        ve generate_test_form.py ile AYNI kaynaktan (bu fonksiyondan)
        türetildiğinden, üretim ve çözümleme arasında koordinat tutarsızlığı
        oluşmaz.
        """
        half = self.marker_size_px / 2.0
        m = float(self.margin_px)
        tl = (m + half, m + half)
        tr = (canvas_width - m - half, m + half)
        br = (canvas_width - m - half, canvas_height - m - half)
        bl = (m + half, canvas_height - m - half)
        return np.array([tl, tr, br, bl], dtype=np.float32)


@dataclass
class OMRTemplate:
    version: str
    canvas_width: int
    canvas_height: int
    grid: GridConfig
    fields: List[FieldConfig]
    reference_markers: ReferenceMarkerConfig
    decision_threshold: float

    def get_field(self, name: str) -> FieldConfig:
        for f in self.fields:
            if f.name == name:
                return f
        raise OMRError(f"Yapılandırmada '{name}' adlı alan tanımlı değil.")


def load_omr_template(path: Union[str, Path]) -> OMRTemplate:
    """config/omr_template.json dosyasını okuyup tip güvenli bir OMRTemplate nesnesine dönüştürür."""
    path = Path(path)
    if not path.exists():
        raise OMRError(f"OMR şablon dosyası bulunamadı: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    canvas = raw["canvas"]
    grid_raw = raw["grid"]
    grid = GridConfig(
        origin_x=grid_raw["origin_x"],
        origin_y=grid_raw["origin_y"],
        cell_width=grid_raw["cell_width"],
        cell_height=grid_raw["cell_height"],
        canvas_width=canvas["width"],
        canvas_height=canvas["height"],
    )

    ref_raw = raw.get("reference_markers", {})
    ref_cfg = ReferenceMarkerConfig(
        threshold_fill_ratio=ref_raw.get("threshold_fill_ratio", 0.55),
        min_area_ratio=ref_raw.get("min_area_ratio", 0.0003),
        max_area_ratio=ref_raw.get("max_area_ratio", 0.02),
        aspect_ratio_tolerance=ref_raw.get("aspect_ratio_tolerance", 0.35),
        corner_search_ratio=ref_raw.get("corner_search_ratio", 0.28),
        marker_size_px=ref_raw.get("marker_size_px", 70),
        margin_px=ref_raw.get("margin_px", 45),
    )

    fields = []
    for f in raw["fields"]:
        fields.append(
            FieldConfig(
                name=f["name"],
                options=f["options"],
                position_count=f["position_count"],
                layout=f["layout"],
                start_row=f["start_row"],
                start_col=f["start_col"],
                output_join=f.get("output_join", None),
                contains_personal_data=f.get("contains_personal_data", False),
            )
        )

    return OMRTemplate(
        version=raw.get("version", "1.0"),
        canvas_width=canvas["width"],
        canvas_height=canvas["height"],
        grid=grid,
        fields=fields,
        reference_markers=ref_cfg,
        decision_threshold=raw.get("decision_threshold", 0.45),
    )


# ============================================================================
# 2) REFERANS NOKTASI TESPİTİ VE PERSPEKTİF DÜZELTME (Denklem 4, Adım 4)
# ============================================================================

@dataclass
class DetectedMarker:
    center: Tuple[float, float]
    contour: np.ndarray
    score: float  # Doluluk skoru (Denklem 4)


def _order_points_clockwise(pts: np.ndarray) -> np.ndarray:
    """
    4 noktayı [sol-üst, sağ-üst, sağ-alt, sol-alt] sırasına göre düzenler.
    Standart belge-tarayıcı sıralama hilesi: toplam (x+y) en küçük olan
    sol-üst, en büyük olan sağ-alt; fark (x-y) en küçük olan sol-alt,
    en büyük olan sağ-üsttür.
    """
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    diff = pts[:, 0] - pts[:, 1]
    rect[0] = pts[np.argmin(s)]        # sol-üst (en küçük x+y)
    rect[2] = pts[np.argmax(s)]        # sağ-alt (en büyük x+y)
    rect[1] = pts[np.argmax(diff)]     # sağ-üst (en büyük x-y)
    rect[3] = pts[np.argmin(diff)]     # sol-alt (en küçük x-y)
    return rect


def find_reference_markers(
    binary_image: np.ndarray,
    ref_cfg: ReferenceMarkerConfig,
) -> np.ndarray:
    """
    Optik formun 4 köşesindeki dolgulu kare referans işaretçilerini tespit
    eder (Denklem 4):

        İşaretçi Skoru = Dolu Alan / Sınırlayıcı Kutu Alanı >= θref

    Yöntem:
      1) İkili görüntü tersine çevrilir (siyah kareler -> beyaz bloblar)
         böylece cv2.findContours bu blobları dış kontur olarak yakalar [18].
      2) Alan, en-boy oranı ve doluluk skoru filtreleriyle aday işaretçiler
         elenir.
      3) Görüntü 4 çeyreğe (sol-üst, sağ-üst, sol-alt, sağ-alt) bölünür ve
         her çeyrekte köşeye en yakın aday, o köşenin işaretçisi seçilir.

    Dönüş: _order_points_clockwise ile sıralanmış 4x2 NumPy dizisi
    (sol-üst, sağ-üst, sağ-alt, sol-alt).
    """
    h, w = binary_image.shape[:2]
    total_area = float(h * w)

    inverted = cv2.bitwise_not(binary_image)
    contours, _ = cv2.findContours(inverted, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: List[DetectedMarker] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area <= 0:
            continue
        area_ratio = area / total_area
        if area_ratio < ref_cfg.min_area_ratio or area_ratio > ref_cfg.max_area_ratio:
            continue

        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw == 0 or bh == 0:
            continue
        aspect_ratio = bw / float(bh)
        if abs(aspect_ratio - 1.0) > ref_cfg.aspect_ratio_tolerance:
            continue

        bbox_area = float(bw * bh)
        fill_score = area / bbox_area  # Denklem 4: Dolu Alan / Sınırlayıcı Kutu Alanı
        if fill_score < ref_cfg.threshold_fill_ratio:
            continue

        cx, cy = x + bw / 2.0, y + bh / 2.0
        candidates.append(DetectedMarker(center=(cx, cy), contour=cnt, score=fill_score))

    if len(candidates) < 4:
        raise ReferenceMarkerNotFoundError(
            f"4 köşe referans işaretçisinin tamamı bulunamadı "
            f"(bulunan aday sayısı: {len(candidates)}). Lütfen formun tüm "
            f"köşelerinin görüntü içinde net biçimde göründüğünden emin olun."
        )

    search_w = w * ref_cfg.corner_search_ratio
    search_h = h * ref_cfg.corner_search_ratio
    corner_targets = {
        "top_left": (0.0, 0.0),
        "top_right": (float(w), 0.0),
        "bottom_left": (0.0, float(h)),
        "bottom_right": (float(w), float(h)),
    }

    selected_points: List[Tuple[float, float]] = []
    for corner_name, (tx, ty) in corner_targets.items():
        best: Optional[DetectedMarker] = None
        best_dist = float("inf")
        for cand in candidates:
            cx, cy = cand.center
            # Adayın, ilgili köşenin arama bölgesi içinde olup olmadığını kontrol et.
            in_region = (
                abs(cx - tx) <= search_w + (w * 0.5) and  # gevşek bölge sınırı
                abs(cy - ty) <= search_h + (h * 0.5)
            )
            dist = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
            # Önce gerçek köşeye en yakın bölgede olanlar, sonra en kısa mesafe.
            quadrant_ok = (
                (cx < w / 2 if tx == 0.0 else cx >= w / 2) and
                (cy < h / 2 if ty == 0.0 else cy >= h / 2)
            )
            if quadrant_ok and dist < best_dist:
                best_dist = dist
                best = cand
        if best is None:
            raise ReferenceMarkerNotFoundError(
                f"'{corner_name}' köşesinde referans işaretçisi bulunamadı."
            )
        selected_points.append(best.center)

    pts = np.array(selected_points, dtype=np.float32)
    return _order_points_clockwise(pts)


def warp_to_canonical(
    grayscale_blurred: np.ndarray,
    ordered_corners: np.ndarray,
    dst_anchor_points: np.ndarray,
    canvas_width: int,
    canvas_height: int,
) -> np.ndarray:
    """
    Tespit edilen 4 referans işaretçi MERKEZİNİ (ordered_corners), bu
    işaretçilerin kanonik uzaydaki BEKLENEN merkez konumlarına
    (dst_anchor_points, bkz. ReferenceMarkerConfig.anchor_points_ordered)
    eşleyen bir perspektif düzeltmesi (homografi) uygular.

    ÖNEMLİ TASARIM NOTU: Hedef noktalar olarak doğrudan kanvasın dış
    köşeleri ((0,0), (W,0), ...) KULLANILMAZ; çünkü gerçek bir basılı
    formda işaretçiler her zaman sayfa kenarından bir miktar (margin_px)
    içeride bulunur. Tespit edilen işaretçi merkezlerini doğrudan kanvas
    köşelerine eşlemek, ızgaranın tüm formun üzerinde sistematik biçimde
    kaymasına yol açar. Bu fonksiyon bunun yerine işaretçilerin
    ReferenceMarkerConfig'te tanımlı GERÇEK beklenen konumlarını hedef
    alır; böylece üretim (generate_test_form.py) ve çözümleme (bu modül)
    her zaman aynı koordinat sistemini paylaşır.

    Düzeltme, gri tonlamalı (henüz eşiklenmemiş) görüntü üzerinde yapılır;
    böylece warp sonrası Otsu eşiklemesi kanonik görüntüde YENİDEN
    uygulanarak en temiz sonucu üretir.
    """
    transform_matrix = cv2.getPerspectiveTransform(ordered_corners, dst_anchor_points)
    warped = cv2.warpPerspective(
        grayscale_blurred, transform_matrix, (canvas_width, canvas_height),
        borderMode=cv2.BORDER_REPLICATE,
    )
    return warped


def rebinarize_canonical(warped_gray: np.ndarray) -> np.ndarray:
    """Perspektif düzeltmesi yapılmış gri görüntüye Otsu eşiklemesini yeniden uygular."""
    _, binary = cv2.threshold(warped_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


# ============================================================================
# 3) IZGARA HÜCRESİ EŞLEMESİ VE CEVAP KARARI (Denklem 5, 6, 7 — Adım 5)
# ============================================================================

@dataclass
class FieldDecodeResult:
    name: str
    raw_values: List[Optional[str]]   # Her pozisyon için seçilen seçenek / BOS / GECERSIZ
    fill_ratios: List[List[float]]    # Hata ayıklama / denetim için: [pozisyon][seçenek] doluluk oranları
    as_string: str                    # output_join ile birleştirilmiş hâli


def _position_to_row_col(field: FieldConfig, position_idx: int, option_idx: int) -> Tuple[int, int]:
    """Bir (pozisyon, seçenek) çiftini global (row, col) ızgara koordinatına çevirir."""
    if field.layout == "rows_are_positions":
        row = field.start_row + position_idx
        col = field.start_col + option_idx
    else:  # columns_are_positions
        row = field.start_row + option_idx
        col = field.start_col + position_idx
    return row, col


def decode_field(
    binary_canonical: np.ndarray,
    grid: GridConfig,
    field: FieldConfig,
    decision_threshold: float,
) -> FieldDecodeResult:
    """
    Denklem (6): rho_{r,c} = (Hücredeki siyah piksel sayısı) / (Hücre piksel sayısı)
    Denklem (7): c_hat_r = argmax_c rho_{r,c} ;  geçerli ise rho_{r, c_hat_r} >= theta_d

    Her pozisyon (soru / hane) için tüm seçeneklerin doluluk oranı
    hesaplanır. theta_d eşiğini aşan tam olarak bir seçenek varsa o
    seçenek kabul edilir; hiçbiri aşmıyorsa BOS, birden fazlası aşıyorsa
    GECERSIZ olarak işaretlenir.
    """
    raw_values: List[Optional[str]] = []
    all_ratios: List[List[float]] = []

    for pos in range(field.position_count):
        ratios: List[float] = []
        for opt_idx in range(len(field.options)):
            row, col = _position_to_row_col(field, pos, opt_idx)
            x1, y1, x2, y2 = grid.cell_rect(row, col)
            x1c, y1c = max(0, x1), max(0, y1)
            x2c = min(binary_canonical.shape[1], x2)
            y2c = min(binary_canonical.shape[0], y2)
            if x2c <= x1c or y2c <= y1c:
                ratios.append(0.0)
                continue
            roi = binary_canonical[y1c:y2c, x1c:x2c]
            ratios.append(black_pixel_ratio(roi))

        all_ratios.append(ratios)

        above = [i for i, r in enumerate(ratios) if r >= decision_threshold]
        if len(above) == 0:
            raw_values.append(EMPTY_MARK)
        elif len(above) == 1:
            raw_values.append(field.options[above[0]])
        else:
            raw_values.append(INVALID_MARK)

    if field.output_join is not None:
        joined = field.output_join.join(
            v if v not in (EMPTY_MARK, INVALID_MARK) else "?" for v in raw_values
        )
    else:
        joined = ""

    return FieldDecodeResult(
        name=field.name,
        raw_values=raw_values,
        fill_ratios=all_ratios,
        as_string=joined,
    )


@dataclass
class OMRResult:
    tc_kimlik_raw: List[Optional[str]]
    tc_kimlik_str: str
    isim_raw: List[Optional[str]]
    isim_str: str
    cevaplar_raw: List[Optional[str]]
    debug_field_results: Dict[str, FieldDecodeResult]
    ordered_corners: np.ndarray
    canonical_binary: np.ndarray


def process_form(
    grayscale_blurred: np.ndarray,
    template: OMRTemplate,
) -> OMRResult:
    """
    Tüm OMR hattını çalıştırır: referans tespiti -> perspektif düzeltme ->
    yeniden eşikleme -> alan bazlı (TC, isim, cevaplar) karar üretimi.

    `grayscale_blurred`, image_processing.process_image() çıktısındaki
    ProcessedImageBundle.blurred alanı olmalıdır (henüz eşiklenmemiş, ama
    gürültüsü azaltılmış gri görüntü). Bu fonksiyon Otsu eşiklemesini
    HAM görüntü üzerinde bir kez, kanonik (perspektif düzeltilmiş) görüntü
    üzerinde ise tekrar uygular; çünkü referans işaretçi tespiti orijinal
    açıda yapılmalı, hücre okuma ise düzeltilmiş açıda yapılmalıdır.
    """
    # Referans işaretçilerini, ham açıdaki ikili görüntüden bulmak için
    # önce orijinal açıda bir ön-eşikleme yapılır.
    _, pre_binary = cv2.threshold(
        grayscale_blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    ordered_corners = find_reference_markers(pre_binary, template.reference_markers)

    dst_anchor_points = template.reference_markers.anchor_points_ordered(
        template.canvas_width, template.canvas_height
    )
    warped_gray = warp_to_canonical(
        grayscale_blurred,
        ordered_corners,
        dst_anchor_points,
        template.canvas_width,
        template.canvas_height,
    )
    canonical_binary = rebinarize_canonical(warped_gray)

    field_results: Dict[str, FieldDecodeResult] = {}
    for field in template.fields:
        field_results[field.name] = decode_field(
            canonical_binary, template.grid, field, template.decision_threshold
        )

    tc_field = field_results.get("tc_kimlik")
    isim_field = field_results.get("isim")
    cevap_field = field_results.get("cevaplar")

    tc_raw = tc_field.raw_values if tc_field else []
    isim_raw = isim_field.raw_values if isim_field else []
    cevap_raw = cevap_field.raw_values if cevap_field else []

    tc_str = "".join(v if v not in (EMPTY_MARK, INVALID_MARK) else "?" for v in tc_raw)
    isim_str = "".join(
        (v if v != " " else " ") if v not in (EMPTY_MARK, INVALID_MARK) else "?"
        for v in isim_raw
    ).strip()

    return OMRResult(
        tc_kimlik_raw=tc_raw,
        tc_kimlik_str=tc_str,
        isim_raw=isim_raw,
        isim_str=isim_str,
        cevaplar_raw=cevap_raw,
        debug_field_results=field_results,
        ordered_corners=ordered_corners,
        canonical_binary=canonical_binary,
    )


def _field_row_col_bounds(field: FieldConfig) -> Tuple[int, int, int, int]:
    """Bir alanın kapladığı TÜM (satır, sütun) ızgara aralığını döndürür
    (row_min, row_max, col_min, col_max) — _position_to_row_col'un tersi
    yönde, tek tek değil toplu sınır hesaplaması."""
    if field.layout == "rows_are_positions":
        row_min, row_max = field.start_row, field.start_row + field.position_count - 1
        col_min, col_max = field.start_col, field.start_col + len(field.options) - 1
    else:  # columns_are_positions
        row_min, row_max = field.start_row, field.start_row + len(field.options) - 1
        col_min, col_max = field.start_col, field.start_col + field.position_count - 1
    return row_min, row_max, col_min, col_max


def redact_personal_fields(
    canonical_binary: np.ndarray, template: OMRTemplate, margin_px: int = 4
) -> np.ndarray:
    """
    Hakem değerlendirmesine yanıt: kişisel veri içeren alanların (TC kimlik,
    isim) ızgara bölgesini, kalıcı/herkese açık bir yere (IPFS, disk) yazılıp
    yüklenmeden ÖNCE beyazla doldurarak GÖRSEL olarak da kaldırır.

    Önemli ayrım: bu fonksiyon process_form()'un DÖNDÜRDÜĞÜ canonical_binary
    üzerinde DEĞİL, onun bir KOPYASI üzerinde çalışır — OMR alan okuma
    (decode_field) işlemi bu fonksiyon çağrılmadan ÖNCE, orijinal
    (sansürsüz) görüntü üzerinde zaten tamamlanmış olmalıdır. Yalnızca
    KALICI OLARAK SAKLANACAK/YÜKLENECEK kopya sansürlenir; TC/isim okuma
    doğruluğu bundan etkilenmez.

    Sansürlenen bölge, hücre kenar çizgilerinin de tam örtülmesi için
    `margin_px` kadar pay ile genişletilir.
    """
    redacted = canonical_binary.copy()
    h, w = redacted.shape[:2]
    for field in template.fields:
        if not field.contains_personal_data:
            continue
        row_min, row_max, col_min, col_max = _field_row_col_bounds(field)
        x1, y1, _, _ = template.grid.cell_rect(row_min, col_min)
        _, _, x2, y2 = template.grid.cell_rect(row_max, col_max)
        x1 = max(0, x1 - margin_px)
        y1 = max(0, y1 - margin_px)
        x2 = min(w, x2 + margin_px)
        y2 = min(h, y2 + margin_px)
        redacted[y1:y2, x1:x2] = 255
    return redacted

