# -*- coding: utf-8 -*-
"""
offline_queue.py
=================
SQLite tabanlı çevrimdışı tampon katmanı (rapor bölüm 3.6). İnternet
bağlantısı olmadığında işlenmiş optik form verilerini PENDING durumunda
yerel olarak saklar; bağlantı yeniden sağlandığında bu kayıtları otomatik
olarak IPFS'e ve ardından Polygon ağına aktaran bir senkronizasyon
servisi (SyncWorker) içerir (Tablo 5'teki durum makinesi).

ÖNEMLİ (KVKK): Bu modülde TC kimlik numarası ve isim bilgisi HİÇBİR
ZAMAN saklanmaz; yalnızca anonimleştirme adımında türetilmiş k anahtarı,
cevap dizisi ve görüntü/h_local bütünlük özeti tutulur (Denklem 9).
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Union

from config import Config

# ============================================================================
# DURUM SABİTLERİ (Tablo 5)
# ============================================================================

STATUS_PENDING = "PENDING"    # OMR tamamlandı, yerel kaydedildi, internet bekleniyor
STATUS_SYNCING = "SYNCING"    # Bağlantı tespit edildi, IPFS/blokzincir işlemi sürüyor
STATUS_SYNCED = "SYNCED"      # TX Hash alındı, blokzincire başarıyla mühürlendi
STATUS_FAILED = "FAILED"      # Hata oluştu, yeniden deneme kuyruğunda

_ALL_STATUSES = (STATUS_PENDING, STATUS_SYNCING, STATUS_SYNCED, STATUS_FAILED)


class OfflineQueueError(Exception):
    """Çevrimdışı kuyruk modülündeki hatalar için temel istisna sınıfı."""


@dataclass
class QueueRecord:
    id: int
    k_anahtar_hex: str
    cevap_dizisi_json: str
    image_path: str
    local_hash: str
    ipfs_cid: Optional[str]
    tx_hash: Optional[str]
    status: str
    created_at: str
    updated_at: str
    retry_count: int
    error_message: Optional[str]
    merkle_root: Optional[str] = None
    merkle_proof_json: Optional[str] = None

    @property
    def cevap_dizisi(self) -> list:
        return json.loads(self.cevap_dizisi_json)

    @property
    def merkle_proof(self) -> Optional[list]:
        """Kanıt (sibling hash) listesini hex string listesi olarak döndürür."""
        if not self.merkle_proof_json:
            return None
        return json.loads(self.merkle_proof_json)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(config: Config):
    """
    Her çağrı için kısa ömürlü bir SQLite bağlantısı açar. Bu desen,
    Flask'ın istek thread'leri ile arka plandaki SyncWorker thread'inin
    aynı bağlantıyı paylaşmasından kaynaklanabilecek kilitlenme (locking)
    sorunlarını önler. WAL (Write-Ahead Log) modu, okuma/yazma
    işlemlerinin birbirini büyük ölçüde engellemeden eş zamanlı
    çalışabilmesini sağlar [20, 21, 22].
    """
    db_path = config.sqlite_full_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def init_db(config: Config) -> None:
    """Kuyruk tablosunu (yoksa) oluşturur. Uygulama başlangıcında bir kez çağrılmalıdır."""
    with _connect(config) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS offline_queue (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                k_anahtar           TEXT    NOT NULL,
                cevap_dizisi_json   TEXT    NOT NULL,
                image_path          TEXT    NOT NULL,
                local_hash          TEXT    NOT NULL,
                ipfs_cid            TEXT,
                tx_hash             TEXT,
                merkle_root         TEXT,
                merkle_proof_json   TEXT,
                status              TEXT    NOT NULL DEFAULT 'PENDING',
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL,
                retry_count         INTEGER NOT NULL DEFAULT 0,
                error_message       TEXT
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_offline_queue_status ON offline_queue(status);"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_offline_queue_k_anahtar ON offline_queue(k_anahtar);"
        )

        # Hafif şema göçü (migration): önceki sürümden kalma veritabanlarında
        # merkle_root / merkle_proof_json sütunları yoksa eklenir. SQLite'ta
        # "ADD COLUMN IF NOT EXISTS" olmadığından mevcut sütunlar kontrol edilir.
        existing_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(offline_queue);").fetchall()
        }
        if "merkle_root" not in existing_cols:
            conn.execute("ALTER TABLE offline_queue ADD COLUMN merkle_root TEXT;")
        if "merkle_proof_json" not in existing_cols:
            conn.execute("ALTER TABLE offline_queue ADD COLUMN merkle_proof_json TEXT;")


def enqueue_record(
    config: Config,
    k_anahtar_hex: str,
    cevaplar: list,
    image_path: Union[str, Path],
    local_hash: str,
) -> int:
    """
    Görüntü işleme ve OMR tamamlandıktan sonra elde edilen veriyi
    PENDING durumuyla yerel veritabanına yazar (rapor 3.6.1, Denklem 9).
    Aynı k_anahtar ile zaten bir kayıt varsa OfflineQueueError fırlatılır
    (mükerrer kayıt / aynı formun iki kez işlenmesini engellemek için).

    Döndürülen değer, kaydın yerel veritabanı id'sidir (durum sorgulama
    / Flask /status/<id> uç noktası için kullanılır).
    """
    now = _utc_now_iso()
    with _connect(config) as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO offline_queue
                    (k_anahtar, cevap_dizisi_json, image_path, local_hash,
                     status, created_at, updated_at, retry_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    k_anahtar_hex,
                    json.dumps(cevaplar, ensure_ascii=False),
                    str(image_path),
                    local_hash,
                    STATUS_PENDING,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise OfflineQueueError(
                f"Bu k anahtarı için zaten bir kayıt mevcut: {k_anahtar_hex}"
            ) from exc
        return int(cur.lastrowid)


def _row_to_record(row: sqlite3.Row) -> QueueRecord:
    return QueueRecord(
        id=row["id"],
        k_anahtar_hex=row["k_anahtar"],
        cevap_dizisi_json=row["cevap_dizisi_json"],
        image_path=row["image_path"],
        local_hash=row["local_hash"],
        ipfs_cid=row["ipfs_cid"],
        tx_hash=row["tx_hash"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        retry_count=row["retry_count"],
        error_message=row["error_message"],
        merkle_root=row["merkle_root"] if "merkle_root" in row.keys() else None,
        merkle_proof_json=(
            row["merkle_proof_json"] if "merkle_proof_json" in row.keys() else None
        ),
    )


def get_record_by_id(config: Config, record_id: int) -> Optional[QueueRecord]:
    with _connect(config) as conn:
        row = conn.execute(
            "SELECT * FROM offline_queue WHERE id = ?;", (record_id,)
        ).fetchone()
        return _row_to_record(row) if row else None


def get_record_by_k_anahtar(config: Config, k_anahtar_hex: str) -> Optional[QueueRecord]:
    with _connect(config) as conn:
        row = conn.execute(
            "SELECT * FROM offline_queue WHERE k_anahtar = ?;", (k_anahtar_hex,)
        ).fetchone()
        return _row_to_record(row) if row else None


def get_records_by_status(
    config: Config, status: str, limit: Optional[int] = None
) -> List[QueueRecord]:
    if status not in _ALL_STATUSES:
        raise OfflineQueueError(f"Geçersiz durum: {status!r}")
    query = "SELECT * FROM offline_queue WHERE status = ? ORDER BY created_at ASC"
    params: tuple = (status,)
    if limit is not None:
        query += " LIMIT ?"
        params = (status, limit)
    with _connect(config) as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_record(r) for r in rows]


def update_status(
    config: Config,
    record_id: int,
    new_status: str,
    ipfs_cid: Optional[str] = None,
    tx_hash: Optional[str] = None,
    error_message: Optional[str] = None,
    increment_retry: bool = False,
    merkle_root: Optional[str] = None,
    merkle_proof_json: Optional[str] = None,
) -> None:
    if new_status not in _ALL_STATUSES:
        raise OfflineQueueError(f"Geçersiz durum: {new_status!r}")

    fields = ["status = ?", "updated_at = ?"]
    params: list = [new_status, _utc_now_iso()]

    if ipfs_cid is not None:
        fields.append("ipfs_cid = ?")
        params.append(ipfs_cid)
    if tx_hash is not None:
        fields.append("tx_hash = ?")
        params.append(tx_hash)
    if merkle_root is not None:
        fields.append("merkle_root = ?")
        params.append(merkle_root)
    if merkle_proof_json is not None:
        fields.append("merkle_proof_json = ?")
        params.append(merkle_proof_json)
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message)
    if increment_retry:
        fields.append("retry_count = retry_count + 1")

    params.append(record_id)
    with _connect(config) as conn:
        conn.execute(
            f"UPDATE offline_queue SET {', '.join(fields)} WHERE id = ?;", params
        )


def queue_stats(config: Config) -> dict:
    """Her durumdaki kayıt sayısını döndürür (izleme / Flask /status için)."""
    with _connect(config) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM offline_queue GROUP BY status;"
        ).fetchall()
    stats = {s: 0 for s in _ALL_STATUSES}
    for row in rows:
        stats[row["status"]] = row["cnt"]
    return stats


# ============================================================================
# İNTERNET BAĞLANTISI TESPİTİ
# ============================================================================

def is_internet_available(
    host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0
) -> bool:
    """
    Hafif bir TCP el sıkışması (Google Public DNS, port 53) ile internet
    bağlantısının var olup olmadığını kontrol eder. Bu yöntem, herhangi
    bir özel servise (Pinata/Alchemy) bağımlı olmadığından, o servislerin
    kendisi geçici olarak çöktüğünde bile genel ağ durumunu doğru
    yansıtır (rapor 3.6.2 — "periyodik olarak internet bağlantısını
    kontrol eden senkronizasyon servisi").
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ============================================================================
# SENKRONİZASYON SERVİSİ (SyncWorker)
# ============================================================================

@dataclass
class SyncResult:
    processed: int = 0
    synced: int = 0
    failed: int = 0


@dataclass
class BatchSyncResult:
    """sync_batch_fn'in döndürmesi gereken sonuç: partinin Merkle kökü, paylaşılan
    TX hash'i ve her kaydın (id -> (ipfs_cid, merkle_proof_json)) eşlemesi."""

    merkle_root_hex: str
    tx_hash: str
    # record.id -> (ipfs_cid, merkle_proof_json)
    per_record: dict


class SyncWorker:
    """
    Arka planda periyodik olarak çalışan, PENDING/FAILED durumundaki
    kayıtları internet bağlantısı algılandığında otomatik olarak IPFS'e
    yükleyen ve Polygon ağına TOPLU (batch) olarak mühürleyen
    senkronizasyon servisi (rapor 3.6.2 + hakem değerlendirmesine yanıt:
    Merkle toplu mühürleme).

    sync_batch_fn: O turda işlenecek TÜM eligible kayıtların listesini alıp
                   tek bir BatchSyncResult döndüren fonksiyon. Bu fonksiyon
                   içinde: (a) her kaydın görüntüsü ayrı ayrı IPFS'e yüklenir
                   (kayıt başına, değişmedi), (b) tüm kayıtların local_hash
                   (h_local) değerlerinden tek bir Merkle ağacı kurulur,
                   (c) ağacın kökü zincire TEK BİR işlemle (addBatchRoot)
                   yazılır, (d) her kayıt için ayrı bir Merkle kanıtı üretilir.
                   app.py içinde tanımlanır ve buraya enjekte edilir.

                   ATOMIKLIK: bütün parti tek bir on-chain işleme bağlı
                   olduğundan, sync_batch_fn ya tümüyle başarılı olur ve
                   tüm kayıtlar SYNCED işaretlenir, ya da bir istisna
                   fırlatır ve partideki TÜM kayıtlar FAILED'a döner
                   (hepsi-ya da-hiçbiri).
    """

    def __init__(
        self,
        config: Config,
        sync_batch_fn: Callable[[List[QueueRecord]], "BatchSyncResult"],
        interval_seconds: Optional[int] = None,
        max_retry: Optional[int] = None,
    ) -> None:
        self.config = config
        self.sync_batch_fn = sync_batch_fn
        self.interval_seconds = interval_seconds or config.SYNC_INTERVAL_SECONDS
        self.max_retry = max_retry or config.SYNC_MAX_RETRY
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.last_run_result: Optional[SyncResult] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # Zaten çalışıyor.
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="UODS-SyncWorker", daemon=True
        )
        self._thread.start()

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if is_internet_available():
                    self.run_once()
            except Exception:  # noqa: BLE001 - arka plan thread'i asla çökmemeli
                traceback.print_exc()
            self._stop_event.wait(self.interval_seconds)

    def run_once(self) -> SyncResult:
        """
        Bekleyen kayıtları TEK BİR PARTİ olarak işler. Hem PENDING hem de
        yeniden deneme limiti aşılmamış FAILED kayıtlar bu partiye dahil
        edilir. Tüm parti, tek bir on-chain işlemle (addBatchRoot)
        mühürlenir; bu nedenle başarı/başarısızlık parti genelinde
        atomiktir (hepsi-ya da-hiçbiri).
        """
        if not self._lock.acquire(blocking=False):
            # Önceki tur hâlâ sürüyor; bu turu atla.
            return SyncResult()

        try:
            result = SyncResult()
            candidates = get_records_by_status(self.config, STATUS_PENDING)
            candidates += [
                r
                for r in get_records_by_status(self.config, STATUS_FAILED)
                if r.retry_count < self.max_retry
            ]

            if not candidates:
                self.last_run_result = result
                return result

            result.processed = len(candidates)

            # Tüm parti SYNCING durumuna alınır.
            for record in candidates:
                update_status(self.config, record.id, STATUS_SYNCING)

            try:
                batch_result = self.sync_batch_fn(candidates)
            except Exception as exc:  # noqa: BLE001
                # Parti başarısız: TÜM kayıtlar FAILED'a döner, retry artırılır.
                for record in candidates:
                    update_status(
                        self.config,
                        record.id,
                        STATUS_FAILED,
                        error_message=str(exc),
                        increment_retry=True,
                    )
                result.failed = len(candidates)
                self.last_run_result = result
                return result

            # Parti başarılı: her kayıt SYNCED, paylaşılan tx_hash + ortak
            # merkle_root + kendi bireysel kanıtı (proof) ile güncellenir.
            for record in candidates:
                ipfs_cid, proof_json = batch_result.per_record.get(
                    record.id, (None, None)
                )
                update_status(
                    self.config,
                    record.id,
                    STATUS_SYNCED,
                    ipfs_cid=ipfs_cid,
                    tx_hash=batch_result.tx_hash,
                    merkle_root=batch_result.merkle_root_hex,
                    merkle_proof_json=proof_json,
                )
                result.synced += 1

            self.last_run_result = result
            return result
        finally:
            self._lock.release()
