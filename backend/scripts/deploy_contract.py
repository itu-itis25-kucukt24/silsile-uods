# -*- coding: utf-8 -*-
"""
scripts/deploy_contract.py
===========================
OpticalFormRegistry akıllı sözleşmesini, daha önce solc ile derlenmiş ve
contracts/ klasöründe hazır bulunan ABI + bytecode çiftini kullanarak
Polygon ağına (mainnet ya da Amoy testnet) dağıtan tek seferlik kurulum
betiği (rapor bölüm 3.7.1 — "Akıllı Sözleşme Dağıtımı").

Bu betik solc derleyicisine GEREK DUYMAZ; contracts/OpticalFormRegistry.sol
zaten derlenmiş ve çıktıları (ABI/bytecode) sürüm kontrolüne dahil edilmiştir.
Sözleşme kaynağını değiştirirseniz, yeniden derleyip bu iki dosyayı manuel
olarak güncellemeniz gerekir.

Kullanım:
    python scripts/deploy_contract.py

Önkoşullar (.env içinde tanımlı olmalı):
    POLYGON_RPC_URL          — Alchemy/Infura vb. Polygon RPC uç noktası
    INSTITUTION_PRIVATE_KEY  — Dağıtımı yapacak kurum cüzdanının özel anahtarı
                                (bu cüzdan, sözleşmenin "owner"ı olur ve
                                ileride addBatchRoot() çağrılarını imzalar)
    POLYGON_CHAIN_ID         — Varsayılan: 80002 (Amoy Testnet)

Çıktı: Dağıtılan sözleşmenin adresi konsola yazdırılır. Bu adresi .env
dosyasındaki CONTRACT_ADDRESS değişkenine elle kopyalamanız gerekir
(uygulama bu adresi config.py üzerinden okur).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Proje kök dizinini import yoluna ekle (betik scripts/ altından çalıştırılsa da).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402
from web3.exceptions import TimeExhausted  # noqa: E402

from config import config  # noqa: E402

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
ABI_PATH = CONTRACTS_DIR / "OpticalFormRegistry.abi.json"
BYTECODE_PATH = CONTRACTS_DIR / "OpticalFormRegistry.bytecode.txt"


def _fail(message: str) -> None:
    print(f"\n[HATA] {message}\n", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    print("=" * 70)
    print("UODS — OpticalFormRegistry Sözleşme Dağıtım Betiği")
    print("=" * 70)

    # --- 1) Önkoşulları doğrula -------------------------------------------
    if not config.POLYGON_RPC_URL:
        _fail(
            "POLYGON_RPC_URL tanımlı değil. .env dosyasında Polygon RPC "
            "uç noktanızı tanımlayın (örn. Alchemy/Infura Amoy testnet URL'si)."
        )
    if not config.INSTITUTION_PRIVATE_KEY or config.INSTITUTION_PRIVATE_KEY.startswith(
        "0xBU_DEGERI"
    ):
        _fail(
            "INSTITUTION_PRIVATE_KEY tanımlı değil. .env dosyasında dağıtımı "
            "yapacak kurum cüzdanının özel anahtarını tanımlayın."
        )
    if not ABI_PATH.exists() or not BYTECODE_PATH.exists():
        _fail(
            f"ABI ({ABI_PATH}) veya bytecode ({BYTECODE_PATH}) dosyası bulunamadı. "
            "Sözleşme önceden derlenmiş olmalı."
        )

    abi = json.loads(ABI_PATH.read_text(encoding="utf-8"))
    bytecode = BYTECODE_PATH.read_text(encoding="utf-8").strip()
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode

    # --- 2) Ağa bağlan ------------------------------------------------------
    print(f"\nRPC uç noktasına bağlanılıyor: {config.POLYGON_RPC_URL}")
    w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        _fail(f"Polygon RPC uç noktasına bağlanılamadı: {config.POLYGON_RPC_URL}")

    account = Account.from_key(config.INSTITUTION_PRIVATE_KEY)
    chain_id = config.POLYGON_CHAIN_ID
    balance_wei = w3.eth.get_balance(account.address)
    balance_matic = w3.from_wei(balance_wei, "ether")
    print(f"Dağıtımı yapan cüzdan : {account.address}")
    print(f"Bakiye                : {balance_matic} MATIC/POL")
    print(f"Zincir kimliği (ID)   : {chain_id}")

    if balance_wei == 0:
        _fail(
            "Cüzdan bakiyesi 0. Dağıtım işlemi için gas ücretini karşılayacak "
            "miktarda testnet MATIC/POL gereklidir (Amoy faucet kullanabilirsiniz)."
        )

    # --- 3) Dağıtım işlemini hazırla, imzala ve gönder ----------------------
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    max_priority = w3.to_wei(config.MAX_PRIORITY_FEE_GWEI, "gwei")
    max_fee = w3.to_wei(config.MAX_FEE_GWEI, "gwei")

    tx = Contract.constructor().build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "chainId": chain_id,
            "maxPriorityFeePerGas": max_priority,
            "maxFeePerGas": max_fee,
        }
    )

    print("\nİşlem imzalanıyor ve ağa gönderiliyor...")
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hash_hex = tx_hash.hex()
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex
    print(f"İşlem gönderildi      : {tx_hash_hex}")
    print("Onay (mining) bekleniyor, bu biraz zaman alabilir...")

    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    except TimeExhausted:
        _fail(
            f"İşlem 180 saniye içinde onaylanmadı. TX Hash: {tx_hash_hex} adresinden "
            "Polygonscan üzerinden durumu manuel kontrol edebilirsiniz."
        )
        return

    if receipt.status != 1 or not receipt.contractAddress:
        _fail(f"Dağıtım işlemi başarısız oldu (revert). TX Hash: {tx_hash_hex}")

    contract_address = receipt.contractAddress
    print("\n" + "=" * 70)
    print("DAĞITIM BAŞARILI")
    print("=" * 70)
    print(f"Sözleşme adresi : {contract_address}")
    print(f"Blok numarası   : {receipt.blockNumber}")
    print(f"Kullanılan gas  : {receipt.gasUsed}")
    print(
        "\nLütfen .env dosyanızdaki CONTRACT_ADDRESS değişkenini şu değerle "
        "güncelleyin:\n"
    )
    print(f"  CONTRACT_ADDRESS={contract_address}\n")

    # Kolay erişim için dağıtım bilgisini bir json dosyasına da yaz.
    artifact_path = CONTRACTS_DIR / "deployment_info.json"
    artifact_path.write_text(
        json.dumps(
            {
                "contract_address": contract_address,
                "deployer_address": account.address,
                "chain_id": chain_id,
                "tx_hash": tx_hash_hex,
                "block_number": receipt.blockNumber,
                "deployed_at_unix": int(time.time()),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"Dağıtım bilgisi şuraya da kaydedildi: {artifact_path}")


if __name__ == "__main__":
    main()
