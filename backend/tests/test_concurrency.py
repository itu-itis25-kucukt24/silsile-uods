# -*- coding: utf-8 -*-
"""
tests/test_concurrency.py
============================
offline_queue.py'nin SQLite katmanını GERÇEK eşzamanlılık altında test eder.

Önemli tasarım notu: gunicorn (bkz. deploy/gunicorn.conf.py) birden fazla
ayrı OS SÜRECİYLE (worker process) çalışır, thread değil. Bu yüzden bu test
dosyası bilerek `multiprocessing` kullanır — aynı SQLite dosyasına çok
sayıda gerçek ayrı Python sürecinden eşzamanlı yazma yaparak, gunicorn'un
üretimdeki gerçek davranışını taklit eder. Bir tek process içinde thread
kullanmak bu riski (WAL modunun çoklu OS SÜRECİ arasında dosya kilitleme
davranışı) yeterince test etmezdi.

Çalıştırma: pytest tests/test_concurrency.py -v -s
(-s bayrağı, süre/sonuç özetinin konsola basılmasını sağlar)
"""
import dataclasses
import multiprocessing
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import offline_queue
from config import config as base_config


def _worker_enqueue_unique(db_path: str, worker_id: int, count: int, result_queue) -> None:
    """Her worker kendi BENZERSİZ k_anahtar'larıyla `count` kayıt ekler."""
    cfg = dataclasses.replace(base_config, SQLITE_DB_PATH=db_path)
    ok = 0
    errors = []
    for i in range(count):
        try:
            offline_queue.enqueue_record(
                cfg,
                k_anahtar_hex=f"worker{worker_id}-kayit{i}",
                cevaplar=["A", "B", None],
                image_path=f"/tmp/fake_{worker_id}_{i}.png",
                local_hash="hash",
            )
            ok += 1
        except Exception as exc:  # pragma: no cover - sadece teşhis için
            errors.append(repr(exc))
    result_queue.put((worker_id, ok, errors))


def _worker_enqueue_same_key(db_path: str, worker_id: int, shared_key: str, result_queue) -> None:
    """Tüm worker'lar AYNI k_anahtar ile kayıt eklemeye çalışır — yarış testi.
    Tam olarak BİR worker başarılı olmalı, diğerleri OfflineQueueError almalı."""
    cfg = dataclasses.replace(base_config, SQLITE_DB_PATH=db_path)
    try:
        offline_queue.enqueue_record(
            cfg,
            k_anahtar_hex=shared_key,
            cevaplar=["A"],
            image_path=f"/tmp/race_{worker_id}.png",
            local_hash="hash",
        )
        result_queue.put((worker_id, "basarili"))
    except offline_queue.OfflineQueueError:
        result_queue.put((worker_id, "reddedildi"))
    except Exception as exc:  # pragma: no cover
        result_queue.put((worker_id, f"beklenmeyen_hata:{exc!r}"))


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "concurrency_test.db")
    cfg = dataclasses.replace(base_config, SQLITE_DB_PATH=p)
    offline_queue.init_db(cfg)
    return p


class TestMultiProcessConcurrency:
    def test_many_processes_enqueue_unique_records_without_loss(self, db_path):
        """8 ayrı OS sürecinin her biri 15 benzersiz kayıt eklerse, toplamda
        tam 120 kayıt veritabanında olmalı; hiçbiri kaybolmamalı ya da
        yarış koşulu nedeniyle bozulmamalı."""
        n_workers = 8
        per_worker = 15
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()
        procs = [
            ctx.Process(
                target=_worker_enqueue_unique,
                args=(db_path, w, per_worker, result_queue),
            )
            for w in range(n_workers)
        ]

        start = time.time()
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
        elapsed = time.time() - start

        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        total_ok = sum(r[1] for r in results)
        all_errors = [e for r in results for e in r[2]]

        print(f"\n[concurrency] {n_workers} süreç x {per_worker} kayıt, "
              f"{elapsed:.2f}s içinde tamamlandı, başarılı={total_ok}, hata={len(all_errors)}")

        assert not all_errors, f"Beklenmeyen hatalar oluştu: {all_errors[:5]}"
        assert total_ok == n_workers * per_worker
        for p in procs:
            assert p.exitcode == 0

        cfg = dataclasses.replace(base_config, SQLITE_DB_PATH=db_path)
        stats = offline_queue.queue_stats(cfg)
        assert stats[offline_queue.STATUS_PENDING] == n_workers * per_worker

    def test_concurrent_writers_racing_on_same_key_exactly_one_wins(self, db_path):
        """5 ayrı sürecin tamamı AYNI ANDA aynı k_anahtar ile kayıt eklemeye
        çalışırsa, UNIQUE kısıtı sayesinde tam olarak biri başarılı olmalı,
        diğer dördü düzgün biçimde OfflineQueueError ile reddedilmeli —
        veritabanında asla iki mükerrer kayıt veya bozuk bir durum oluşmamalı."""
        n_workers = 5
        shared_key = "yaris-testi-ortak-anahtar"
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()
        procs = [
            ctx.Process(
                target=_worker_enqueue_same_key,
                args=(db_path, w, shared_key, result_queue),
            )
            for w in range(n_workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        outcomes = [r[1] for r in results]
        print(f"\n[concurrency] yarış testi sonuçları: {outcomes}")

        assert outcomes.count("basarili") == 1, f"Tam olarak 1 kazanan bekleniyordu: {outcomes}"
        assert outcomes.count("reddedildi") == n_workers - 1
        assert all(o in ("basarili", "reddedildi") for o in outcomes), (
            f"Beklenmeyen sonuç türü: {outcomes}"
        )

        cfg = dataclasses.replace(base_config, SQLITE_DB_PATH=db_path)
        record = offline_queue.get_record_by_k_anahtar(cfg, shared_key)
        assert record is not None
        stats = offline_queue.queue_stats(cfg)
        assert stats[offline_queue.STATUS_PENDING] == 1, "Veritabanında tam olarak 1 kayıt olmalı"
