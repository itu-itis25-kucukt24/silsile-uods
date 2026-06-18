# -*- coding: utf-8 -*-
"""
ipfs_service.py
================
Pinata bulut servisi [19] üzerinden temizlenmiş optik form görüntülerinin
IPFS (InterPlanetary File System) [7] ağına yüklenmesini sağlayan modül.
Rapor bölüm 3.5'teki "Merkeziyetsiz Dosya Depolama" mimarisini uygular.

Pinata'nın kendi içerik özetleme süreci CID'yi (Content Identifier)
üretir (Denklem 8: CID = SHA-256(dosya içeriği), IPFS'te teknik olarak
multihash/CIDv1 biçiminde, fakat kavramsal olarak aynı "içerik tabanlı
parmak izi" mantığını taşır). Bu modül CID'yi ÜRETMEZ; Pinata'dan
DÖNDÜRÜLEN değeri olduğu gibi kullanır.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import requests

from config import Config

_PINATA_PIN_FILE_URL = "https://api.pinata.cloud/pinning/pinFileToIPFS"
_PINATA_TEST_AUTH_URL = "https://api.pinata.cloud/data/testAuthentication"

_DEFAULT_TIMEOUT_SECONDS = 60


class IPFSServiceError(Exception):
    """IPFS/Pinata servisindeki hatalar için temel istisna sınıfı."""


class PinataAuthenticationError(IPFSServiceError):
    """Pinata API kimlik bilgileri geçersiz veya eksikse fırlatılır."""


class PinataUploadError(IPFSServiceError):
    """Dosya yükleme isteği başarısız olduğunda fırlatılır."""


@dataclass(frozen=True)
class IPFSUploadResult:
    """Pinata'ya başarılı bir yükleme sonrası elde edilen bilgiler."""

    cid: str
    pin_size_bytes: int
    timestamp: str
    gateway_url: str


def _auth_headers(config: Config) -> dict:
    """
    Pinata kimlik doğrulama başlıklarını oluşturur. JWT varsa o tercih
    edilir (Pinata'nın önerdiği yöntem); yoksa klasik API Key/Secret
    çiftine geri düşülür.
    """
    if config.PINATA_JWT:
        return {"Authorization": f"Bearer {config.PINATA_JWT}"}
    return {
        "pinata_api_key": config.PINATA_API_KEY,
        "pinata_secret_api_key": config.PINATA_API_SECRET,
    }


