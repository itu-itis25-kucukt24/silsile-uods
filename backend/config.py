# -*- coding: utf-8 -*-
"""
config.py
=========
Uç Optik Doğrulama Sistemi (UODS) için merkezi yapılandırma modülü.

Bu modül, .env dosyasındaki tüm ortam değişkenlerini python-dotenv
aracılığıyla belleğe yükler ve uygulamanın diğer katmanlarına tek bir
kaynaktan (Config sınıfı) sunar. Hiçbir gizli anahtar (API key, özel
anahtar vb.) kaynak kodun içine doğrudan yazılmaz [15].

Önemli tasarım kararı: Bu modül import edildiğinde DOĞRULAMA HATASI
fırlatmaz. Eksik olan kritik değerler (örn. PINATA_JWT, INSTITUTION_
PRIVATE_KEY) yalnızca ilgili servis (ipfs_service / blockchain_service)
gerçekten çağrıldığında kontrol edilir. Bu sayede görüntü işleme ve OMR
modülleri, blokzincir kimlik bilgileri olmadan da bağımsız test edilebilir.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Proje kök dizini (bu dosyanın bulunduğu klasör)
BASE_DIR = Path(__file__).resolve().parent

# .env dosyasını yükle (varsa). Yoksa sistem ortam değişkenleri kullanılır.
load_dotenv(BASE_DIR / ".env")


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "evet", "on")


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Uygulama genelinde kullanılan değişmez (frozen) yapılandırma nesnesi."""

    # --- Flask -----------------------------------------------------------
    FLASK_ENV: str = os.getenv("FLASK_ENV", "production")
    SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
    HOST: str = os.getenv("FLASK_HOST", "0.0.0.0")
    PORT: int = _get_int("FLASK_PORT", 5000)
    MAX_UPLOAD_SIZE_MB: int = _get_int("MAX_UPLOAD_SIZE_MB", 15)

    # --- KVKK / Anonimleştirme --------------------------------------------
    KURUM_TUZU: str = os.getenv("KURUM_TUZU", "")
    KURUM_ADI: str = os.getenv("KURUM_ADI", "BILINMEYEN-KURUM")

    # --- IPFS / Pinata -----------------------------------------------------
    PINATA_API_KEY: str = os.getenv("PINATA_API_KEY", "")
    PINATA_API_SECRET: str = os.getenv("PINATA_API_SECRET", "")
    PINATA_JWT: str = os.getenv("PINATA_JWT", "")
    PINATA_GATEWAY_BASE: str = os.getenv(
        "PINATA_GATEWAY_BASE", "https://gateway.pinata.cloud/ipfs"
    )

    # --- Blokzincir (Polygon) ----------------------------------------------
    POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "")
    POLYGON_CHAIN_ID: int = _get_int("POLYGON_CHAIN_ID", 80002)
    INSTITUTION_PRIVATE_KEY: str = os.getenv("INSTITUTION_PRIVATE_KEY", "")
    CONTRACT_ADDRESS: str = os.getenv("CONTRACT_ADDRESS", "")
    MAX_PRIORITY_FEE_GWEI: float = _get_float("MAX_PRIORITY_FEE_GWEI", 30.0)
    MAX_FEE_GWEI: float = _get_float("MAX_FEE_GWEI", 60.0)

    # --- Çevrimdışı Tampon (SQLite) -----------------------------------------
    SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "instance/uods_queue.db")
    SYNC_INTERVAL_SECONDS: int = _get_int("SYNC_INTERVAL_SECONDS", 15)
    SYNC_MAX_RETRY: int = _get_int("SYNC_MAX_RETRY", 5)

    # --- OMR ----------------------------------------------------------------
    OMR_TEMPLATE_PATH: str = os.getenv("OMR_TEMPLATE_PATH", "config/omr_template.json")

    # --- /verify Erişim Koruması (HTTP Basic Auth) --------------------------
    # Tanımlıysa /verify ekranı HTTP Basic Auth ile korunur. Şifre asla
    # düz metin olarak saklanmaz; werkzeug.security.generate_password_hash
    # ile üretilmiş bir özet (hash) burada tutulur.
    INSTITUTION_VERIFY_USERNAME: str = os.getenv("INSTITUTION_VERIFY_USERNAME", "")
    INSTITUTION_VERIFY_PASSWORD_HASH: str = os.getenv("INSTITUTION_VERIFY_PASSWORD_HASH", "")

    # --- Loglama --------------------------------------------------------------
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR: Path = field(default_factory=lambda: BASE_DIR / "instance" / "logs")

    # --- Hata İzleme (Sentry, isteğe bağlı) ------------------------------------
    SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")

    # --- Dosya yolları --------------------------------------------------------
    BASE_DIR: Path = field(default_factory=lambda: BASE_DIR)
    UPLOAD_DIR: Path = field(default_factory=lambda: BASE_DIR / "data" / "uploads")
    PROCESSED_DIR: Path = field(default_factory=lambda: BASE_DIR / "data" / "processed")
    CONTRACT_ABI_PATH: Path = field(
        default_factory=lambda: BASE_DIR / "contracts" / "OpticalFormRegistry.abi.json"
    )

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def sqlite_full_path(self) -> Path:
        p = Path(self.SQLITE_DB_PATH)
        if not p.is_absolute():
            p = self.BASE_DIR / p
        return p

    @property
    def verify_auth_enabled(self) -> bool:
        """INSTITUTION_VERIFY_USERNAME ve _PASSWORD_HASH tanımlıysa True döner."""
        return bool(self.INSTITUTION_VERIFY_USERNAME and self.INSTITUTION_VERIFY_PASSWORD_HASH)

    def ensure_directories(self) -> None:
        """Gerekli klasörlerin var olduğundan emin olur (idempotent)."""
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        self.sqlite_full_path.parent.mkdir(parents=True, exist_ok=True)
        self.LOG_DIR.mkdir(parents=True, exist_ok=True)

    def validate_for_ipfs(self) -> None:
        """IPFS işlemi öncesi gerekli alanları doğrular."""
        if not self.PINATA_JWT and not (self.PINATA_API_KEY and self.PINATA_API_SECRET):
            raise RuntimeError(
                "Pinata kimlik bilgileri eksik: PINATA_JWT veya "
                "(PINATA_API_KEY + PINATA_API_SECRET) ortam değişkenlerini "
                ".env dosyasında tanımlayın."
            )

    def validate_for_blockchain(self) -> None:
        """Blokzincir işlemi öncesi gerekli alanları doğrular."""
        missing = []
        if not self.POLYGON_RPC_URL:
            missing.append("POLYGON_RPC_URL")
        if not self.INSTITUTION_PRIVATE_KEY or self.INSTITUTION_PRIVATE_KEY.startswith(
            "0xBU_DEGERI"
        ):
            missing.append("INSTITUTION_PRIVATE_KEY")
        if not self.CONTRACT_ADDRESS or self.CONTRACT_ADDRESS == (
            "0x0000000000000000000000000000000000000000"
        ):
            missing.append("CONTRACT_ADDRESS")
        if missing:
            raise RuntimeError(
                "Blokzincir yapılandırması eksik: " + ", ".join(missing) +
                " ortam değişkenlerini .env dosyasında tanımlayın."
            )

    def validate_for_anonymization(self) -> None:
        if not self.KURUM_TUZU:
            raise RuntimeError(
                "KURUM_TUZU tanımlı değil. KVKK anonimleştirme adımı için "
                "kuruma özel gizli bir tuz değeri .env dosyasında "
                "tanımlanmalıdır."
            )


# Uygulama genelinde paylaşılan tek (singleton) yapılandırma örneği.
config = Config()
config.ensure_directories()
