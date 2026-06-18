# -*- coding: utf-8 -*-
"""
tests/test_image_processing.py
================================
image_processing.py modülü için birim testleri: BT.601 gri tonlama
ağırlıklarının doğruluğu, Gauss bulanıklaştırma, Otsu eşiklemesi ve
bozuk/desteklenmeyen girdilerde anlaşılır hata fırlatılması.

Çalıştırma: pytest tests/test_image_processing.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest

from image_processing import (
    ImageProcessingError,
    apply_gaussian_blur,
    apply_otsu_threshold,
    load_image,
    process_image,
    resize_if_needed,
    to_grayscale_bt601,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
SAMPLE_IMAGE = DATA_DIR / "test_flat.jpg"


class TestToGrayscaleBt601:
    def test_pure_white_pixel_stays_white(self):
        img = np.full((4, 4, 3), 255, dtype=np.uint8)
        gray = to_grayscale_bt601(img)
        assert gray.shape == (4, 4)
        assert np.all(gray == 255)

    def test_pure_black_pixel_stays_black(self):
        img = np.zeros((4, 4, 3), dtype=np.uint8)
        gray = to_grayscale_bt601(img)
        assert np.all(gray == 0)

    def test_matches_known_bt601_formula(self):
        # BGR sırasıyla tek bir piksel: B=10, G=20, R=30
        img = np.array([[[10, 20, 30]]], dtype=np.uint8)
        gray = to_grayscale_bt601(img)
        expected = round(0.299 * 30 + 0.587 * 20 + 0.114 * 10)
        assert int(gray[0, 0]) == expected

    def test_rejects_non_uint8_input(self):
        img = np.zeros((4, 4, 3), dtype=np.float32)
        with pytest.raises(ImageProcessingError):
            to_grayscale_bt601(img)


class TestApplyGaussianBlur:
    def test_output_shape_matches_input(self):
        gray = np.random.randint(0, 256, (50, 50), dtype=np.uint8)
        blurred = apply_gaussian_blur(gray)
        assert blurred.shape == gray.shape

    def test_smooths_a_single_bright_spot(self):
        gray = np.zeros((21, 21), dtype=np.uint8)
        gray[10, 10] = 255
        blurred = apply_gaussian_blur(gray)
        # Bulanıklaştırma sonrası tek bir nokta artık komşularına yayılmış
        # olmalı; merkezdeki değer 255'ten düşmeli ama sıfır olmamalı.
        assert 0 < blurred[10, 10] < 255

    def test_rejects_even_kernel_size(self):
        gray = np.zeros((10, 10), dtype=np.uint8)
        with pytest.raises(ImageProcessingError):
            apply_gaussian_blur(gray, kernel_size=(4, 4))


class TestApplyOtsuThreshold:
    def test_binary_output_only_has_two_values(self):
        gray = np.random.randint(0, 256, (60, 60), dtype=np.uint8)
        binary, _ = apply_otsu_threshold(gray)
        unique_vals = set(np.unique(binary).tolist())
        assert unique_vals.issubset({0, 255})

    def test_bimodal_image_separates_correctly(self):
        gray = np.full((20, 20), 60, dtype=np.uint8)
        gray[:, 10:] = 200  # iki net küme: koyu gri ve açık gri
        binary, otsu_val = apply_otsu_threshold(gray)
        assert binary[0, 0] == 0       # koyu küme -> eşik altı -> 0
        assert binary[0, 19] == 255    # açık küme -> eşik üstü -> 255
        assert 60 <= otsu_val < 200


class TestResizeIfNeeded:
    def test_small_image_is_not_resized(self):
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        resized = resize_if_needed(img, max_dim=2600)
        assert resized.shape == img.shape

    def test_large_image_is_downscaled_preserving_aspect_ratio(self):
        img = np.zeros((4000, 2000, 3), dtype=np.uint8)
        resized = resize_if_needed(img, max_dim=2600)
        h, w = resized.shape[:2]
        assert max(h, w) == 2600
        assert abs((w / h) - (2000 / 4000)) < 0.01


class TestLoadImage:
    def test_missing_file_raises(self):
        with pytest.raises(ImageProcessingError):
            load_image("/tmp/does_not_exist_uods_test.jpg")

    def test_garbage_bytes_raise(self):
        with pytest.raises(ImageProcessingError):
            load_image(b"this is not a valid image file")

    def test_grayscale_ndarray_input_is_rejected(self):
        gray_only = np.zeros((10, 10), dtype=np.uint8)
        with pytest.raises(ImageProcessingError):
            load_image(gray_only)


@pytest.mark.skipif(not SAMPLE_IMAGE.exists(), reason="test_flat.jpg üretilmemiş")
class TestProcessImageIntegration:
    def test_full_pipeline_produces_expected_bundle(self):
        bundle = process_image(SAMPLE_IMAGE)
        assert bundle.grayscale.ndim == 2
        assert bundle.blurred.shape == bundle.grayscale.shape
        assert bundle.binary.shape == bundle.grayscale.shape
        assert set(np.unique(bundle.binary).tolist()).issubset({0, 255})
        assert 0.0 <= bundle.otsu_threshold_value <= 255.0
