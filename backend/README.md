# UODS — Uç Optik Doğrulama Sistemi

Blokzincir tabanlı, gerçek zamanlı optik form (OMR) analiz ve doğrulama
sistemi. TEKNOFEST yarışması kapsamında geliştirilmiştir.

Bir optik cevap kâğıdının fotoğrafı yüklendiğinde sistem: görüntüyü
temizler (BT.601 gri tonlama → Gauss bulanıklaştırma → Otsu eşikleme),
referans işaretçileri bularak perspektif düzeltmesi yapar, kareleri okur
(TC kimlik no, isim, cevaplar), kimlik bilgisini SHA-256 tabanlı tek yönlü
bir anahtarla (k) anonimleştirir, ham fotoğrafı ve kimlik bilgisini siler,
yalnızca anonim k anahtarı + cevap dizisi + bütünlük özetini IPFS'e (Pinata)
yükler ve Polygon ağındaki bir akıllı sözleşmeye mühürler. İnternet yoksa
kayıt yerel SQLite kuyruğunda bekletilir ve bağlantı geri geldiğinde
otomatik olarak senkronize edilir.

## İçindekiler

1. [Mimari özet](#mimari-özet)
2. [Proje yapısı](#proje-yapısı)
3. [Kurulum](#kurulum)
4. [Akıllı sözleşmenin dağıtımı](#akıllı-sözleşmenin-dağıtımı)
5. [Uygulamayı çalıştırma](#uygulamayı-çalıştırma)
6. [Testler](#testler)
7. [Çevrimdışı kuyruk ve senkronizasyon](#çevrimdışı-kuyruk-ve-senkronizasyon)
8. [Güvenlik ve KVKK notları](#güvenlik-ve-kvkk-notları)
9. [Bilinen sınırlamalar](#bilinen-sınırlamalar)

## Mimari özet

```
[Tarayıcı]
   │  1) Fotoğraf + sınav tarihi yükle (POST /upload)
   ▼
[Flask — app.py]
   │  2) image_processing.py  : BT.601 gri tonlama → Gauss → Otsu
   │  3) omr_engine.py        : referans tespiti → perspektif düzeltme → hücre okuma
   │  4) crypto_utils.py      : k = SHA256(TC ‖ isim ‖ tarih ‖ kurum_tuzu)
   │                              (TC ve isim BURADAN SONRA HİÇBİR YERE YAZILMAZ)
   │  4b) redact_personal_fields : TC/isim baloncuk bölgeleri görüntüden de
   │                              SİLİNİR (beyazlanır) — saklanan/IPFS'e giden
   │                              görüntü artık görsel olarak da kişisel veri içermez
   │  5) offline_queue.py     : SQLite'a PENDING olarak kaydet (h_local, Denklem 9)
   ▼
[SyncWorker — arka plan thread'i, periyodik, TOPLU/BATCH]
   │  6) ipfs_service.py      : her kaydın REDAKTE görüntüsünü Pinata'ya yükle → CID
   │  7) merkle.py            : turdaki TÜM kayıtların h_local'lerinden Merkle ağacı kur
   │  8) blockchain_service.py: Polygon'a YALNIZCA kökü gönder → addBatchRoot(root, n)
   │                              (bireysel hiçbir kayıt değeri zincire yazılmaz)
   ▼
[OpticalFormRegistry.sol — Polygon üzerindeki akıllı sözleşme]
   kalıcı, değiştirilemez kayıt: merkleRoot → (zaman damgası, imzalayan, kayıt sayısı)
   bireysel üyelik ispatı: verifyInclusion(root, leaf, proof) (zincirde, ücretsiz)
```

> **Hakem değerlendirmesine yanıt:** Bu akış, jüri geri bildirimi
> doğrultusunda güncellenmiştir; ayrıntılı gerekçe için aşağıdaki
> "Hakem Değerlendirmesine Yanıt: Merkle Toplu Mühürleme" bölümüne bakınız.

## Proje yapısı

```
uods_project/
├── app.py                          Flask sunucusu (rotalar, orkestrasyon)
├── wsgi.py                         Üretim WSGI giriş noktası (gunicorn buradan çalıştırılır)
├── config.py                       Ortam değişkenlerinden okunan yapılandırma
├── crypto_utils.py                 KVKK anonimleştirme + SHA-256 yardımcıları
├── image_processing.py             OpenCV görüntü temizleme hattı
├── omr_engine.py                   Referans tespiti, perspektif düzeltme, hücre okuma
├── ipfs_service.py                 Pinata (IPFS) REST entegrasyonu
├── blockchain_service.py           Web3.py ile Polygon entegrasyonu (Merkle toplu mühürleme)
├── merkle.py                       Merkle ağacı: kök üretimi + üyelik kanıtı (proof)
├── offline_queue.py                SQLite tabanlı çevrimdışı kuyruk + SyncWorker (batch)
├── requirements.txt                Çalışma zamanı bağımlılıkları
├── requirements-dev.txt            Geliştirme/derleme/test bağımlılıkları
├── .env.example                    Ortam değişkeni şablonu (kopyalayıp doldurun)
├── config/
│   └── omr_template.json           Form ızgara/alan/eşik tanımı (kurum bazlı ayarlanabilir)
├── contracts/
│   ├── OpticalFormRegistry.sol      Akıllı sözleşme kaynağı (Solidity ^0.8.24)
│   ├── OpticalFormRegistry.abi.json Derlenmiş ABI (solc çıktısı, hazır)
│   ├── OpticalFormRegistry.bytecode.txt  Derlenmiş bytecode (solc çıktısı, hazır)
│   └── SLITHER_REPORT.md            Statik güvenlik analizi sonuçları (gerçek tarama)
├── scripts/
│   ├── generate_test_form.py        Bilinen cevaplı sentetik test formu üretir
│   ├── deploy_contract.py           Hazır ABI/bytecode'u Polygon'a dağıtır
│   └── solc_compat_wrapper.py       Slither için solc uyumluluk katmanı (bkz. yukarı)
├── deploy/
│   ├── README.md                    Gunicorn+nginx+systemd+HTTPS kurulum rehberi
│   ├── gunicorn.conf.py             Üretim WSGI sunucu yapılandırması
│   ├── uods.service                 systemd servis tanımı
│   └── nginx_uods.conf               Nginx ters vekil + HTTPS şablonu
├── templates/                       Jinja2 HTML şablonları
├── static/
│   ├── css/style.css                 Ortak tasarım sistemi
│   └── js/                           Yükleme önizlemesi + durum sorgulama (polling)
├── tests/                           pytest birim/entegrasyon testleri
└── data/, instance/                 Çalışma zamanı verisi (Git'e dahil edilmez)
```

## Kurulum

```bash
# 1) Sanal ortam (önerilir)
python3 -m venv .venv
source .venv/bin/activate

# 2) Bağımlılıklar
pip install -r requirements.txt
# (Geliştirme/test için ek olarak:)
pip install -r requirements-dev.txt

# 3) Ortam değişkenleri
cp .env.example .env
# .env dosyasını açıp KURUM_TUZU, PINATA_*, POLYGON_*,
# INSTITUTION_PRIVATE_KEY ve CONTRACT_ADDRESS değerlerini doldurun.
```

`config.py` modülü, uygulama her başladığında `data/uploads/`,
`data/processed/` ve `instance/` klasörlerini otomatik olarak oluşturur.

## Akıllı sözleşmenin dağıtımı

Sözleşme zaten derlenmiştir (`contracts/OpticalFormRegistry.abi.json` ve
`.bytecode.txt`); yeniden derlemeniz GEREKMEZ. Yalnızca Polygon ağına
dağıtmanız (deploy) yeterlidir:

```bash
python scripts/deploy_contract.py
```

Betik, `.env` içindeki `POLYGON_RPC_URL` ve `INSTITUTION_PRIVATE_KEY`
değerlerini kullanarak sözleşmeyi dağıtır ve sonunda size şu satırı verir:

```
CONTRACT_ADDRESS=0x...
```

Bu satırı `.env` dosyanıza ekleyin/güncelleyin. Sözleşme kaynağını
değiştirirseniz (`contracts/OpticalFormRegistry.sol`), yeniden derleyip
ABI/bytecode dosyalarını manuel olarak güncellemeniz gerekir (solc 0.8.24
ile derlenmiştir; `requirements-dev.txt` içindeki `py-solc-x` paketi bu
amaçla kullanılabilir).

## Uygulamayı çalıştırma

```bash
python app.py
# veya
flask --app app run
```

Varsayılan olarak `http://0.0.0.0:5000` adresinde çalışır. Tarayıcınızdan
`http://localhost:5000` adresine giderek formu yükleyebilirsiniz.

Üretim ortamında `FLASK_ENV=production` (varsayılan) bırakılmalı ve
gunicorn/uwsgi gibi bir WSGI sunucusu arkasında çalıştırılmalıdır; Flask'ın
yerleşik geliştirme sunucusu üretim için tasarlanmamıştır.

Gerçek bir sunucuya (gunicorn + nginx + systemd + HTTPS) adım adım kurulum
için [`deploy/README.md`](deploy/README.md)'ye bakınız; oradaki
yapılandırma dosyaları (`gunicorn.conf.py`, `uods.service`,
`nginx_uods.conf`) gerçekten test edilmiştir.

## Testler

```bash
pytest tests/ -v
```

Test kapsamı: KVKK anonimleştirme zinciri (isim normalizasyonu, TCKN
kontrol basamağı, k anahtarı determinizmi), görüntü işleme hattı (BT.601
gri tonlama, Gauss, Otsu), OMR motoru (sentetik formlar üzerinde uçtan uca
TC/isim/cevap okuma doğruluğu, hem düz hem perspektif-bozulmalı görüntüde,
ayrıca GECERSIZ/çoklu-işaretleme dalı), çevrimdışı kuyruk durum makinesi ve
SyncWorker (enjekte edilmiş sahte senkronizasyon fonksiyonuyla, gerçek ağ
GEREKMEDEN), Merkle ağacı (kök determinizmi, tek/çift yaprak kanıtları,
bozuk veri tespiti), ve Flask rotalarının doğrulama/hata/başarı yolları.

`tests/test_blockchain_service.py`, akıllı sözleşmeyi GERÇEK bir yerel EVM'e
(`eth-tester`/`py-evm`) dağıtarak `blockchain_service.py`'nin batch/Merkle
fonksiyonlarını uçtan uca doğrular (1–33 yaprak senaryoları, yanlış
yaprak/bozuk kanıt reddi, var olmayan parti reddi dahil). Bu, gerçek
Polygon erişimi olmadan bile zincir mantığının gerçek EVM yürütmesiyle
test edilebilmesini sağlar.

Pinata/Polygon ağına gerçek (canlı, internet üzerinden) bağlantı gerektiren
testler bilerek **dahil edilmemiştir**: bunun yerine, ağ erişimi olmadığında
sistemin PENDING/FAILED durumuna zarifçe düşüp kullanıcıya anlaşılır biçimde
bunu göstermesi test edilir — bu, raporun çevrimdışı dayanıklılık
gereksinimiyle (bölüm 3.6) doğrudan örtüşür.

`tests/test_concurrency.py` ayrıca gunicorn'un üretimdeki gerçek çoklu
süreç (multi-process) modelini taklit eden GERÇEK bir eşzamanlılık testi
içerir: 8 ayrı OS sürecinin aynı SQLite dosyasına eşzamanlı yazdığı ve
5 ayrı sürecin aynı anahtar için yarıştığı senaryolar — her ikisi de veri
kaybı veya bozulma olmadan doğru sonuçlanır.

### Akıllı sözleşme statik güvenlik taraması (Slither)

Rapor bölüm 3.8.1'de taahhüt edilen Slither taraması fiilen
çalıştırılmıştır:

```bash
slither contracts/OpticalFormRegistry.sol
```

**Sonuç: 101 dedektörün tamamında sıfır bulgu** — yeniden giriş
(reentrancy), erişim kontrolü, tamsayı taşması ve `tx.origin` kategorileri
dahil. Ayrıntılı sonuç tablosu ve metodoloji için
[`contracts/SLITHER_REPORT.md`](contracts/SLITHER_REPORT.md)'ye bakınız.
(Bu geliştirme ortamında native `solc` ikilisine ağ erişimi kısıtlı
olduğundan, tarama `scripts/solc_compat_wrapper.py` adlı küçük bir
uyumluluk katmanı üzerinden npm'in resmî solcjs paketiyle çalıştırılmıştır;
native solc kurulu bir ortamda sarmalayıcıya gerek yoktur.)


## Çevrimdışı kuyruk ve senkronizasyon

`offline_queue.py`, her kaydı `PENDING → SYNCING → SYNCED` (veya hatada
`FAILED`, otomatik yeniden deneme ile) durum makinesinde tutar. Arka
planda çalışan `SyncWorker`, `SYNC_INTERVAL_SECONDS` aralığında interneti
kontrol eder (Google Public DNS'e hafif bir TCP el sıkışmasıyla) ve
bağlantı varsa bekleyen/başarısız kayıtları **tek bir parti (batch)**
olarak işler: her kaydın redakte görüntüsü ayrı ayrı IPFS'e yüklenir,
ardından tüm kayıtların `h_local` değerlerinden bir Merkle ağacı kurulup
**yalnızca kökü** Polygon'a tek bir işlemle mühürlenir. Parti atomiktir:
on-chain işlem tek olduğundan, tur ya tümüyle başarılı olur (tüm kayıtlar
`SYNCED`, ortak `tx_hash` + ortak `merkle_root` + her kayıt için kendi
bireysel kanıtı `merkle_proof_json` ile) ya da tümü `FAILED`'a döner.
`SYNC_MAX_RETRY` sınırını aşan kayıtlar `FAILED` durumunda kalır ve manuel
inceleme gerektirir.

**Önemli (KVKK):** SQLite kuyruğunda TC kimlik numarası ve isim **hiçbir
zaman** saklanmaz; yalnızca anonim k anahtarı, cevap dizisi, görüntü
bütünlük özeti (h_local), Merkle kökü/kanıtı ve dosya yolu tutulur. Ham
fotoğraf, kimlik alanları okunduktan sonra işlenmiş (kanonik) **ve TC/isim
bölgeleri görsel olarak silinmiş (redakte)** bir görüntüye dönüştürülür;
kimlik bilgisi hiçbir aşamada IPFS'e veya zincire yazılmaz.

## Güvenlik ve KVKK notları

- Kimlik bilgisi anonimleştirmesi tek yönlüdür (SHA-256); kurumun elindeki
  TC/isim/tarih bilgisiyle k anahtarı yeniden türetilip zincirdeki kayıt
  doğrulanabilir (`/verify` ekranı), ancak zincirden veya IPFS'ten asla
  geriye TC/isim elde edilemez.
- `INSTITUTION_PRIVATE_KEY`, sunucu tarafında "kurumsal aktarıcı
  (relayer)" modeliyle kullanılır; öğrenciler/okuyucular kendi cüzdanlarıyla
  hiçbir şekilde etkileşmez. Bu anahtar asla kaynak koduna gömülmez, yalnızca
  `.env` üzerinden okunur ve **kesinlikle** sürüm kontrolüne eklenmemelidir.
- `/verify` ekranı `INSTITUTION_VERIFY_USERNAME` / `INSTITUTION_VERIFY_PASSWORD_HASH`
  tanımlandığında HTTP Basic Auth ile korunur (şifre asla düz metin
  saklanmaz, yalnızca `werkzeug.security.generate_password_hash` özeti
  tutulur). Bu ikisi `.env`'de tanımlı değilse ekran açık kalır ve bunu
  ekranda görünür bir uyarı banner'ıyla bildirir — sessizce güvensiz
  kalmaz. Üretime almadan önce bu iki değeri tanımlamanız ZORUNLUDUR
  (kurulum talimatı `.env.example` içinde).
- `OpticalFormRegistry.sol`, yetki kontrolünü `msg.sender` üzerinden yapar
  (`tx.origin` KULLANILMAZ); bu, yaygın bir güvenlik açığı sınıfından
  (phishing/yeniden yönlendirme saldırıları) kaçınmak için bilinçli bir
  tasarım kararıdır. Bu, gerçek bir Slither taraması ile de doğrulanmıştır
  (bkz. Slither bölümü).
- Uygulama, `instance/logs/uods.log`'a dönen (rotating) dosya loglaması
  yapar ve isteğe bağlı olarak Sentry'ye hata bildirimi gönderebilir
  (`SENTRY_DSN` tanımlıysa). Sentry'ye KVKK kapsamında kişisel veri
  GÖNDERİLMEZ (`send_default_pii=False`).

## Hakem Değerlendirmesine Yanıt: Merkle Toplu Mühürleme

### Geri bildirim

Jüri değerlendirmesinde özetle şu husus belirtilmiştir: öğrenci numarası
ve T.C. kimlik numarası gibi hassas kişisel verilerin **özet (hash)
değerlerinin** blokzincire yazılması, özetten gerçek veriye ulaşmak
teknik olarak imkânsıza yakın olsa dahi, hassas verinin herhangi bir
biçiminin kalıcı ve herkese açık bir yapıya kaydedilmesi açısından genel
kabul gören bir uygulama değildir. Jüri, sıfır bilgi ispatı (ZKP) veya
homomorfik şifreleme gibi alternatiflerin değerlendirilmesini önermiştir.

### Neden tam ZKP / homomorfik şifreleme doğrudan uygulanmadı

Tam bir zk-SNARK/zk-STARK devresi veya homomorfik şifreleme şeması; özel
devre tasarımı, güvenilir kurulum (trusted setup) seremonisi, alışılmadık
araç zincirleri (circom, snarkjs, halo2 vb.) ve önemli bir performans/gas
maliyeti getirir. Bu, mevcut yarışma kapsamı ve takım ölçeği için
gerçekçi bir mühendislik tercihi değildir. Bunun yerine, **aynı
kriptografik ailenin** (Merkle ağacı tabanlı üyelik ispatı) daha hafif
ama gerçek bir gizlilik kazancı sağlayan tekniği uygulanmıştır — ki bu
teknik, Semaphore, Tornado Cash ve zk-rollup'ların tamamının temel yapı
taşıdır.

### Yeni tasarım: yalnızca kök zincire yazılır

- Bir senkronizasyon turunda biriken **tüm** kayıtların bütünlük özetleri
  (`h_local`, Denklem 9) tek bir **Merkle ağacında** birleştirilir
  (`merkle.py`).
- Zincire artık `addRecord(kAnahtar, CID)` ile **bireysel** hiçbir değer
  yazılmaz; yalnızca tek bir **kök** yazılır:
  `addBatchRoot(merkleRoot, recordCount)`.
- Bu kök, tek başına hiçbir kaydın kimliğini, varlığını veya içeriğini
  ifşa etmez (rastgele görünen 32 baytlık bir özet).
- Belirli bir kaydın o köke dahil olduğunu ispatlamak için gereken
  **Merkle kanıtı** (kardeş düğüm listesi) zincire/IPFS'e **hiçbir zaman**
  yazılmaz; yalnızca yetkili tarafın (kurum) yerel veritabanında saklanır
  ve doğrulama anında `verifyInclusion(root, leaf, proof)` ile zincirde
  kriptografik olarak kanıtlanır.

Bu sayede, jürinin asıl kaygısı olan senaryo ortadan kalkar: artık kurum
tuzu bir gün sızsa bile, zincirde tek tek sorgulanıp ifşa edilebilecek
bireysel kayıt türevleri yoktur; zincirde dolaşan tek kamuya açık veri,
anonim parti kökleridir.

### Tamamlayıcı iyileştirme: görüntü redaksiyonu

Kod incelemesi sırasında, IPFS'e yüklenen kanonik görüntünün TC/isim
baloncuk bölgelerini **görsel olarak** hâlâ içerdiği (yani sadece hash
değil, görüntünün kendisi de kişisel veri sızdırdığı) tespit edilmiştir.
`omr_engine.redact_personal_fields`, bu bölgeleri kalıcı saklamadan önce
beyazla doldurur; OMR okuma orijinal görüntü üzerinde zaten tamamlandığı
için doğruluk etkilenmez. Bütünlük özeti (`h_local`), gerçekten saklanan
redakte edilmiş görüntü üzerinden hesaplanır.

### Doğrulama

Yeni sözleşme, gerçek bir yerel EVM'e (`eth-tester`/`py-evm`) fiilen
dağıtılıp uçtan uca test edilmiştir (`tests/test_blockchain_service.py`,
`tests/test_merkle.py`): 1–33 yaprak arası tek/çift sayıda kayıt
senaryoları, yanlış yaprak/bozuk kanıt reddi, erişim kontrolü, mükerrer
kök reddi ve sahiplik devri dahil. Slither statik analizi 101 dedektörün
tamamında sıfır bulgu vermiştir (`contracts/SLITHER_REPORT.md`).

### Gelecek Çalışma

Tam bir gizlilik korumalı üyelik ispatı için, bu Merkle tabanı doğal bir
yükseltme yolu sunar: **Semaphore protokolü** veya **zk-kit** kütüphaneleri
ile, üyelik kanıtının kendisi de bir zk-SNARK'a dönüştürülerek "kaydın
hangi yaprak olduğu" bilgisi dahi gizlenebilir. Mevcut tasarım, bu
yükseltmeyi mümkün kılan aynı ağaç yapısını zaten kullanmaktadır.

## Bilinen sınırlamalar

- `static/js/upload_preview.js` içindeki köşe kılavuzu, canlı kamera akışı
  değil, seçilen/sürüklenen fotoğraf üzerine statik olarak çizilen bir
  hizalama yardımcısıdır; raporun "gerçek zamanlı kılavuz çizgiler"
  vizyonunun hafif bir ön-uçtaki karşılığıdır.
- OMR ızgara/alan/eşik tanımı (`config/omr_template.json`) belirli bir form
  tasarımına göre kalibre edilmiştir; farklı bir form düzeni kullanılacaksa
  bu dosyanın (ve `scripts/generate_test_form.py`'nin) güncellenmesi
  gerekir.
- Sözleşme tek bir kurum (`owner`) modeline göre tasarlanmıştır; çok
  kurumlu bir senaryo için yetkilendirme mantığının genişletilmesi
  gerekir.
- Parti (batch) granülerliği şu an "bir senkronizasyon turunda biriken
  ne varsa" şeklindedir; yani bir partinin hangi kayıtları içereceği
  `SYNC_INTERVAL_SECONDS` tikine bağlıdır. Daha öngörülebilir partiler
  için bu aralık artırılabilir veya ileride manuel bir "partiyi kapat"
  tetikleyicisi (oturum-farkında gruplama) eklenebilir.
- Merkle üyelik kanıtı (`merkle_proof_json`) yalnızca yerel veritabanında
  tutulur ve bilerek hiçbir yere yayınlanmaz (gizlilik amacı budur). Bu
  nedenle yerel veritabanının yedeklenmesi önemlidir: kanıt kaybolursa,
  o kaydın belirli bir köke ait olduğu (kök zincirde kalsa bile) yeniden
  ispatlanamaz. Kök ve yapraklar elde mevcutsa kanıt yeniden üretilebilir;
  bu yüzden h_local değerleri de kuyrukta saklanır.

## Takım içi klasör taşıma notu

Bu klasör şu an **tek başına çalışabilen tam bir prototip** olarak
paketlenmiştir; `app.py`'nin çalışması için gereken her modül burada
mevcuttur. Ancak bazı dosyalar kavramsal olarak başka bir takım üyesinin
uzmanlık alanına ait olup, ekip deposunun (repo) ana dizinindeki kendi
klasörlerine (`omr_engine/`, `blokchain/contracts/`) taşınması daha
doğru olur:

- `omr_engine.py`, `image_processing.py`, `config/omr_template.json`,
  `scripts/generate_test_form.py` → **Görüntü İşleme Uzmanı**'nın alanı,
  `omr_engine/` klasörüne taşınabilir.
- `blockchain_service.py`, `crypto_utils.py`, `contracts/` klasörünün
  tamamı, `scripts/deploy_contract.py`, `scripts/solc_compat_wrapper.py`
  → **Blokzincir ve Veri Güvenliği Uzmanı**'nın alanı, `blokchain/contracts/`
  klasörüne taşınabilir.

Bu dosyalar taşındığında `app.py`'nin en üstündeki import satırlarının
(`import omr_engine`, `from omr_engine import ...`,
`import blockchain_service`, `import crypto_utils`) yeni klasör yoluna
göre güncellenmesi gerekir (örn. paket haline getirip
`from omr_engine.omr_engine import ...` ya da projeyi bir paket kök
dizininden çalıştırıp `sys.path`'e ilgili klasörleri eklemek gibi). Bu
taşıma işlemi bilerek bu teslimde yapılmamıştır; backend bağımsız ve
çalışır durumda kalsın istendi.
