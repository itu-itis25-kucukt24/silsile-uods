# -*- coding: utf-8 -*-
"""
tests/test_offline_queue.py
=============================
offline_queue.py modülü için birim testleri: PENDING/SYNCING/SYNCED/FAILED
durum makinesi, mükerrer k anahtarı reddi, kuyruk istatistikleri ve
SyncWorker.run_once() davranışı (enjekte edilmiş sahte bir senkronizasyon
fonksiyonuyla, gerçek ağ bağlantısı GEREKMEDEN test edilir).

Her test, geçici bir SQLite dosyası kullanır ve test sonunda siler;
gerçek instance/uods.db dosyasına ASLA dokunulmaz.

Çalıştırma: pytest tests/test_offline_queue.py -v
"""

import dataclasses
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import offline_queue
from config import config as base_config


@pytest.fixture
def queue_config(tmp_path):
    """Gerçek config'in bir kopyası, ama SQLITE_DB_PATH geçici bir test
    dosyasına yönlendirilmiş hâli (gerçek veritabanına asla dokunulmaz)."""
    test_db = tmp_path / "test_queue.db"
    cfg = dataclasses.replace(base_config, SQLITE_DB_PATH=str(test_db))
    offline_queue.init_db(cfg)
    yield cfg
    # tmp_path pytest tarafından otomatik temizlenir; ekstra işlem gerekmez.


def _enqueue_sample(cfg, k="k-test-0001"):
    return offline_queue.enqueue_record(
        cfg,
        k_anahtar_hex=k,
        cevaplar=["A", "B", None],
        image_path="/tmp/fake_image.png",
        local_hash="fake-local-hash",
    )


class TestEnqueueAndRetrieve:
    def test_enqueue_returns_positive_id(self, queue_config):
        record_id = _enqueue_sample(queue_config)
        assert record_id > 0

    def test_new_record_starts_as_pending(self, queue_config):
        record_id = _enqueue_sample(queue_config)
        record = offline_queue.get_record_by_id(queue_config, record_id)
        assert record.status == offline_queue.STATUS_PENDING
        assert record.retry_count == 0
        assert record.ipfs_cid is None
        assert record.tx_hash is None

    def test_duplicate_k_anahtar_raises(self, queue_config):
        _enqueue_sample(queue_config, k="k-dup")
        with pytest.raises(offline_queue.OfflineQueueError):
            _enqueue_sample(queue_config, k="k-dup")

    def test_get_by_k_anahtar(self, queue_config):
        record_id = _enqueue_sample(queue_config, k="k-lookup")
        record = offline_queue.get_record_by_k_anahtar(queue_config, "k-lookup")
        assert record.id == record_id

    def test_get_nonexistent_id_returns_none(self, queue_config):
        assert offline_queue.get_record_by_id(queue_config, 99999) is None

    def test_cevap_dizisi_property_roundtrips_through_json(self, queue_config):
        record_id = _enqueue_sample(queue_config)
        record = offline_queue.get_record_by_id(queue_config, record_id)
        assert record.cevap_dizisi == ["A", "B", None]


class TestUpdateStatus:
    def test_transition_to_synced_sets_cid_and_tx(self, queue_config):
        record_id = _enqueue_sample(queue_config)
        offline_queue.update_status(
            queue_config, record_id, offline_queue.STATUS_SYNCED,
            ipfs_cid="bafy123", tx_hash="0xabc",
        )
        record = offline_queue.get_record_by_id(queue_config, record_id)
        assert record.status == offline_queue.STATUS_SYNCED
        assert record.ipfs_cid == "bafy123"
        assert record.tx_hash == "0xabc"

    def test_transition_to_failed_increments_retry_and_stores_message(self, queue_config):
        record_id = _enqueue_sample(queue_config)
        offline_queue.update_status(
            queue_config, record_id, offline_queue.STATUS_FAILED,
            error_message="ağ hatası", increment_retry=True,
        )
        record = offline_queue.get_record_by_id(queue_config, record_id)
        assert record.status == offline_queue.STATUS_FAILED
        assert record.retry_count == 1
        assert record.error_message == "ağ hatası"


class TestQueueStats:
    def test_counts_each_status_correctly(self, queue_config):
        id_a = _enqueue_sample(queue_config, k="k-a")
        _enqueue_sample(queue_config, k="k-b")
        _enqueue_sample(queue_config, k="k-c")
        offline_queue.update_status(queue_config, id_a, offline_queue.STATUS_SYNCED, ipfs_cid="x", tx_hash="y")

        stats = offline_queue.queue_stats(queue_config)
        assert stats[offline_queue.STATUS_PENDING] == 2
        assert stats[offline_queue.STATUS_SYNCED] == 1
        assert stats[offline_queue.STATUS_FAILED] == 0

    def test_empty_queue_returns_zero_for_all_statuses(self, queue_config):
        stats = offline_queue.queue_stats(queue_config)
        assert all(v == 0 for v in stats.values())


