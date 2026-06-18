# -*- coding: utf-8 -*-
"""
scripts/generate_test_form.py
==============================
UODS'nin görüntü işleme ve OMR motorunu gerçek bir optik form taraması
olmadan uçtan uca test edebilmek için sentetik (yapay) optik form
görüntüleri üretir.

Üretilen görüntü şunları içerir:
  - 4 köşede dolgulu kare referans işaretçileri (find_reference_markers
    tarafından tespit edilmesi beklenir)
  - config/omr_template.json içinde tanımlı TC kimlik, isim ve cevap
    ızgaralarına karşılık gelen baloncuklar (bazıları "işaretli" / siyah)
  - İsteğe bağlı olarak gerçekçi bir fotoğraf çekimini simüle eden
    perspektif bozulma, hafif döndürme ve gauss gürültüsü

Bu script CLI olarak da çalıştırılabilir:
    python scripts/generate_test_form.py --out ornek_form.jpg --warp
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omr_engine import OMRTemplate, FieldConfig, load_omr_template  # noqa: E402


def _draw_reference_markers(canvas: np.ndarray, template: OMRTemplate) -> None:
    """
    4 köşeye dolgulu siyah kare referans işaretçileri çizer.

    Kare merkezleri, omr_engine.ReferenceMarkerConfig.anchor_points_ordered()
    fonksiyonundan alınır — bu, decode tarafının da aynı fonksiyonla hedef
    (dst) noktalarını hesapladığı TEK kaynaktır. Böylece üretici (bu script)
    ve çözümleyici (omr_engine.process_form) arasında koordinat tutarsızlığı
    yapısal olarak imkânsız hâle gelir.
    """
    ref_cfg = template.reference_markers
    s = ref_cfg.marker_size_px
    half = s / 2.0
    anchors = ref_cfg.anchor_points_ordered(template.canvas_width, template.canvas_height)
    for (cx, cy) in anchors:
        x1, y1 = int(round(cx - half)), int(round(cy - half))
        x2, y2 = int(round(cx + half)), int(round(cy + half))
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color=0, thickness=-1)


def _draw_bubble(canvas: np.ndarray, rect: Tuple[int, int, int, int], filled: bool) -> None:
    """Bir ızgara hücresinin merkezine baloncuk (daire) çizer."""
    x1, y1, x2, y2 = rect
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    radius = int(min(x2 - x1, y2 - y1) * 0.38)
    if filled:
        cv2.circle(canvas, (cx, cy), radius, color=0, thickness=-1)
    else:
        cv2.circle(canvas, (cx, cy), radius, color=120, thickness=1)


def build_canonical_form(
    template: OMRTemplate,
    tc_kimlik: str,
    isim: str,
    cevaplar: List[str],
) -> np.ndarray:
    """
    Şablonda tanımlı boyut ve ızgarayla, hiçbir perspektif/gürültü
    bozulması içermeyen, "ideal" hizalanmış bir form görüntüsü üretir.
    Bu görüntü grid decode mantığını doğrulamak için kullanılır.

    tc_kimlik : 11 karakterli rakam dizesi (örn. "10000000146")
    isim      : Şablondaki "isim" alanının options listesindeki
                karakterlerden oluşan bir dize (örn. "AHMET YILMAZ").
                Desteklenmeyen bir karakter varsa o pozisyon boş bırakılır.
    cevaplar  : Her soru için bir şık harfi listesi (örn. ["A","B",...]).
                None ya da boş string verilirse o soru boş bırakılır.
    """
    canvas = np.full((template.canvas_height, template.canvas_width), 255, dtype=np.uint8)
    _draw_reference_markers(canvas, template)

    def draw_field(field: FieldConfig, position_values: List[Optional[str]]) -> None:
        for pos_idx in range(field.position_count):
            value = position_values[pos_idx] if pos_idx < len(position_values) else None
            for opt_idx, opt_label in enumerate(field.options):
                if field.layout == "rows_are_positions":
                    row = field.start_row + pos_idx
                    col = field.start_col + opt_idx
                else:
                    row = field.start_row + opt_idx
                    col = field.start_col + pos_idx
                rect = template.grid.cell_rect(row, col)
                is_filled = (value is not None and value == opt_label)
                _draw_bubble(canvas, rect, filled=is_filled)

    tc_field = template.get_field("tc_kimlik")
    tc_positions = list(tc_kimlik[: tc_field.position_count])
    draw_field(tc_field, tc_positions)

    isim_field = template.get_field("isim")
    isim_positions = list(isim[: isim_field.position_count])
    draw_field(isim_field, isim_positions)

    cevap_field = template.get_field("cevaplar")
    draw_field(cevap_field, cevaplar[: cevap_field.position_count])

    return canvas


def to_bgr(gray_canvas: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(gray_canvas, cv2.COLOR_GRAY2BGR)


def simulate_camera_photo(
    canonical_bgr: np.ndarray,
    pad_ratio: float = 0.18,
    max_perspective_shift_ratio: float = 0.04,
    rotation_degrees: float = 1.5,
    gaussian_noise_sigma: float = 4.0,
    rng_seed: Optional[int] = 42,
) -> np.ndarray:
    """
    İdeal hizalanmış kanonik formu, telefon kamerasıyla hafif açılı ve
    biraz gürültülü çekilmiş bir fotoğrafa benzetmek için dönüştürür:
      1) Etrafına beyaz "masa" boşluğu eklenir (pad)
      2) Rastgele küçük bir perspektif kayma uygulanır
      3) Hafif döndürme uygulanır
      4) Gauss gürültüsü eklenir

    Bu, find_reference_markers + warp_to_canonical hattının gerçek dünya
    koşullarına karşı dayanıklılığını test etmek için kullanılır.
    """
    rng = np.random.default_rng(rng_seed)
    h, w = canonical_bgr.shape[:2]
    pad_x, pad_y = int(w * pad_ratio), int(h * pad_ratio)

    padded = cv2.copyMakeBorder(
        canonical_bgr, pad_y, pad_y, pad_x, pad_x,
        borderType=cv2.BORDER_CONSTANT, value=(235, 235, 235),
    )
    ph, pw = padded.shape[:2]

    src_pts = np.array(
        [[0, 0], [pw - 1, 0], [pw - 1, ph - 1], [0, ph - 1]], dtype=np.float32
    )
    max_shift = max_perspective_shift_ratio * min(pw, ph)
    jitter = rng.uniform(-max_shift, max_shift, size=(4, 2)).astype(np.float32)
    dst_pts = src_pts + jitter

    transform = cv2.getPerspectiveTransform(src_pts, dst_pts)
    warped = cv2.warpPerspective(
        padded, transform, (pw, ph), borderMode=cv2.BORDER_CONSTANT, borderValue=(235, 235, 235)
    )

    center = (pw // 2, ph // 2)
    rot_matrix = cv2.getRotationMatrix2D(center, rotation_degrees, 1.0)
    rotated = cv2.warpAffine(
        warped, rot_matrix, (pw, ph), borderMode=cv2.BORDER_CONSTANT, borderValue=(235, 235, 235)
    )

    noise = rng.normal(0, gaussian_noise_sigma, rotated.shape).astype(np.float32)
    noisy = np.clip(rotated.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    return noisy


def generate_sample(
    template_path: Path,
    tc_kimlik: str = "10000000146",
    isim: str = "AHMET YILMAZ",
    answer_pattern: Optional[List[str]] = None,
    warp: bool = True,
) -> Tuple[np.ndarray, Dict]:
    """Test amaçlı bir örnek form fotoğrafı ve "doğru cevap" sözlüğünü üretir."""
    template = load_omr_template(template_path)
    cevap_field = template.get_field("cevaplar")

    if answer_pattern is None:
        opts = cevap_field.options
        answer_pattern = [opts[i % len(opts)] for i in range(cevap_field.position_count)]
        # Birkaç soruyu kasıtlı olarak boş bırak (gerçekçilik için).
        answer_pattern[3] = None
        answer_pattern[11] = None

    canonical_gray = build_canonical_form(template, tc_kimlik, isim, answer_pattern)
    canonical_bgr = to_bgr(canonical_gray)

    if warp:
        photo = simulate_camera_photo(canonical_bgr)
    else:
        photo = canonical_bgr

    ground_truth = {
        "tc_kimlik": tc_kimlik,
        "isim": isim,
        "cevaplar": answer_pattern,
    }
    return photo, ground_truth


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentetik test optik formu üretici")
    parser.add_argument("--template", default="config/omr_template.json")
    parser.add_argument("--out", default="data/uploads/ornek_test_formu.jpg")
    parser.add_argument("--tc", default="10000000146")
    parser.add_argument("--isim", default="AHMET YILMAZ")
    parser.add_argument("--no-warp", action="store_true")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent.parent
    template_path = base_dir / args.template
    out_path = base_dir / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    photo, ground_truth = generate_sample(
        template_path, tc_kimlik=args.tc, isim=args.isim, warp=not args.no_warp
    )
    cv2.imwrite(str(out_path), photo)
    print(f"Test formu kaydedildi: {out_path}")
    print(f"Gerçek (ground-truth) değerler: {ground_truth}")


if __name__ == "__main__":
    main()
