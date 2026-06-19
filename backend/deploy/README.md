# UODS — Üretime Alma Rehberi

Bu rehber, projeyi gerçek bir Linux sunucuda (Ubuntu/Debian) gunicorn +
nginx + systemd ile çalıştırmak için izlenecek adımları açıklar. Tüm
yapılandırma dosyaları bu klasördedir ve gerçekten test edilmiştir
(`gunicorn.conf.py` gerçek bir gunicorn sürecinde çalıştırılıp HTTP isteği
ile doğrulanmış, `nginx_uods.conf` `nginx -t` ile sözdizimi açısından
doğrulanmıştır).

## Önkoşullar

- Bir alan adı (domain) ve bu alan adını sunucunuzun IP adresine yönlendiren
  bir DNS A kaydı (SSL sertifikası için zorunlu — bu rehberin kapsamı
  dışındadır, alan adı sağlayıcınızdan edinmeniz gerekir).
- Doldurulmuş bir `.env` dosyası (bkz. ana `README.md`).
- `python scripts/deploy_contract.py` ile dağıtılmış ve `.env`'e yazılmış
  bir `CONTRACT_ADDRESS`.

## 1) Sunucu hazırlığı

```bash
sudo apt update
sudo apt install -y python3-venv nginx certbot python3-certbot-nginx

sudo useradd -r -s /usr/sbin/nologin uods   # uygulamayı root olarak çalıştırmayın
sudo mkdir -p /opt/uods_project
sudo chown uods:uods /opt/uods_project
```

Proje dosyalarını `/opt/uods_project` altına kopyalayın (örn. `scp`/`rsync`
veya `git clone`), ardından:

```bash
cd /opt/uods_project
sudo -u uods python3 -m venv .venv
sudo -u uods .venv/bin/pip install -r requirements.txt
sudo -u uods cp .env.example .env
sudo -u uods nano .env   # gerçek değerlerinizi doldurun
```

## 2) Gunicorn'u systemd servisi olarak kaydedin

```bash
sudo cp deploy/uods.service /etc/systemd/system/uods.service
sudo nano /etc/systemd/system/uods.service   # <UODS_KULLANICISI> yer tutucusunu "uods" yapın
sudo systemctl daemon-reload
sudo systemctl enable --now uods
sudo systemctl status uods
```

Servis ayakta mı diye yerel olarak kontrol edin:

```bash
curl -I http://127.0.0.1:8000/
```

## 3) Nginx ters vekili kurun

```bash
sudo cp deploy/nginx_uods.conf /etc/nginx/sites-available/uods
sudo nano /etc/nginx/sites-available/uods   # <ALAN_ADINIZ> yer tutucusunu gerçek alan adınızla değiştirin
sudo ln -s /etc/nginx/sites-available/uods /etc/nginx/sites-enabled/uods
sudo nginx -t
sudo systemctl reload nginx
```

Bu noktada `http://<alan_adınız>` üzerinden (henüz HTTPS olmadan) siteye
erişebilmeniz gerekir.

## 4) HTTPS (Let's Encrypt / Certbot)

```bash
sudo certbot --nginx -d <ALAN_ADINIZ>
```

Certbot, nginx yapılandırmanızı otomatik olarak düzenleyip 443 portu ve
sertifika yenileme görevini (cron/systemd timer) kendisi ekler. Bu komut
yalnızca DNS A kaydınız doğru sunucuyu gösteriyorsa çalışır.

## 5) Doğrulama

```bash
curl -I https://<ALAN_ADINIZ>/
sudo journalctl -u uods -f      # canlı uygulama logları
tail -f /opt/uods_project/instance/logs/uods.log
```

## 6) `/verify` erişim korumasını unutmayın

`.env` içinde `INSTITUTION_VERIFY_USERNAME` ve
`INSTITUTION_VERIFY_PASSWORD_HASH` tanımlı değilse `/verify` ekranı
korumasız kalır (ekranda da görünür bir uyarı çıkar). Üretime almadan önce
ana `README.md`'deki talimatla bir şifre hash'i üretip bu iki değeri
doldurun.

## Güncelleme (yeni sürüm dağıtımı)

```bash
cd /opt/uods_project
sudo -u uods git pull             # veya dosyaları yeniden kopyalayın
sudo -u uods .venv/bin/pip install -r requirements.txt
sudo systemctl restart uods
```

`offline_queue.py` SQLite veritabanı (`instance/uods_queue.db`) ve
yüklenen/işlenen görüntüler (`data/`) güncellemeler arasında korunur;
yalnızca uygulama kodu yeniden başlatılır.
