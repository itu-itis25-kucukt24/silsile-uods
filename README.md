# SİLSİLE - Uç Optik Doğrulama Sistemi (UODS)

Bu proje, merkezi sınav süreçlerindeki optik cevap formlarının dijital görüntülerini alıp, kriptografik yöntemlerle değiştirilemez hale getirerek Polygon blokzincir ağına kaydeden bir sistemdir.

## Kullanılan Teknolojiler
* **Frontend:** HTML5, CSS3, JS
* **Backend:** Python (Flask), SQLite (Çevrimdışı Tampon)
* **Görüntü İşleme:** OpenCV, NumPy (Otsu Eşikleme, Gauss Filtresi)
* **Blokzincir & Depolama:** Polygon PoS Testnet, Solidity, Web3.py, IPFS (Pinata API)

## Kurulum
1. Repoyu klonlayın.
2. `pip install -r requirements.txt` komutu ile kütüphaneleri yükleyin.
3. `.env.example` dosyasının adını `.env` olarak değiştirin ve Pinata, Alchemy ile Cüzdan API/Private Key bilgilerinizi girin.
4. `python backend/app.py` ile sunucuyu başlatın.