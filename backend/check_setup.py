"""
UODS kurulum doğrulama scripti.
.env içindeki bağlantıları test eder ama HİÇBİR gizli değeri ekrana yazmaz.
Sadece "var/yok" ve "bağlantı tamam/hatalı" gösterir.

Çalıştırma:  python check_setup.py
"""
from __future__ import annotations

import sys


def mask(value: str) -> str:
    """Bir değerin sadece var olup olmadığını ve uzunluğunu gösterir."""
    if not value:
        return "BOŞ"
    return f"dolu ({len(value)} karakter)"


def ok(msg: str) -> None:
    print(f"  [TAMAM] {msg}")


def fail(msg: str) -> None:
    print(f"  [HATA ] {msg}")


def warn(msg: str) -> None:
    print(f"  [UYARI] {msg}")


def main() -> int:
    print("=" * 60)
    print("UODS KURULUM KONTROLÜ")
    print("=" * 60)

    # --- 1. .env yükleniyor mu? ---
    print("\n[1] Yapılandırma yükleniyor...")
    try:
        from config import Config
        cfg = Config()
        ok(".env okundu, Config oluşturuldu")
    except Exception as e:
        fail(f"Config yüklenemedi: {e}")
        return 1

    # --- 2. Değerler dolu mu? (içerik gösterilmez) ---
    print("\n[2] Gerekli değerler dolu mu?")
    print(f"      POLYGON_RPC_URL        : {mask(cfg.POLYGON_RPC_URL)}")
    print(f"      INSTITUTION_PRIVATE_KEY: {mask(cfg.INSTITUTION_PRIVATE_KEY)}")
    print(f"      PINATA_JWT             : {mask(cfg.PINATA_JWT)}")
    print(f"      KURUM_TUZU             : {mask(cfg.KURUM_TUZU)}")
    print(f"      FLASK_SECRET_KEY       : {mask(cfg.SECRET_KEY)}")
    print(f"      CONTRACT_ADDRESS       : "
          f"{'henüz deploy edilmedi' if (not cfg.CONTRACT_ADDRESS or cfg.CONTRACT_ADDRESS.startswith('0x0000')) else 'ayarlı'}")

    # --- 3. Pinata bağlantısı ---
    print("\n[3] Pinata (IPFS) bağlantısı test ediliyor...")
    try:
        cfg.validate_for_ipfs()
        import ipfs_service
        # Pinata'nın testAuthentication ucu
        import requests
        headers = (
            {"Authorization": f"Bearer {cfg.PINATA_JWT}"}
            if cfg.PINATA_JWT
            else {
                "pinata_api_key": cfg.PINATA_API_KEY,
                "pinata_secret_api_key": cfg.PINATA_API_SECRET,
            }
        )
        r = requests.get(
            "https://api.pinata.cloud/data/testAuthentication",
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            ok("Pinata kimlik doğrulaması başarılı")
        else:
            fail(f"Pinata reddetti (HTTP {r.status_code}) - JWT yanlış olabilir")
    except Exception as e:
        fail(f"Pinata bağlantısı başarısız: {type(e).__name__}: {e}")

    # --- 4. Polygon RPC bağlantısı ---
    print("\n[4] Polygon (Amoy) RPC bağlantısı test ediliyor...")
    try:
        cfg.validate_for_blockchain()
        blockchain_validated = True
    except Exception as e:
        blockchain_validated = False
        warn(f"Blokzincir yapılandırması tam değil: {e}")
        warn("(CONTRACT_ADDRESS henüz boşsa bu normaldir - deploy adımında dolacak)")

    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(cfg.POLYGON_RPC_URL, request_kwargs={"timeout": 20}))
        if w3.is_connected():
            chain_id = w3.eth.chain_id
            ok(f"RPC bağlantısı kuruldu (chain_id={chain_id})")
            if chain_id == cfg.POLYGON_CHAIN_ID:
                ok(f"Chain ID doğru ({chain_id} = Amoy)")
            else:
                fail(f"Chain ID uyuşmuyor! RPC={chain_id}, beklenen={cfg.POLYGON_CHAIN_ID}")
        else:
            fail("RPC'ye bağlanılamadı - POLYGON_RPC_URL yanlış olabilir")
    except Exception as e:
        fail(f"RPC bağlantısı başarısız: {type(e).__name__}: {e}")

    # --- 5. Cüzdan + bakiye ---
    print("\n[5] Kurum cüzdanı ve POL bakiyesi kontrol ediliyor...")
    try:
        from eth_account import Account
        from web3 import Web3
        acct = Account.from_key(cfg.INSTITUTION_PRIVATE_KEY)
        # Adresin sadece son 4 hanesini göster (gizlilik)
        addr = acct.address
        print(f"      Cüzdan adresi: {addr[:6]}...{addr[-4:]}")
        w3 = Web3(Web3.HTTPProvider(cfg.POLYGON_RPC_URL, request_kwargs={"timeout": 20}))
        if w3.is_connected():
            bal_wei = w3.eth.get_balance(addr)
            bal_pol = w3.from_wei(bal_wei, "ether")
            if bal_wei > 0:
                ok(f"Bakiye: {bal_pol} POL - gas için yeterli")
            else:
                warn("Bakiye 0 POL - faucet'ten test POL çekmen gerekiyor")
        else:
            warn("RPC bağlı değil, bakiye okunamadı")
    except Exception as e:
        fail(f"Cüzdan okunamadı: {type(e).__name__}: {e} "
             "(INSTITUTION_PRIVATE_KEY '0x' ile başlamalı)")

    print("\n" + "=" * 60)
    print("Kontrol bitti. [HATA] satırlarını düzeltmen yeterli.")
    print("CONTRACT_ADDRESS uyarısı normaldir - sıradaki adım sözleşme deploy.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
