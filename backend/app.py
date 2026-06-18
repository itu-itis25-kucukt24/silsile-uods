# -*- coding: utf-8 -*-
"""
app.py
======
UODS (Uç Optik Doğrulama Sistemi) Flask sunucusu — rapor Şekil 1'deki
6 adımlı uçtan uca veri akışının orkestrasyon katmanı:

  1) ARAYÜZ      -> GET  /            (HTML form)
  2) FLASK       -> POST /upload      (veriyi yakalar, sıraya alır)
  3) OMR+OpenCV  -> image_processing.process_image() + omr_engine.process_form()
  4) IPFS        -> ipfs_service.upload_file_to_ipfs()
  5) BLOCKCHAIN  -> blockchain_service.send_record_to_chain()
  6) SONUÇ       -> result.html / pending.html

İnternet bağlantısı yoksa adım 4-5 hemen gerçekleşmez; kayıt
offline_queue üzerinde PENDING olarak bekletilir ve arka plandaki
SyncWorker tarafından bağlantı geri geldiğinde otomatik tamamlanır
(rapor bölüm 3.6).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import uuid
from functools import wraps
from pathlib import Path
from typing import Tuple

import cv2
from flask import Flask, jsonify, render_template, request, Response
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

import blockchain_service
import crypto_utils
import ipfs_service
import merkle
import offline_queue
from config import config
from image_processing import ImageProcessingError, process_image
from omr_engine import (
    OMRError,
    load_omr_template,
    process_form,
    redact_personal_fields,
)

# ============================================================================
# UYGULAMA VE BAĞIMLI SERVİSLERİN BAŞLATILMASI
# ============================================================================

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.max_upload_size_bytes
app.config["SECRET_KEY"] = config.SECRET_KEY

# ----------------------------------------------------------------------------
# LOGLAMA
# ----------------------------------------------------------------------------
# Konsola (gunicorn/systemd çıktısı olarak yakalanır) ve dönen bir dosyaya
# (instance/logs/uods.log) eş zamanlı yazar. Üretimde systemd/journalctl
# veya bir log toplayıcı (örn. Loki) konsol çıktısını zaten yakalayacağından
# dosya tabanlı loglama yalnızca yedek/yerel inceleme amaçlıdır.
logger = logging.getLogger("uods")
logger.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
if not logger.handlers:
    _log_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_log_formatter)
    logger.addHandler(_console_handler)

    _file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "uods.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file_handler.setFormatter(_log_formatter)
    logger.addHandler(_file_handler)

# ----------------------------------------------------------------------------
# HATA İZLEME (Sentry, isteğe bağlı)
# ----------------------------------------------------------------------------
# SENTRY_DSN tanımlı değilse bu blok sessizce atlanır; sentry_sdk paketi
# kurulu değilse de uygulama çalışmaya devam eder (zorunlu bağımlılık değil).
if config.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=config.SENTRY_DSN,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.0,
            send_default_pii=False,  # KVKK: kişisel veri Sentry'ye asla gönderilmez
        )
        logger.info("Sentry hata izleme etkinleştirildi.")
    except ImportError:
        logger.warning(
            "SENTRY_DSN tanımlı ama sentry-sdk paketi kurulu değil; "
            "hata izleme devre dışı kalacak (pip install sentry-sdk)."
        )

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

_template_path = config.BASE_DIR / config.OMR_TEMPLATE_PATH
ACTIVE_TEMPLATE = load_omr_template(_template_path)

offline_queue.init_db(config)


def _sync_batch_records(
    records: "list[offline_queue.QueueRecord]",
) -> offline_queue.BatchSyncResult:
    """
    Bir senkronizasyon turundaki TÜM kayıtları tek bir parti (batch) olarak
    işler (rapor 3.6.2 + hakem değerlendirmesine yanıt: Merkle toplu
    mühürleme). Adımlar:

      1) Her kaydın görüntüsü AYRI AYRI IPFS'e (Pinata) yüklenir — bu adım
         kayıt başınadır ve değişmemiştir. Yüklenen görüntü, kişisel veri
         alanları (TC/isim) redaksiyonla gizlenmiş REDAKTE EDİLMİŞ kopyadır
         (bkz. upload anındaki redact_personal_fields çağrısı).
      2) Tüm kayıtların local_hash (h_local, Denklem 9) değerlerinden TEK
         bir Merkle ağacı kurulur.
      3) Ağacın KÖKÜ, zincire TEK BİR işlemle (addBatchRoot) yazılır —
         bireysel hiçbir kayıt değeri zincire yazılmaz.
      4) Her kayıt için, o köke ait bireysel Merkle kanıtı (proof) üretilir
         ve yalnızca yerel veritabanında saklanır (zincire/IPFS'e YAZILMAZ).

    Dönüş: BatchSyncResult (ortak merkle_root_hex + paylaşılan tx_hash +
    her record.id için (ipfs_cid, merkle_proof_json)).

    Parti atomiktir: herhangi bir adım hata verirse istisna fırlatılır ve
    SyncWorker partideki TÜM kayıtları FAILED'a döndürür.
    """
    # 1) Her kaydın görüntüsünü ayrı ayrı IPFS'e yükle.
    per_record_cid: dict = {}
    for record in records:
        upload_result = ipfs_service.upload_file_to_ipfs(
            config, record.image_path, pin_name=f"uods_{record.k_anahtar_hex[:12]}"
        )
        per_record_cid[record.id] = upload_result.cid

    # 2) local_hash (h_local) değerlerinden Merkle ağacı kur. Yaprak sırası,
    #    kanıt üretiminde kullanılacağından deterministik olmalı: records
    #    listesindeki sıra korunur.
    leaves = [merkle.hex_to_bytes32(r.local_hash) for r in records]
    tree = merkle.build_merkle_tree(leaves)

    # 3) Kökü zincire TEK işlemle yaz.
    receipt = blockchain_service.send_batch_to_chain(
        config, tree.root, record_count=len(records), wait_for_receipt=True
    )

    # 4) Her kayıt için bireysel kanıt üret ve sonucu derle.
    per_record: dict = {}
    for idx, record in enumerate(records):
        proof = merkle.generate_proof(tree, idx)
        proof_hex = [merkle.bytes32_to_hex(p) for p in proof]
        per_record[record.id] = (
            per_record_cid[record.id],
            json.dumps(proof_hex, ensure_ascii=False),
        )

    return offline_queue.BatchSyncResult(
        merkle_root_hex=tree.root_hex,
        tx_hash=receipt.tx_hash,
        per_record=per_record,
    )


_sync_worker = offline_queue.SyncWorker(config, _sync_batch_records)
_sync_worker.start()


# ============================================================================
# YARDIMCI FONKSİYONLAR
# ============================================================================

def _allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _unauthorized_response() -> Response:
    return Response(
        "Kimlik doğrulama gerekli.",
        401,
        {"WWW-Authenticate": 'Basic realm="UODS Kurumsal Sorgu"'},
    )


def requires_verify_auth(view_func):
    """
    /verify ekranını HTTP Basic Auth ile korur (rapor 'Bilinen Sınırlamalar'
    bölümünde belirtilen, üretim öncesi eklenmesi gereken erişim katmanı).

    Davranış:
      - INSTITUTION_VERIFY_USERNAME ve INSTITUTION_VERIFY_PASSWORD_HASH her
        ikisi de .env'de tanımlıysa: doğru kullanıcı adı/şifre girilmeden
        erişim reddedilir (401).
      - Tanımlı DEĞİLSE: ekran açık kalır (yerel geliştirme/test için), ancak
        verify.html bunu görünür bir uyarı banner'ıyla kullanıcıya bildirir
        — sessizce güvensiz kalmaz.

    Şifre asla düz metin karşılaştırılmaz; werkzeug.security.check_password_hash
    ile, .env'de saklanan hash karşısında doğrulanır.
    """

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not config.verify_auth_enabled:
            return view_func(*args, **kwargs)

        auth = request.authorization
        if (
            not auth
            or not auth.username
            or auth.username != config.INSTITUTION_VERIFY_USERNAME
            or not check_password_hash(config.INSTITUTION_VERIFY_PASSWORD_HASH, auth.password or "")
        ):
            logger.warning(
                "verify_auth_basarisiz kullanici_adi=%r ip=%r",
                getattr(auth, "username", None),
                request.remote_addr,
            )
            return _unauthorized_response()

        return view_func(*args, **kwargs)

    return wrapper


def _render_error(title: str, message: str, status_code: int = 400):
    return render_template("error.html", title=title, message=message), status_code


def _format_cevap_for_display(raw_values):
    """Cevap listesindeki BOS/GECERSIZ kodlarını kullanıcı arayüzü için Türkçe etiketlere çevirir."""
    from omr_engine import EMPTY_MARK, INVALID_MARK

    out = []
    for v in raw_values:
        if v == EMPTY_MARK:
            out.append({"value": None, "label": "Boş", "css": "bos"})
        elif v == INVALID_MARK:
            out.append({"value": None, "label": "Geçersiz (Çoklu İşaret)", "css": "gecersiz"})
        else:
            out.append({"value": v, "label": v, "css": "isaretli"})
    return out


# ============================================================================
# ROTALAR
# ============================================================================

@app.route("/", methods=["GET"])
def index():
    stats = offline_queue.queue_stats(config)
    return render_template("index.html", stats=stats, kurum_adi=config.KURUM_ADI)


@app.route("/upload", methods=["POST"])
def upload():
    # --- 1) Dosya doğrulama -------------------------------------------------
    if "optik_form" not in request.files:
        return _render_error(
            "Dosya Eksik", "İstekte 'optik_form' adlı bir dosya alanı bulunamadı."
        )

    file = request.files["optik_form"]
    if file.filename == "":
        return _render_error("Dosya Seçilmedi", "Lütfen bir optik form fotoğrafı seçin.")

    if not _allowed_file(file.filename):
        return _render_error(
            "Desteklenmeyen Dosya Türü",
            f"Yalnızca şu uzantılar kabul edilir: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    sinav_tarihi = (request.form.get("sinav_tarihi") or "").strip()
    if not sinav_tarihi:
        return _render_error(
            "Sınav Tarihi Eksik", "Anonimleştirme anahtarı için sınav tarihi gereklidir."
        )

    try:
        config.validate_for_anonymization()
    except RuntimeError as exc:
        return _render_error("Sunucu Yapılandırma Hatası", str(exc), status_code=500)

    # --- 2) Ham fotoğrafı geçici olarak diske kaydet -------------------------
    upload_id = uuid.uuid4().hex
    safe_name = secure_filename(file.filename) or "optik_form.jpg"
    raw_path = config.UPLOAD_DIR / f"{upload_id}_{safe_name}"
    file.save(raw_path)

    # --- 3) Görüntü işleme + OMR --------------------------------------------
    try:
        bundle = process_image(raw_path)
    except ImageProcessingError as exc:
        return _render_error(
            "Görüntü İşlenemedi",
            f"Fotoğraf okunamadı veya bozuk: {exc}. Lütfen daha net bir fotoğraf "
            "ile yeniden deneyin.",
        )

    try:
        omr_result = process_form(bundle.blurred, ACTIVE_TEMPLATE)
    except OMRError as exc:
        return _render_error(
            "Referans İşaretçileri Bulunamadı",
            f"Formun 4 köşesindeki referans kareleri tespit edilemedi: {exc}. "
            "Lütfen formu düz bir zemine koyup, kenarları net görünecek şekilde "
            "yeniden fotoğraflayın.",
        )

    # --- 4) Kimlik bilgisi doğrulama + anonimleştirme ------------------------
    try:
        anon_key = crypto_utils.generate_anon_key(
            tc_kimlik=omr_result.tc_kimlik_str,
            isim=omr_result.isim_str,
            tarih=sinav_tarihi,
            kurum_tuzu=config.KURUM_TUZU,
            validate_tc=True,
        )
    except (crypto_utils.InvalidTCKimlikError, ValueError) as exc:
        return _render_error(
            "Kimlik Bilgisi Okunamadı",
            f"OMR ile okunan TC kimlik / isim bilgisi doğrulanamadı: {exc}. "
            "Bu genellikle düşük ışık, eğik çekim veya silik işaretlemeden "
            "kaynaklanır; lütfen formu yeniden fotoğraflayın.",
        )

    # --- 5) Temizlenmiş (kanonik) görüntüyü REDAKTE EDEREK diske kaydet ------
    # KVKK + hakem değerlendirmesi: IPFS'e yüklenecek/diskte saklanacak
    # görüntüdeki TC kimlik ve isim baloncuk bölgeleri, kalıcı saklamadan
    # ÖNCE beyazla doldurularak görsel olarak da kaldırılır. OMR okuma
    # (yukarıdaki adım) zaten orijinal görüntü üzerinde tamamlandığından,
    # bu redaksiyon okuma doğruluğunu etkilemez. goruntu_hash/h_local,
    # GERÇEKTEN saklanan (redakte edilmiş) görüntü üzerinden hesaplanır;
    # böylece doğrulama anında yeniden hesaplanan hash birebir tutar.
    redacted_canonical = redact_personal_fields(omr_result.canonical_binary, ACTIVE_TEMPLATE)
    processed_filename = f"{upload_id}.png"
    processed_path = config.PROCESSED_DIR / processed_filename
    cv2.imwrite(str(processed_path), redacted_canonical)

    goruntu_hash = crypto_utils.sha256_file(processed_path)
    h_local = crypto_utils.compute_local_integrity_hash(
        k_hex=anon_key.k_hex,
        cevap_dizisi=omr_result.cevaplar_raw,
        goruntu_hash=goruntu_hash,
    )

    # --- 6) Yerel kuyruğa yaz (PENDING) -------------------------------------
    try:
        record_id = offline_queue.enqueue_record(
            config,
            k_anahtar_hex=anon_key.k_hex,
            cevaplar=omr_result.cevaplar_raw,
            image_path=str(processed_path),
            local_hash=h_local,
        )
    except offline_queue.OfflineQueueError as exc:
        return _render_error(
            "Mükerrer Kayıt",
            f"Bu öğrenci ve sınav tarihi için zaten bir kayıt mevcut: {exc}",
            status_code=409,
        )

    # --- 7) Mümkünse hemen senkronize et (internet varsa) --------------------
    _sync_worker.run_once()
    record = offline_queue.get_record_by_id(config, record_id)

    cevap_display = _format_cevap_for_display(omr_result.cevaplar_raw)

    if record.status == offline_queue.STATUS_SYNCED:
        polygonscan_url = blockchain_service.get_polygonscan_tx_url(config, record.tx_hash)
        gateway_url = ipfs_service.build_gateway_url(config, record.ipfs_cid)
        return render_template(
            "result.html",
            record=record,
            cevaplar=cevap_display,
            polygonscan_url=polygonscan_url,
            gateway_url=gateway_url,
            k_anahtar=anon_key.k_hex,
        )

    return render_template(
        "pending.html",
        record=record,
        cevaplar=cevap_display,
        k_anahtar=anon_key.k_hex,
        gateway_base=config.PINATA_GATEWAY_BASE.rstrip("/"),
    )


@app.route("/status/<int:record_id>", methods=["GET"])
def status(record_id: int):
    record = offline_queue.get_record_by_id(config, record_id)
    if record is None:
        return jsonify({"hata": "Kayıt bulunamadı."}), 404

    payload = {
        "id": record.id,
        "durum": record.status,
        "ipfs_cid": record.ipfs_cid,
        "tx_hash": record.tx_hash,
        "retry_count": record.retry_count,
        "hata_mesaji": record.error_message,
        "olusturulma": record.created_at,
        "guncelleme": record.updated_at,
    }
    if record.status == offline_queue.STATUS_SYNCED and record.tx_hash:
        payload["polygonscan_url"] = blockchain_service.get_polygonscan_tx_url(
            config, record.tx_hash
        )
    return jsonify(payload)


@app.route("/verify", methods=["GET", "POST"])
@requires_verify_auth
def verify():
    """
    Yetkili kurumun, elindeki TC/isim/tarih bilgisiyle k anahtarını
    yeniden hesaplayıp zincirdeki kaydı sorgulamasını sağlayan ekran
    (rapor 1. Bölüm: "yetkili kurum kaydı her zaman teyit edebilir").

    GÜVENLİK NOTU: HTTP Basic Auth ile korunur (bkz. requires_verify_auth).
    INSTITUTION_VERIFY_USERNAME / INSTITUTION_VERIFY_PASSWORD_HASH .env'de
    tanımlı değilse ekran açık kalır ve bu durum verify.html'de görünür bir
    uyarı olarak gösterilir; üretimde bu ikisinin tanımlanması ZORUNLUDUR.
    """
    if request.method == "GET":
        return render_template(
            "verify.html", result=None, error=None, auth_enabled=config.verify_auth_enabled
        )

    tc = (request.form.get("tc_kimlik") or "").strip()
    isim = (request.form.get("isim") or "").strip()
    tarih = (request.form.get("sinav_tarihi") or "").strip()

    try:
        config.validate_for_anonymization()
        anon_key = crypto_utils.generate_anon_key(
            tc_kimlik=tc, isim=isim, tarih=tarih, kurum_tuzu=config.KURUM_TUZU
        )
    except (crypto_utils.InvalidTCKimlikError, ValueError) as exc:
        return render_template(
            "verify.html", result=None, error=str(exc), auth_enabled=config.verify_auth_enabled
        )

    # Yeni (Merkle) doğrulama akışı: zincir artık bireysel kaydı tutmadığından,
    # kurum önce YEREL kuyruktan k anahtarına ait kaydı (merkle_root + proof +
    # kendi ipfs_cid'i) bulur; ardından zincirden parti bilgisini çeker ve
    # kaydın o köke dahil olduğunu ZİNCİR ÜZERİNDE (verifyInclusion)
    # kriptografik olarak kanıtlar.
    record = offline_queue.get_record_by_k_anahtar(config, anon_key.k_hex)
    if record is None:
        return render_template(
            "verify.html",
            result=None,
            error="Bu bilgilere karşılık gelen bir kayıt yerel sistemde bulunamadı.",
            auth_enabled=config.verify_auth_enabled,
        )

    if record.status != offline_queue.STATUS_SYNCED or not record.merkle_root:
        return render_template(
            "verify.html",
            result=None,
            error=(
                "Kayıt mevcut ancak henüz blokzincire mühürlenmemiş "
                f"(durum: {record.status}). Senkronizasyon tamamlandığında "
                "doğrulama yapılabilir."
            ),
            auth_enabled=config.verify_auth_enabled,
        )

    try:
        merkle_root_bytes = merkle.hex_to_bytes32(record.merkle_root)
        leaf_bytes = merkle.hex_to_bytes32(record.local_hash)
        proof_hex = record.merkle_proof or []
        proof_bytes = [merkle.hex_to_bytes32(p) for p in proof_hex]

        onchain_batch = blockchain_service.get_batch_from_chain(config, merkle_root_bytes)
        included = blockchain_service.verify_inclusion_on_chain(
            config, merkle_root_bytes, leaf_bytes, proof_bytes
        )

        result = {
            "k_anahtar": anon_key.k_hex,
            "merkle_root": record.merkle_root,
            "inclusion_verified": included,
            "timestamp": onchain_batch.timestamp,
            "verifier_address": onchain_batch.verifier_address,
            "record_count": onchain_batch.record_count,
            "tx_hash": record.tx_hash,
            "polygonscan_url": (
                blockchain_service.get_polygonscan_tx_url(config, record.tx_hash)
                if record.tx_hash
                else None
            ),
            "ipfs_cid": record.ipfs_cid,
            "gateway_url": (
                ipfs_service.build_gateway_url(config, record.ipfs_cid)
                if record.ipfs_cid
                else None
            ),
        }
        return render_template(
            "verify.html", result=result, error=None, auth_enabled=config.verify_auth_enabled
        )
    except blockchain_service.BatchNotFoundError:
        return render_template(
            "verify.html",
            result=None,
            error="Kayda ait parti (Merkle kökü) zincirde bulunamadı.",
            auth_enabled=config.verify_auth_enabled,
        )
    except blockchain_service.BlockchainServiceError as exc:
        return render_template(
            "verify.html",
            result=None,
            error=f"Blokzincir sorgusu başarısız: {exc}",
            auth_enabled=config.verify_auth_enabled,
        )
    except (ValueError, RuntimeError, merkle.MerkleError) as exc:
        return render_template(
            "verify.html", result=None, error=str(exc), auth_enabled=config.verify_auth_enabled
        )


# ============================================================================
# HATA İŞLEYİCİLER
# ============================================================================

@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(_exc):
    return _render_error(
        "Dosya Çok Büyük",
        f"Yüklenen dosya {config.MAX_UPLOAD_SIZE_MB} MB sınırını aşıyor.",
        status_code=413,
    )


@app.errorhandler(404)
def handle_not_found(_exc):
    return _render_error("Sayfa Bulunamadı", "İstenen sayfa mevcut değil.", status_code=404)


@app.errorhandler(500)
def handle_internal_error(_exc):
    return _render_error(
        "Sunucu Hatası",
        "Beklenmeyen bir hata oluştu. Lütfen daha sonra yeniden deneyin.",
        status_code=500,
    )


if __name__ == "__main__":
    debug_mode = config.FLASK_ENV == "development"
    app.run(host=config.HOST, port=config.PORT, debug=debug_mode)
