# -*- coding: utf-8 -*-
"""
deploy/gunicorn.conf.py
=========================
Gunicorn üretim yapılandırması. Çalıştırma:

    cd uods_project
    gunicorn -c deploy/gunicorn.conf.py wsgi:app

Tasarım gerekçesi:
- Worker sayısı CPU çekirdek sayısına göre ayarlanır (OpenCV/OMR işlemi
  CPU-bağımlıdır; (2 x çekirdek + 1) formülü gunicorn'un genel önerisidir).
- worker_class "sync" bırakılmıştır: her istek zaten kısa sürede biter
  (OMR + SQLite yazma saniyeler içinde tamamlanır) ve eşzamanlılık ihtiyacı
  worker SAYISIYLA karşılanır; async worker (gevent/eventlet) burada ek
  bir fayda sağlamaz ve OpenCV'nin C uzantılarıyla bazı uyumluluk riskleri
  taşır.
- timeout, blokzincir işleminin (IPFS yükleme + Polygon onayı) normalde
  saniyeler içinde bitmesine göre 120s'ye ayarlanmıştır; sync senkron olarak
  beklenirse (run_once() upload() içinde çağrılıyor) bu süre yeterlidir.
- preload_app=False bırakılmıştır: her worker kendi SyncWorker arka plan
  thread'ini ayrı ayrı başlatır. Bu KASITLIDIR — preload_app=True ile fork
  öncesi tek bir SyncWorker başlatılırsa, fork sonrası thread'ler çocuk
  süreçlere TAŞINMAZ ve senkronizasyon sessizce durur. Bu nedenle "sync"
  worker_class + preload_app=False kombinasyonu, her worker'ın kendi
  SyncWorker'ını çalıştırmasını garanti eder (offline_queue.py'nin SQLite
  UNIQUE kısıtı, birden fazla worker'ın aynı kaydı iki kez işlemesini zaten
  engeller).
"""
import multiprocessing

bind = "127.0.0.1:8000"  # nginx bu adrese ters vekillik (reverse proxy) yapar
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "sync"
timeout = 120
graceful_timeout = 30
keepalive = 5

accesslog = "-"   # stdout (systemd/journalctl yakalar)
errorlog = "-"    # stderr
loglevel = "info"

preload_app = False