class TestSyncWorkerRunOnce:
    def _make_batch_fn(self, per_record_overrides=None, fail_with=None):
        """Sahte bir sync_batch_fn üretir. fail_with verilirse istisna fırlatır;
        aksi halde verilen kayıtlardan bir BatchSyncResult döndürür."""

        def batch_fn(records):
            if fail_with is not None:
                raise fail_with
            per_record = {}
            for r in records:
                cid = f"bafy-{r.id}"
                proof_json = '["0x' + "11" * 32 + '"]'
                per_record[r.id] = (cid, proof_json)
            if per_record_overrides:
                per_record.update(per_record_overrides)
            return offline_queue.BatchSyncResult(
                merkle_root_hex="0x" + "ab" * 32,
                tx_hash="0xBATCHTX",
                per_record=per_record,
            )

        return batch_fn

    def test_successful_sync_marks_record_synced(self, queue_config):
        record_id = _enqueue_sample(queue_config, k="k-sync-ok")

        worker = offline_queue.SyncWorker(queue_config, self._make_batch_fn())
        result = worker.run_once()

        assert result.processed == 1
        assert result.synced == 1
        assert result.failed == 0

        record = offline_queue.get_record_by_id(queue_config, record_id)
        assert record.status == offline_queue.STATUS_SYNCED
        assert record.ipfs_cid == f"bafy-{record_id}"
        assert record.tx_hash == "0xBATCHTX"
        assert record.merkle_root == "0x" + "ab" * 32
        assert record.merkle_proof is not None

    def test_failed_batch_marks_all_failed_and_increments_retry(self, queue_config):
        record_id = _enqueue_sample(queue_config, k="k-sync-fail")

        worker = offline_queue.SyncWorker(
            queue_config, self._make_batch_fn(fail_with=RuntimeError("simüle edilmiş ağ hatası"))
        )
        result = worker.run_once()

        assert result.processed == 1
        assert result.synced == 0
        assert result.failed == 1

        record = offline_queue.get_record_by_id(queue_config, record_id)
        assert record.status == offline_queue.STATUS_FAILED
        assert record.retry_count == 1
        assert "simüle edilmiş ağ hatası" in record.error_message

    def test_whole_batch_fails_atomically(self, queue_config):
        """Parti atomiktir: bir tur içinde herhangi bir hata olursa, o turdaki
        TÜM kayıtlar (sadece biri değil) FAILED'a döner."""
        id1 = _enqueue_sample(queue_config, k="k-atomic-1")
        id2 = _enqueue_sample(queue_config, k="k-atomic-2")

        worker = offline_queue.SyncWorker(
            queue_config, self._make_batch_fn(fail_with=RuntimeError("parti hatası"))
        )
        result = worker.run_once()

        assert result.processed == 2
        assert result.failed == 2
        for rid in (id1, id2):
            rec = offline_queue.get_record_by_id(queue_config, rid)
            assert rec.status == offline_queue.STATUS_FAILED
            assert rec.retry_count == 1

    def test_batch_processes_multiple_records_together(self, queue_config):
        id1 = _enqueue_sample(queue_config, k="k-multi-1")
        id2 = _enqueue_sample(queue_config, k="k-multi-2")

        worker = offline_queue.SyncWorker(queue_config, self._make_batch_fn())
        result = worker.run_once()

        assert result.processed == 2
        assert result.synced == 2

        for rid in (id1, id2):
            rec = offline_queue.get_record_by_id(queue_config, rid)
            assert rec.status == offline_queue.STATUS_SYNCED
            # Aynı parti -> aynı tx_hash ve aynı merkle_root paylaşılır
            assert rec.tx_hash == "0xBATCHTX"
            assert rec.merkle_root == "0x" + "ab" * 32

    def test_run_once_does_not_reprocess_already_synced_records(self, queue_config):
        record_id = _enqueue_sample(queue_config, k="k-already-synced")
        seen = []

        def counting_batch_fn(records):
            seen.append([r.id for r in records])
            return offline_queue.BatchSyncResult(
                merkle_root_hex="0x" + "cd" * 32,
                tx_hash="0xX",
                per_record={r.id: (f"bafy-{r.id}", "[]") for r in records},
            )

        worker = offline_queue.SyncWorker(queue_config, counting_batch_fn)
        worker.run_once()
        worker.run_once()  # ikinci çağrı artık SYNCED kaydı yeniden işlememeli

        # İlk turda 1 kayıt görüldü; ikinci turda işlenecek kayıt kalmadığından
        # batch_fn ya hiç çağrılmadı ya da boş listeyle çağrılmadı.
        assert seen == [[record_id]]


class TestSyncWorkerBackgroundThread:
    def test_start_and_stop_runs_loop_and_syncs_pending_record(self, queue_config, monkeypatch):
        record_id = _enqueue_sample(queue_config, k="k-background")
        monkeypatch.setattr(offline_queue, "is_internet_available", lambda *a, **kw: True)

        def fake_batch_fn(records):
            return offline_queue.BatchSyncResult(
                merkle_root_hex="0x" + "ef" * 32,
                tx_hash="0xBG",
                per_record={r.id: (f"bafy-{r.id}", "[]") for r in records},
            )

        worker = offline_queue.SyncWorker(queue_config, fake_batch_fn, interval_seconds=1)
        worker.start()
        try:
            deadline = time.time() + 5
            record = offline_queue.get_record_by_id(queue_config, record_id)
            while record.status != offline_queue.STATUS_SYNCED and time.time() < deadline:
                time.sleep(0.2)
                record = offline_queue.get_record_by_id(queue_config, record_id)
        finally:
            worker.stop()

        assert record.status == offline_queue.STATUS_SYNCED
        assert record.tx_hash == "0xBG"
