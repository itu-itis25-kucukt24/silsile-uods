# -*- coding: utf-8 -*-
"""
tests/test_app.py
===================
Flask uygulamasının (app.py) rotaları için entegrasyon testleri.
Gerçek Pinata/Polygon ağ erişimi GEREKTİRMEZ: kurumsal kimlik bilgileri
tanımlı olmadığında senkronizasyon adımının FAILED durumuna düşüp
sayfanın yine de doğru biçimde (pending.html) render edilmesi beklenen
ve doğru davranıştır.

Çalıştırma: pytest tests/test_app.py -v

NOT: Bu modül, app.py'yi import ederken arka planda bir SyncWorker thread'i
başlatır; bu yüzden SYNC_INTERVAL_SECONDS uzun bir değere ayarlanır, testler
sırasında gereksiz arka plan döngüsünün araya girmesi önlenir.
"""

import io
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("SYNC_INTERVAL_SECONDS", "3600")
os.environ.setdefault("KURUM_TUZU", "test-kurum-gizli-tuz-degeri-pytest")
os.environ.setdefault("KURUM_ADI", "Test Sınav Merkezi (pytest)")

import pytest

import app as appmod
import offline_queue

DATA_DIR = PROJECT_ROOT / "data" / "uploads"
SAMPLE_IMAGE = DATA_DIR / "test_warped.jpg"


@pytest.fixture(scope="module", autouse=True)
def _reset_instance_db():
    """Bu test modülü gerçek app.py örneğini import ettiğinden, gerçek
    instance veritabanı dosyasını kullanır. Testlerin tekrar tekrar
    çalıştırılabilir (idempotent) olması için modül başında temizlenir."""
    db_path = appmod.config.sqlite_full_path
    if db_path.exists():
        db_path.unlink()
    offline_queue.init_db(appmod.config)
    yield


@pytest.fixture
def client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


@pytest.fixture
def sample_image_bytes():
    with open(SAMPLE_IMAGE, "rb") as f:
        return f.read()


