# -*- coding: utf-8 -*-
"""
tests/test_crypto_utils.py
===========================
crypto_utils.py modülü için birim testleri: KVKK anonimleştirme zincirinin
(isim normalizasyonu -> TCKN doğrulaması -> k anahtarı türetimi -> yerel
bütünlük özeti) doğruluğunu ve determinizmini doğrular.

Çalıştırma: pytest tests/test_crypto_utils.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import crypto_utils


# Resmî algoritmaya göre geçerli kontrol basamaklarına sahip, test amaçlı
# türetilmiş bir TC kimlik numarası (gerçek bir kişiye ait değildir).
VALID_TC = "10000000146"
INVALID_CHECKSUM_TC = "10000000147"  # son hane bilerek bozulmuş


class TestNormalizeName:
    def test_turkish_characters_are_converted(self):
        assert crypto_utils.normalize_name("şükrü öztürk") == "SUKRU OZTURK"

    def test_already_uppercase_ascii_is_unchanged(self):
        assert crypto_utils.normalize_name("AHMET YILMAZ") == "AHMET YILMAZ"

    def test_multiple_spaces_collapse_to_one(self):
        assert crypto_utils.normalize_name("Mehmet   Öz") == "MEHMET OZ"

    def test_punctuation_is_stripped(self):
        assert crypto_utils.normalize_name("Ay-şe N.") == "AYSE N"

    def test_two_different_spellings_normalize_identically(self):
        # Aynı kişinin formda farklı yazılmış olması, anonimleştirme
        # anahtarının farklılaşmasına yol AÇMAMALIDIR.
        a = crypto_utils.normalize_name("Mehmet  ÖZ")
        b = crypto_utils.normalize_name("MEHMET OZ")
        assert a == b

    def test_none_input_returns_empty_string(self):
        assert crypto_utils.normalize_name(None) == ""


class TestNormalizeTcKimlik:
    def test_valid_tc_passes(self):
        assert crypto_utils.normalize_tc_kimlik(VALID_TC) == VALID_TC

    def test_strips_non_digit_characters(self):
        spaced = "1 0000000146"
        assert crypto_utils.normalize_tc_kimlik(spaced) == VALID_TC

    def test_wrong_length_raises(self):
        with pytest.raises(crypto_utils.InvalidTCKimlikError):
            crypto_utils.normalize_tc_kimlik("123")

    def test_leading_zero_raises(self):
        with pytest.raises(crypto_utils.InvalidTCKimlikError):
            crypto_utils.normalize_tc_kimlik("01234567890")

    def test_bad_checksum_raises(self):
        with pytest.raises(crypto_utils.InvalidTCKimlikError):
            crypto_utils.normalize_tc_kimlik(INVALID_CHECKSUM_TC)

    def test_none_raises(self):
        with pytest.raises(crypto_utils.InvalidTCKimlikError):
            crypto_utils.normalize_tc_kimlik(None)


class TestGenerateAnonKey:
    def test_is_deterministic(self):
        a = crypto_utils.generate_anon_key(VALID_TC, "AHMET YILMAZ", "2026-06-17", "tuz-1")
        b = crypto_utils.generate_anon_key(VALID_TC, "AHMET YILMAZ", "2026-06-17", "tuz-1")
        assert a.k_hex == b.k_hex

    def test_name_spelling_variants_produce_same_key(self):
        a = crypto_utils.generate_anon_key(VALID_TC, "Mehmet  Öz", "2026-06-17", "tuz-1")
        b = crypto_utils.generate_anon_key(VALID_TC, "MEHMET OZ", "2026-06-17", "tuz-1")
        assert a.k_hex == b.k_hex

    def test_different_salt_produces_different_key(self):
        a = crypto_utils.generate_anon_key(VALID_TC, "AHMET YILMAZ", "2026-06-17", "tuz-1")
        b = crypto_utils.generate_anon_key(VALID_TC, "AHMET YILMAZ", "2026-06-17", "tuz-2")
        assert a.k_hex != b.k_hex

    def test_different_date_produces_different_key(self):
        a = crypto_utils.generate_anon_key(VALID_TC, "AHMET YILMAZ", "2026-06-17", "tuz-1")
        b = crypto_utils.generate_anon_key(VALID_TC, "AHMET YILMAZ", "2026-06-18", "tuz-1")
        assert a.k_hex != b.k_hex

    def test_k_hex_is_64_chars_and_bytes32_is_32_bytes(self):
        result = crypto_utils.generate_anon_key(VALID_TC, "AHMET YILMAZ", "2026-06-17", "tuz-1")
        assert len(result.k_hex) == 64
        assert len(result.k_bytes32) == 32
        assert bytes.fromhex(result.k_hex) == result.k_bytes32

    def test_empty_salt_raises(self):
        with pytest.raises(ValueError):
            crypto_utils.generate_anon_key(VALID_TC, "AHMET YILMAZ", "2026-06-17", "")

    def test_invalid_tc_raises_when_validation_enabled(self):
        with pytest.raises(crypto_utils.InvalidTCKimlikError):
            crypto_utils.generate_anon_key(
                INVALID_CHECKSUM_TC, "AHMET YILMAZ", "2026-06-17", "tuz-1", validate_tc=True
            )

    def test_invalid_tc_skipped_when_validation_disabled(self):
        # validate_tc=False, sadece rakam olmayan karakterleri temizler,
        # kontrol basamağı algoritmasını çalıştırmaz.
        result = crypto_utils.generate_anon_key(
            INVALID_CHECKSUM_TC, "AHMET YILMAZ", "2026-06-17", "tuz-1", validate_tc=False
        )
        assert result.tc_normalized == INVALID_CHECKSUM_TC


class TestComputeLocalIntegrityHash:
    def test_is_deterministic(self):
        h1 = crypto_utils.compute_local_integrity_hash("kabc", ["A", "B", None], "imgabc")
        h2 = crypto_utils.compute_local_integrity_hash("kabc", ["A", "B", None], "imgabc")
        assert h1 == h2

    def test_answer_order_changes_hash(self):
        h1 = crypto_utils.compute_local_integrity_hash("kabc", ["A", "B"], "imgabc")
        h2 = crypto_utils.compute_local_integrity_hash("kabc", ["B", "A"], "imgabc")
        assert h1 != h2

    def test_none_answers_are_represented_consistently(self):
        h1 = crypto_utils.compute_local_integrity_hash("kabc", [None, "A"], "imgabc")
        h2 = crypto_utils.compute_local_integrity_hash("kabc", ["", "A"], "imgabc")
        assert h1 == h2


class TestBytes32Conversion:
    def test_roundtrip(self):
        k_hex = crypto_utils.sha256_hex("test-deger")
        as_bytes = crypto_utils.k_hex_to_bytes32(k_hex)
        back_to_hex = crypto_utils.bytes32_to_hex(as_bytes)
        assert back_to_hex == "0x" + k_hex

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError):
            crypto_utils.k_hex_to_bytes32("deadbeef")
