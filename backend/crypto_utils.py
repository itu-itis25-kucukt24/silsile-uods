# -*- coding: utf-8 -*-
"""
crypto_utils.py
===============
KVKK (6698 sayılı Kişisel Verilerin Korunması Kanunu) uyumlu kriptografik
anonimleştirme işlemlerini içerir [8, 12, 25].

Bu modüldeki fonksiyonlar projenin "1. PROJE ÖZETİ" ve "3.7.2" bölümlerinde
tanımlanan iki temel denklemi uygular:

    k = SHA-256(TC ∥ isim_normalize ∥ tarih ∥ kurum_tuzu)         (Denklem 10)
    h_local = SHA-256(k ∥ cevap_dizisi ∥ görüntü_hash)            (Denklem 9)

Burada ∥ sembolü birleştirme (concatenation) operatörünü ifade eder. Bu
modülde birleştirme, ayrıştırma belirsizliğini (ambiguity) önlemek için
sabit bir ayraç karakteri ("|") kullanılarak yapılır; aksi hâlde
"12" ∥ "3" ile "1" ∥ "23" gibi farklı girdiler aynı sonucu üretebilirdi.

ÖNEMLİ GÜVENLİK NOTU: SHA-256 tek yönlü (one-way) bir özet fonksiyonudur.
Kurum_tuzu değeri bilinmeden, üretilen k anahtarından TC kimlik numarasına
ya da isme geri dönüş hesaplama açısından mümkün değildir [25]. Bu nedenle
kurum_tuzu değeri .env dosyasında saklanmalı ve KESİNLİKLE paylaşılmamalıdır.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Union

# Alan ayraç karakteri: birleştirme işleminde alanlar arasına eklenir.
# Bu sayede "ABC" + "DEF" ile "AB" + "CDEF" gibi çakışmalar önlenir.
_FIELD_SEPARATOR = "|"

# Türkçe karakterlerin ASCII büyük harf normalizasyon tablosu.
_TURKISH_UPPER_MAP = {
    "ç": "C", "Ç": "C",
    "ğ": "G", "Ğ": "G",
    "ı": "I", "I": "I", "İ": "I", "i": "I",
    "ö": "O", "Ö": "O",
    "ş": "S", "Ş": "S",
    "ü": "U", "Ü": "U",
}


class InvalidTCKimlikError(ValueError):
    """TC kimlik numarası formatı geçersiz olduğunda fırlatılır."""


def normalize_name(raw_name: str) -> str:
    """
    İsim/soyisim metnini, optik formdan OMR ile okunan baloncuk dizisiyle
    daima aynı kanonik forma indirger.

    İşlem sırası:
      1. Türkçe özel karakterler ASCII karşılıklarına çevrilir (İ->I, ş->S vb.)
      2. Tüm karakterler büyük harfe çevrilir.
      3. Birden fazla boşluk tek boşluğa indirilir, baş/son boşluk atılır.
      4. Harf ve boşluk dışındaki karakterler (noktalama vb.) temizlenir.

    Bu normalizasyon, aynı kişinin formda "Mehmet  Öz" ya da "MEHMET OZ"
    şeklinde farklı yazılmış olsa da SHA-256 girdisi olarak HER ZAMAN aynı
    metni üretmesini sağlar; aksi hâlde aynı kişi için farklı k değerleri
    üretilir ve doğrulama bütünlüğü bozulur.
    """
    if raw_name is None:
        raw_name = ""

    translated_chars = []
    for ch in raw_name:
        if ch in _TURKISH_UPPER_MAP:
            translated_chars.append(_TURKISH_UPPER_MAP[ch])
        else:
            translated_chars.append(ch)
    text = "".join(translated_chars)

    # Kalan olası aksanlı karakterleri (NFKD ile) ayrıştırıp birleştirici
    # işaretleri (combining marks) at; ardından büyük harfe çevir.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.upper()

    # Sadece A-Z ve boşluk karakterlerine izin ver.
    text = re.sub(r"[^A-Z ]", "", text)
    # Çoklu boşlukları tekille, baş/son boşlukları kırp.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_tc_kimlik(raw_tc: str) -> str:
    """
    TC kimlik numarasını doğrular ve normalize eder.
    Beklenen format: 11 hane, sadece rakam, ilk hane 0 olamaz.

    Not: Bu fonksiyon resmî TCKN algoritmik doğrulamasını (10. ve 11.
    hanenin kontrol basamağı formülü) de uygular; OMR okumasında oluşan
    tek hanelik hataları erken aşamada yakalamak için kritik bir kontroldür.
    """
    if raw_tc is None:
        raise InvalidTCKimlikError("TC kimlik numarası boş olamaz.")

    digits = re.sub(r"\D", "", raw_tc)
    if len(digits) != 11:
        raise InvalidTCKimlikError(
            f"TC kimlik numarası 11 haneli olmalıdır (okunan: {len(digits)} hane)."
        )
    if digits[0] == "0":
        raise InvalidTCKimlikError("TC kimlik numarasının ilk hanesi 0 olamaz.")

    d = [int(c) for c in digits]
    # Resmî TCKN kontrol basamağı algoritması.
    odd_sum = d[0] + d[2] + d[4] + d[6] + d[8]
    even_sum = d[1] + d[3] + d[5] + d[7]
    digit10_check = (odd_sum * 7 - even_sum) % 10
    digit11_check = (sum(d[0:10])) % 10

    if digit10_check != d[9] or digit11_check != d[10]:
        raise InvalidTCKimlikError(
            "TC kimlik numarası kontrol basamağı doğrulaması başarısız. "
            "OMR okuma hatalı olabilir; formun yeniden taranması önerilir."
        )
    return digits


def sha256_hex(*parts: Union[str, bytes]) -> str:
    """
    Verilen parçaları _FIELD_SEPARATOR ile birleştirip SHA-256 özetinin
    onaltılık (hex) gösterimini döndürür. Bayt ve metin girdilerini
    birlikte kabul eder.
    """
    hasher = hashlib.sha256()
    for i, part in enumerate(parts):
        if i > 0:
            hasher.update(_FIELD_SEPARATOR.encode("utf-8"))
        if isinstance(part, bytes):
            hasher.update(part)
        else:
            hasher.update(str(part).encode("utf-8"))
    return hasher.hexdigest()


def sha256_file(file_path: Union[str, Path], chunk_size: int = 65536) -> str:
    """
    Bir dosyanın içeriğinin SHA-256 özetini bellek dostu biçimde (chunk'lar
    hâlinde okuyarak) hesaplar. Bu fonksiyon "görüntü_hash" değerinin ve
    IPFS'e yüklenecek içeriğin dijital parmak izinin elde edilmesinde
    kullanılır (Denklem 8'deki CID mantığına paralel, yerel doğrulama
    amaçlıdır) [7].
    """
    path = Path(file_path)
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Bayt dizisinin SHA-256 özetini hex olarak döndürür."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class AnonKeyResult:
    """generate_anon_key() çağrısının döndürdüğü sonuç paketi."""
    k_hex: str          # 64 karakterlik hex string (32 bayt)
    k_bytes32: bytes     # Solidity'ye doğrudan gönderilebilecek 32 baytlık form
    tc_normalized: str
    isim_normalized: str
    tarih: str


def generate_anon_key(
    tc_kimlik: str,
    isim: str,
    tarih: str,
    kurum_tuzu: str,
    validate_tc: bool = True,
) -> AnonKeyResult:
    """
    Denklem (10)'u uygular:
        k = SHA-256(TC ∥ isim_normalize ∥ tarih ∥ kurum_tuzu)

    Parametreler
    ------------
    tc_kimlik : OMR ile okunan, henüz normalize edilmemiş TC kimlik no.
    isim      : OMR ile okunan, henüz normalize edilmemiş ad-soyad.
    tarih     : Sınav tarihi (ISO-8601 formatı önerilir, örn. "2026-06-17").
    kurum_tuzu: Kuruma özel, .env dosyasında saklanan gizli tuz değeri.
    validate_tc: True ise TCKN kontrol basamağı algoritmik olarak doğrulanır.

    Dönüş
    -----
    AnonKeyResult: k anahtarının hem hex hem bytes32 biçimi ve normalize
    edilmiş ara değerler (loglama/hata ayıklama amaçlı, ASLA blokzincire
    veya kalıcı depoya yazılmamalıdır).
    """
    if not kurum_tuzu:
        raise ValueError(
            "kurum_tuzu boş olamaz. Anonimleştirme adımı kuruma özel gizli "
            "bir tuz değeri gerektirir (KVKK uyumluluğu için zorunludur)."
        )
    if not tarih:
        raise ValueError("tarih boş olamaz.")

    tc_norm = normalize_tc_kimlik(tc_kimlik) if validate_tc else re.sub(r"\D", "", tc_kimlik or "")
    isim_norm = normalize_name(isim)

    if not isim_norm:
        raise ValueError("İsim normalizasyonu sonucunda boş bir değer elde edildi.")

    k_hex = sha256_hex(tc_norm, isim_norm, tarih, kurum_tuzu)
    k_bytes32 = bytes.fromhex(k_hex)
    if len(k_bytes32) != 32:
        # SHA-256 her zaman 32 bayt üretir; bu kontrol bir bütünlük güvencesidir.
        raise RuntimeError("k anahtarı 32 bayt (bytes32) olmalıdır; beklenmeyen durum.")

    return AnonKeyResult(
        k_hex=k_hex,
        k_bytes32=k_bytes32,
        tc_normalized=tc_norm,
        isim_normalized=isim_norm,
        tarih=tarih,
    )


def compute_local_integrity_hash(
    k_hex: str,
    cevap_dizisi: Iterable[str],
    goruntu_hash: str,
) -> str:
    """
    Denklem (9)'u uygular:
        h_local = SHA-256(k ∥ cevap_dizisi ∥ görüntü_hash)

    cevap_dizisi listesi, deterministik bir metne dönüştürülmeden önce
    sırası korunarak "," ile birleştirilir (sıra bilgisi de bütünlüğe
    dahildir; cevapların yer değiştirmesi farklı bir hash üretmelidir).
    """
    cevap_str = ",".join("" if c is None else str(c) for c in cevap_dizisi)
    return sha256_hex(k_hex, cevap_str, goruntu_hash)


def k_hex_to_bytes32(k_hex: str) -> bytes:
    """64 karakterlik hex string'i 32 baytlık (bytes32) forma çevirir."""
    cleaned = k_hex.lower().replace("0x", "")
    if len(cleaned) != 64:
        raise ValueError(f"k_hex 64 hex karakter olmalıdır, alınan uzunluk: {len(cleaned)}")
    return bytes.fromhex(cleaned)


def bytes32_to_hex(value: bytes) -> str:
    """32 baytlık değeri '0x' önekli hex string'e çevirir."""
    return "0x" + value.hex()