class TestIndexAndStaticPages:
    def test_index_returns_200(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Optik" in r.data.decode("utf-8")

    def test_verify_get_returns_200(self, client):
        r = client.get("/verify")
        assert r.status_code == 200

    def test_unknown_route_returns_404(self, client):
        r = client.get("/bu-rota-yok")
        assert r.status_code == 404

    def test_status_for_unknown_id_returns_404(self, client):
        r = client.get("/status/999999")
        assert r.status_code == 404


class TestUploadValidation:
    def test_missing_file_field_returns_400(self, client):
        r = client.post("/upload", data={})
        assert r.status_code == 400

    def test_empty_filename_returns_400(self, client):
        r = client.post(
            "/upload",
            data={"optik_form": (io.BytesIO(b""), ""), "sinav_tarihi": "2026-06-17"},
            content_type="multipart/form-data",
        )
        assert r.status_code == 400

    def test_disallowed_extension_returns_400(self, client):
        r = client.post(
            "/upload",
            data={
                "optik_form": (io.BytesIO(b"not an image"), "form.txt"),
                "sinav_tarihi": "2026-06-17",
            },
            content_type="multipart/form-data",
        )
        assert r.status_code == 400

    def test_missing_sinav_tarihi_returns_400(self, client, sample_image_bytes):
        r = client.post(
            "/upload",
            data={"optik_form": (io.BytesIO(sample_image_bytes), "form.jpg")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 400

    def test_corrupt_image_bytes_returns_400(self, client):
        r = client.post(
            "/upload",
            data={
                "optik_form": (io.BytesIO(b"\x00\x01\x02 bozuk veri"), "form.jpg"),
                "sinav_tarihi": "2026-06-17",
            },
            content_type="multipart/form-data",
        )
        assert r.status_code == 400


class TestUploadHappyPathWithoutNetwork:
    """Sandbox/CI ortamında Pinata/Polygon ağına erişim yoktur; bu durumda
    sistemin PENDING/FAILED durumuna zarifçe düşmesi ve 200 ile pending.html
    döndürmesi beklenen davranıştır (gerçek üretimde .env tanımlıyken
    bu kayıtlar SYNCED olur)."""

    def test_valid_upload_renders_pending_page(self, client, sample_image_bytes):
        r = client.post(
            "/upload",
            data={
                "optik_form": (io.BytesIO(sample_image_bytes), "form.jpg"),
                "sinav_tarihi": "2026-06-17",
            },
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
        body = r.data.decode("utf-8")
        assert "bubble-sheet" in body or "bubble" in body
        assert "pending-card" in body

    def test_duplicate_upload_returns_409(self, client, sample_image_bytes):
        data = {
            "optik_form": (io.BytesIO(sample_image_bytes), "form.jpg"),
            "sinav_tarihi": "2099-01-01",  # benzersiz tarih -> benzersiz k anahtarı
        }
        r1 = client.post("/upload", data=dict(data), content_type="multipart/form-data")
        assert r1.status_code == 200

        data2 = {
            "optik_form": (io.BytesIO(sample_image_bytes), "form.jpg"),
            "sinav_tarihi": "2099-01-01",
        }
        r2 = client.post("/upload", data=data2, content_type="multipart/form-data")
        assert r2.status_code == 409

    def test_status_endpoint_reports_pending_or_failed(self, client, sample_image_bytes):
        r = client.post(
            "/upload",
            data={
                "optik_form": (io.BytesIO(sample_image_bytes), "form.jpg"),
                "sinav_tarihi": "2099-02-02",
            },
            content_type="multipart/form-data",
        )
        assert r.status_code == 200

        stats = offline_queue.queue_stats(appmod.config)
        assert stats[offline_queue.STATUS_PENDING] + stats[offline_queue.STATUS_FAILED] >= 1


class TestVerifyValidation:
    def test_verify_with_invalid_tc_shows_error(self, client):
        r = client.post(
            "/verify",
            data={"tc_kimlik": "123", "isim": "TEST KISI", "sinav_tarihi": "2026-06-17"},
        )
        assert r.status_code == 200
        assert "callout" in r.data.decode("utf-8")


class TestVerifyAuth:
    """/verify HTTP Basic Auth korumasının davranışını doğrular.

    appmod.config, dataclasses.replace ile auth alanları doldurulmuş bir
    kopyaya geçici olarak değiştirilir (monkeypatch ile), test sonunda
    pytest otomatik olarak orijinaline geri döndürür.
    """

    @pytest.fixture
    def auth_credentials(self, monkeypatch):
        import dataclasses
        from werkzeug.security import generate_password_hash

        password = "test-sifre-123"
        patched_config = dataclasses.replace(
            appmod.config,
            INSTITUTION_VERIFY_USERNAME="yetkili_kullanici",
            INSTITUTION_VERIFY_PASSWORD_HASH=generate_password_hash(password),
        )
        monkeypatch.setattr(appmod, "config", patched_config)
        return "yetkili_kullanici", password

    def _basic_auth_header(self, username: str, password: str) -> dict:
        import base64

        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def test_open_with_warning_when_not_configured(self, client):
        r = client.get("/verify")
        assert r.status_code == 200
        assert "Güvenlik uyar" in r.data.decode("utf-8")

    def test_rejects_missing_credentials_when_configured(self, client, auth_credentials):
        r = client.get("/verify")
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers

    def test_rejects_wrong_password(self, client, auth_credentials):
        username, _ = auth_credentials
        r = client.get("/verify", headers=self._basic_auth_header(username, "yanlis-sifre"))
        assert r.status_code == 401

    def test_rejects_wrong_username(self, client, auth_credentials):
        _, password = auth_credentials
        r = client.get("/verify", headers=self._basic_auth_header("baska_biri", password))
        assert r.status_code == 401

    def test_accepts_correct_credentials_without_warning(self, client, auth_credentials):
        username, password = auth_credentials
        r = client.get("/verify", headers=self._basic_auth_header(username, password))
        assert r.status_code == 200
        assert "Güvenlik uyar" not in r.data.decode("utf-8")
