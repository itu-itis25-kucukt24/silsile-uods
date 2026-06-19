# -*- coding: utf-8 -*-
"""
wsgi.py
=======
Üretim WSGI sunucuları (gunicorn, uwsgi) için giriş noktası.

app.py'deki `if __name__ == "__main__":` bloğu (Flask'ın yerleşik
geliştirme sunucusunu başlatan kısım) yalnızca `python app.py` ile
doğrudan çalıştırıldığında devreye girer; gunicorn bu dosyayı import
ettiğinde o blok ÇALIŞMAZ. Bu yüzden üretimde her zaman bu dosya
üzerinden çalıştırın:

    gunicorn -c deploy/gunicorn.conf.py wsgi:app

Ayrıntılı kurulum adımları için deploy/README.md'ye bakınız.
"""
from app import app

if __name__ == "__main__":
    # Doğrudan `python wsgi.py` ile çalıştırılırsa da makul bir varsayılan
    # davranış sağlar (yine de üretimde gunicorn kullanılması önerilir).
    app.run(host="0.0.0.0", port=5000)