def test_pinata_authentication(config: Config) -> bool:
    """
    Pinata kimlik bilgilerinin geçerli olup olmadığını, dosya yüklemeden
    önce hafif bir istekle (testAuthentication) doğrular.
    """
    config.validate_for_ipfs()
    try:
        resp = requests.get(
            _PINATA_TEST_AUTH_URL,
            headers=_auth_headers(config),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise IPFSServiceError(f"Pinata'ya bağlanılamadı: {exc}") from exc

    if resp.status_code == 200:
        return True
    if resp.status_code in (401, 403):
        raise PinataAuthenticationError(
            f"Pinata kimlik doğrulaması başarısız (HTTP {resp.status_code}): {resp.text}"
        )
    raise IPFSServiceError(f"Pinata testAuthentication beklenmeyen yanıt döndürdü: {resp.status_code} {resp.text}")


def upload_file_to_ipfs(
    config: Config,
    file_path: Union[str, Path],
    pin_name: Optional[str] = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> IPFSUploadResult:
    """
    Verilen dosyayı, requests kütüphanesi ile Pinata'nın
    'pinFileToIPFS' REST uç noktasına multipart/form-data [13] biçiminde
    yükler ve karşılığında CID değerini alır (rapor 3.5.2).

    file_path : Yüklenecek temizlenmiş optik form görüntüsünün yerel
                dosya yolu (örn. Otsu eşiklemesi uygulanmış .png/.jpg).
    pin_name  : Pinata panelinde görünecek isteğe bağlı insan-okunur isim.
    """
    config.validate_for_ipfs()

    path = Path(file_path)
    if not path.exists() or not path.is_file():
        raise IPFSServiceError(f"Yüklenecek dosya bulunamadı: {path}")

    mime_type, _ = mimetypes.guess_type(str(path))
    mime_type = mime_type or "application/octet-stream"

    pin_name = pin_name or path.name

    try:
        with open(path, "rb") as f:
            files = {"file": (path.name, f, mime_type)}
            data = {
                "pinataMetadata": _json_dumps_compact({"name": pin_name}),
                "pinataOptions": _json_dumps_compact({"cidVersion": 1}),
            }
            resp = requests.post(
                _PINATA_PIN_FILE_URL,
                headers=_auth_headers(config),
                files=files,
                data=data,
                timeout=timeout_seconds,
            )
    except requests.RequestException as exc:
        raise PinataUploadError(f"Pinata'ya dosya yüklenirken ağ hatası oluştu: {exc}") from exc

    if resp.status_code == 200:
        body = resp.json()
        cid = body.get("IpfsHash")
        if not cid:
            raise PinataUploadError(f"Pinata yanıtında IpfsHash alanı bulunamadı: {body}")
        pin_size = int(body.get("PinSize", 0))
        timestamp = body.get("Timestamp", "")
        return IPFSUploadResult(
            cid=cid,
            pin_size_bytes=pin_size,
            timestamp=timestamp,
            gateway_url=build_gateway_url(config, cid),
        )

    if resp.status_code in (401, 403):
        raise PinataAuthenticationError(
            f"Pinata kimlik doğrulaması başarısız (HTTP {resp.status_code}): {resp.text}"
        )

    raise PinataUploadError(
        f"Pinata dosya yükleme isteği başarısız oldu (HTTP {resp.status_code}): {resp.text}"
    )


def upload_bytes_to_ipfs(
    config: Config,
    data: bytes,
    filename: str,
    pin_name: Optional[str] = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> IPFSUploadResult:
    """
    Diskte ayrı bir dosya oluşturmadan, bellekteki ham bayt dizisini
    doğrudan Pinata'ya yükler (örn. OpenCV'den elde edilen ikili görüntü
    np.ndarray -> cv2.imencode -> bytes zincirinde kullanışlıdır).
    """
    config.validate_for_ipfs()
    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or "application/octet-stream"
    pin_name = pin_name or filename

    try:
        files = {"file": (filename, data, mime_type)}
        form = {
            "pinataMetadata": _json_dumps_compact({"name": pin_name}),
            "pinataOptions": _json_dumps_compact({"cidVersion": 1}),
        }
        resp = requests.post(
            _PINATA_PIN_FILE_URL,
            headers=_auth_headers(config),
            files=files,
            data=form,
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise PinataUploadError(f"Pinata'ya veri yüklenirken ağ hatası oluştu: {exc}") from exc

    if resp.status_code == 200:
        body = resp.json()
        cid = body.get("IpfsHash")
        if not cid:
            raise PinataUploadError(f"Pinata yanıtında IpfsHash alanı bulunamadı: {body}")
        return IPFSUploadResult(
            cid=cid,
            pin_size_bytes=int(body.get("PinSize", 0)),
            timestamp=body.get("Timestamp", ""),
            gateway_url=build_gateway_url(config, cid),
        )

    if resp.status_code in (401, 403):
        raise PinataAuthenticationError(
            f"Pinata kimlik doğrulaması başarısız (HTTP {resp.status_code}): {resp.text}"
        )

    raise PinataUploadError(
        f"Pinata veri yükleme isteği başarısız oldu (HTTP {resp.status_code}): {resp.text}"
    )


def build_gateway_url(config: Config, cid: str) -> str:
    """Verilen CID için kullanıcıya gösterilebilir bir Pinata gateway bağlantısı üretir."""
    base = config.PINATA_GATEWAY_BASE.rstrip("/")
    return f"{base}/{cid}"


def _json_dumps_compact(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
