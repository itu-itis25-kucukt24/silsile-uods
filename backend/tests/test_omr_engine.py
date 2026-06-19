# -*- coding: utf-8 -*-
"""
tests/test_omr_engine.py
==========================
omr_engine.py için uçtan uca testler. scripts/generate_test_form.py ile
üretilmiş, gerçek (bilinen) cevap dizisine sahip sentetik optik formlar
üzerinde tam hattı (referans tespiti -> perspektif düzeltme -> alan bazlı
karar üretimi) doğrular. Hem düz hem de perspektif bozulmalı (kamera ile
çekilmiş gibi simüle edilmiş) görüntüler test edilir.

Çalıştırma: pytest tests/test_omr_engine.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest

from config import config
from image_processing import process_image
from omr_engine import EMPTY_MARK, INVALID_MARK, decode_field, load_omr_template, process_form

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
FLAT_IMAGE = DATA_DIR / "test_flat.jpg"
WARPED_IMAGE = DATA_DIR / "test_warped.jpg"

EXPECTED_TC = "10000000146"
EXPECTED_ISIM = "AHMET YILMAZ"
EXPECTED_CEVAPLAR = [
    "A", "B", "C", None, "E", "A", "B", "C", "D", "E",
    "A", None, "C", "D", "E", "A", "B", "C", "D", "E",
]


@pytest.fixture(scope="module")
def template():
    return load_omr_template(config.BASE_DIR / config.OMR_TEMPLATE_PATH)


def _expected_with_codes():
    """None -> EMPTY_MARK kodlu beklenen dizi (omr_engine'in döndürdüğü ham koda eşit)."""
    return [EMPTY_MARK if v is None else v for v in EXPECTED_CEVAPLAR]


@pytest.mark.skipif(not FLAT_IMAGE.exists(), reason="test_flat.jpg üretilmemiş")
class TestProcessFormFlat:
    def test_decodes_tc_kimlik_correctly(self, template):
        bundle = process_image(FLAT_IMAGE)
        result = process_form(bundle.blurred, template)
        assert result.tc_kimlik_str == EXPECTED_TC

    def test_decodes_isim_correctly(self, template):
        bundle = process_image(FLAT_IMAGE)
        result = process_form(bundle.blurred, template)
        assert result.isim_str == EXPECTED_ISIM

    def test_decodes_all_cevaplar_correctly(self, template):
        bundle = process_image(FLAT_IMAGE)
        result = process_form(bundle.blurred, template)
        assert result.cevaplar_raw == _expected_with_codes()


@pytest.mark.skipif(not WARPED_IMAGE.exists(), reason="test_warped.jpg üretilmemiş")
class TestProcessFormWarped:
    """Perspektif bozulmalı (kamera açısıyla simüle edilmiş) görüntü; bu test
    referans işaretçi tespiti + perspektif düzeltme (warp) adımının da
    doğru çalıştığını kanıtlar, sadece düz tarama senaryosunu değil."""

    def test_decodes_tc_kimlik_correctly(self, template):
        bundle = process_image(WARPED_IMAGE)
        result = process_form(bundle.blurred, template)
        assert result.tc_kimlik_str == EXPECTED_TC

    def test_decodes_isim_correctly(self, template):
        bundle = process_image(WARPED_IMAGE)
        result = process_form(bundle.blurred, template)
        assert result.isim_str == EXPECTED_ISIM

    def test_decodes_all_cevaplar_correctly(self, template):
        bundle = process_image(WARPED_IMAGE)
        result = process_form(bundle.blurred, template)
        assert result.cevaplar_raw == _expected_with_codes()

    def test_canonical_binary_has_template_canvas_dimensions(self, template):
        bundle = process_image(WARPED_IMAGE)
        result = process_form(bundle.blurred, template)
        assert result.canonical_binary.shape == (template.canvas_height, template.canvas_width)


class TestDecodeFieldInvalidBranch:
    """decode_field'in GECERSIZ (çoklu işaretleme) kararını doğru üretip
    üretmediğini, yapay olarak iki hücreyi birden doldurulmuş (siyah)
    yaparak doğrudan test eder."""

    def test_double_filled_cell_returns_invalid_mark(self, template):
        canvas = np.full((template.canvas_height, template.canvas_width), 255, dtype=np.uint8)
        field = next(f for f in template.fields if f.name == "cevaplar")
        grid = template.grid

        # İlk soru için A ve B seçeneklerinin hücrelerini birlikte doldur.
        row = field.start_row
        for col in (field.start_col, field.start_col + 1):
            cx = grid.origin_x + col * grid.cell_width + grid.cell_width // 2
            cy = grid.origin_y + row * grid.cell_height + grid.cell_height // 2
            half = grid.cell_width // 3
            canvas[cy - half : cy + half, cx - half : cx + half] = 0

        result = decode_field(canvas, grid, field, template.decision_threshold)
        assert result.raw_values[0] == INVALID_MARK

    def test_single_filled_cell_at_adjacent_question_still_decodes(self, template):
        canvas = np.full((template.canvas_height, template.canvas_width), 255, dtype=np.uint8)
        field = next(f for f in template.fields if f.name == "cevaplar")
        grid = template.grid

        row = field.start_row + 1
        col = field.start_col + 2  # "C" seçeneği
        cx = grid.origin_x + col * grid.cell_width + grid.cell_width // 2
        cy = grid.origin_y + row * grid.cell_height + grid.cell_height // 2
        half = grid.cell_width // 3
        canvas[cy - half : cy + half, cx - half : cx + half] = 0

        result = decode_field(canvas, grid, field, template.decision_threshold)
        assert result.raw_values[1] == "C"
        assert result.raw_values[0] == EMPTY_MARK
