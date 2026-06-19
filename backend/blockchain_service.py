# -*- coding: utf-8 -*-
"""
blockchain_service.py
======================
Polygon (PoS / Amoy Testnet) ağına Web3.py [28] üzerinden bağlanan,
OpticalFormRegistry akıllı sözleşmesiyle (v2 — Merkle toplu mühürleme)
etkileşen blokzincir servisi. Rapor bölüm 3.7'deki "Blokzincir Entegrasyonu
ve Dijital Noterleme" mimarisini, hakem değerlendirmesi doğrultusunda
güncellenmiş gizlilik tasarımıyla uygular.

HAKEM DEĞERLENDİRMESİNE YANIT (özet):
  Önceki tasarımda her kayıt için ayrı bir addRecord(kAnahtar, ipfsCID)
  çağrısı yapılıyordu; yani her öğrencinin kimlik-türevli k anahtarı
  (hash'lenmiş olsa da) tek tek ve kalıcı olarak herkese açık zincire
  yazılıyordu. Yeni tasarımda zincire artık HİÇBİR bireysel kayda ait
  değer yazılmaz; bir senkronizasyon turundaki tüm kayıtların h_local
  değerleri (Denklem 9) bir Merkle ağacında birleştirilir ve zincire
  YALNIZCA tek bir kök (merkleRoot) yazılır. Bireysel üyelik ispatı,
  zincire yazılmayan, yalnızca ilgili tarafa verilen bir Merkle kanıtıyla
  (verifyInclusion) yapılır. Ayrıntı: merkle.py ve contracts/OpticalFormRegistry.sol.

Bu modül, raporda tanımlanan "kurumsal aktarıcı (relayer)" modelini
gerçekleştirir (bkz. 3.7.4): son kullanıcılar hiçbir zaman kendi
cüzdanlarıyla etkileşmez; tüm imzalama işlemleri sunucu tarafında,
INSTITUTION_PRIVATE_KEY ile yerel olarak yapılır (3.7.5, Adım 3).

Akış (3.7.5):
  1) Ağa Bağlanma      -> get_web3()
  2) Veri Paketleme    -> build_add_batch_transaction()
  3) Dijital İmzalama  -> sign_transaction()
  4) Ağa Gönderme/Onay -> send_batch_to_chain() (orkestrasyon)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.contract.contract import Contract
from web3.exceptions import ContractLogicError, TimeExhausted

from config import Config

# Yerel EVM (eth-tester) ortamında, view fonksiyonlarının revert'i
# ContractLogicError yerine eth_tester.exceptions.TransactionFailed olarak
# gelir. Her iki ortamda da (yerel test + canlı ağ) tutarlı davranmak için
# her ikisi de yakalanır. eth-tester kurulu değilse (yalnızca canlı ağ
# senaryosu) sadece ContractLogicError kullanılır.
try:  # pragma: no cover - ortama bağlı
    from eth_tester.exceptions import TransactionFailed as _EthTesterTxFailed

    _REVERT_EXCEPTIONS: tuple = (ContractLogicError, _EthTesterTxFailed)
except ImportError:  # pragma: no cover
    _REVERT_EXCEPTIONS = (ContractLogicError,)


class BlockchainServiceError(Exception):
    """Blokzincir servisindeki hatalar için temel istisna sınıfı."""


class NetworkConnectionError(BlockchainServiceError):
    """Polygon RPC uç noktasına bağlanılamadığında fırlatılır."""


class TransactionFailedError(BlockchainServiceError):
    """İşlem gönderildi ancak ağ tarafından reddedildi ya da revert oldu."""


class BatchAlreadyExistsError(BlockchainServiceError):
    """Aynı Merkle köküyle zincirde zaten bir parti varsa fırlatılır."""


class BatchNotFoundError(BlockchainServiceError):
    """getBatch sorgulanan Merkle kökü zincirde bulunamadığında fırlatılır."""


def _resolve_abi_path() -> Path:
    """ABI dosyasını birden fazla olası konumda arar; hem backend düzeni
    (bu dosyanın yanındaki contracts/) hem de bağımsız blokchain paketi
    düzeni (bir üst dizindeki contracts/) desteklenir."""
    here = Path(__file__).resolve().parent
    candidates = [
        here / "contracts" / "OpticalFormRegistry.abi.json",
        here.parent / "contracts" / "OpticalFormRegistry.abi.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


_ABI_PATH = _resolve_abi_path()


def _load_abi() -> list:
    if not _ABI_PATH.exists():
        raise BlockchainServiceError(
            f"Akıllı sözleşme ABI dosyası bulunamadı: {_ABI_PATH}. "
            "Lütfen 'contracts/OpticalFormRegistry.abi.json' dosyasının mevcut "
            "olduğundan emin olun (solc ile derlenmiş ya da elle hazırlanmış)."
        )
    with open(_ABI_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_web3(config: Config) -> Web3:
    """
    Config.POLYGON_RPC_URL üzerinden Polygon ağına (Alchemy uç noktası [28])
    bir Web3 bağlantısı kurar ve bağlantıyı doğrular.
    """
    config.validate_for_blockchain()
    w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        raise NetworkConnectionError(
            f"Polygon RPC uç noktasına bağlanılamadı: {config.POLYGON_RPC_URL}"
        )
    return w3


def get_institution_account(config: Config) -> LocalAccount:
    """
    .env içindeki INSTITUTION_PRIVATE_KEY değerinden, kurumun yetkili
    relayer cüzdan hesabını (LocalAccount) türetir. Bu özel anahtar HİÇBİR
    ZAMAN kaynak kodda sabit (hard-coded) yer almaz; yalnızca çalışma
    zamanında python-dotenv ile bellekteki ortam değişkeninden okunur [15].
    """
    config.validate_for_blockchain()
    return Account.from_key(config.INSTITUTION_PRIVATE_KEY)


def get_contract(w3: Web3, config: Config) -> Contract:
    """Dağıtılmış OpticalFormRegistry sözleşmesinin Web3 Contract nesnesini döndürür."""
    config.validate_for_blockchain()
    abi = _load_abi()
    address = Web3.to_checksum_address(config.CONTRACT_ADDRESS)
    return w3.eth.contract(address=address, abi=abi)


@dataclass(frozen=True)
class ChainReceipt:
    """Zincire başarıyla yazılan bir parti işleminin özet bilgisi (rapor 3.7.6 — "makbuz")."""

    tx_hash: str
    block_number: int
    merkle_root_hex: str
    record_count: int
    status: int  # 1 = başarılı, 0 = revert


def _normalize_root(merkle_root_bytes32: bytes) -> bytes:
    if not isinstance(merkle_root_bytes32, (bytes, bytearray)) or len(merkle_root_bytes32) != 32:
        raise BlockchainServiceError(
            f"merkleRoot tam olarak 32 bayt olmalıdır, alınan: {merkle_root_bytes32!r}"
        )
    return bytes(merkle_root_bytes32)


def build_add_batch_transaction(
    w3: Web3,
    contract: Contract,
    account: LocalAccount,
    merkle_root_bytes32: bytes,
    record_count: int,
    config: Config,
) -> dict:
    """
    addBatchRoot(merkleRoot, recordCount) çağrısı için EIP-1559 (type-2) ham
    işlem sözlüğünü hazırlar. Nonce, ağdaki bekleyen (pending) işlemler dahil
    olacak şekilde belirlenir.
    """
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    chain_id = config.POLYGON_CHAIN_ID

    max_priority = w3.to_wei(config.MAX_PRIORITY_FEE_GWEI, "gwei")
    max_fee = w3.to_wei(config.MAX_FEE_GWEI, "gwei")

    tx = contract.functions.addBatchRoot(
        merkle_root_bytes32, int(record_count)
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "chainId": chain_id,
            "maxPriorityFeePerGas": max_priority,
            "maxFeePerGas": max_fee,
        }
    )
    return tx


def sign_transaction(account: LocalAccount, tx: dict):
    """
    İşlem paketini kurumun özel anahtarıyla YEREL olarak imzalar
    (rapor 3.7.5, Adım 3 — ECDSA imza [29]). Özel anahtar bu fonksiyon
    dışına asla çıkmaz; ağa yalnızca imzalı ham (raw) işlem gönderilir.
    """
    return account.sign_transaction(tx)


def send_batch_to_chain(
    config: Config,
    merkle_root_bytes32: bytes,
    record_count: int,
    wait_for_receipt: bool = True,
    timeout_seconds: int = 180,
) -> ChainReceipt:
    """
    Bir partinin Merkle kökünü zincire yazan tam uçtan uca orkestrasyon
    (rapor 3.7.5, Adım 1-4): 1) ağa bağlan, 2) işlemi paketle, 3) imzala,
    4) yayınla ve (isteğe bağlı olarak) onay makbuzunu bekle.

    Aynı Merkle köküyle zincirde önceden bir parti varsa, sözleşme
    PartiZatenMevcut hatasıyla revert eder; bu durum burada
    BatchAlreadyExistsError olarak yeniden fırlatılır.
    """
    root = _normalize_root(merkle_root_bytes32)
    if record_count <= 0:
        raise BlockchainServiceError("record_count pozitif olmalıdır.")

    w3 = get_web3(config)
    account = get_institution_account(config)
    contract = get_contract(w3, config)

    # Aynı parti zaten zincirde mi diye önceden kontrol ederek gereksiz
    # gas harcamasının (ve revert'in) önüne geçilir.
    try:
        already_exists = contract.functions.batchExists(root).call()
    except Exception as exc:  # noqa: BLE001 - RPC geçici hatalarını da kapsar
        raise NetworkConnectionError(f"batchExists sorgusu başarısız: {exc}") from exc

    if already_exists:
        raise BatchAlreadyExistsError(
            f"Merkle kökü zincirde zaten kayıtlı: 0x{root.hex()}"
        )

    tx = build_add_batch_transaction(w3, contract, account, root, record_count, config)
    signed = sign_transaction(account, tx)

    try:
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    except ContractLogicError as exc:
        raise TransactionFailedError(f"İşlem sözleşme tarafından reddedildi: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise TransactionFailedError(f"İşlem ağa gönderilemedi: {exc}") from exc

    tx_hash_hex = tx_hash.hex()
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex

    if not wait_for_receipt:
        return ChainReceipt(
            tx_hash=tx_hash_hex,
            block_number=-1,
            merkle_root_hex=f"0x{root.hex()}",
            record_count=record_count,
            status=-1,
        )

    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_seconds)
    except TimeExhausted as exc:
        raise TransactionFailedError(
            f"İşlem onayı zaman aşımına uğradı (TX Hash: {tx_hash_hex}): {exc}"
        ) from exc

    if receipt.status != 1:
        raise TransactionFailedError(
            f"İşlem zincire yazıldı ancak başarısız oldu (revert), TX Hash: {tx_hash_hex}"
        )

    return ChainReceipt(
        tx_hash=tx_hash_hex,
        block_number=receipt.blockNumber,
        merkle_root_hex=f"0x{root.hex()}",
        record_count=record_count,
        status=receipt.status,
    )


@dataclass(frozen=True)
class OnChainBatch:
    """getBatch() sorgusunun sonucu."""

    timestamp: int
    verifier_address: str
    record_count: int


def get_batch_from_chain(config: Config, merkle_root_bytes32: bytes) -> OnChainBatch:
    """
    Verilen Merkle köküne ait parti bilgisini (zaman damgası, doğrulayıcı
    kurum adresi, kayıt sayısı) zincirden sorgular. Parti yoksa
    BatchNotFoundError fırlatılır.
    """
    root = _normalize_root(merkle_root_bytes32)
    w3 = get_web3(config)
    contract = get_contract(w3, config)
    try:
        timestamp, verifier, record_count = contract.functions.getBatch(root).call()
    except _REVERT_EXCEPTIONS as exc:
        raise BatchNotFoundError(
            f"Merkle köküne ait parti zincirde bulunamadı: 0x{root.hex()}"
        ) from exc
    return OnChainBatch(
        timestamp=timestamp, verifier_address=verifier, record_count=record_count
    )


def batch_exists_on_chain(config: Config, merkle_root_bytes32: bytes) -> bool:
    """Revert etmeden, sadece var/yok bilgisini döndüren hafif sorgu."""
    root = _normalize_root(merkle_root_bytes32)
    w3 = get_web3(config)
    contract = get_contract(w3, config)
    return bool(contract.functions.batchExists(root).call())


def verify_inclusion_on_chain(
    config: Config,
    merkle_root_bytes32: bytes,
    leaf_bytes32: bytes,
    proof: Sequence[bytes],
) -> bool:
    """
    Belirli bir kaydın (leaf = h_local), verilen Merkle köküne ait bir
    partinin GERÇEKTEN üyesi olduğunu, kanıt (proof) listesi aracılığıyla
    ZİNCİR ÜZERİNDE doğrular. Bu, yetkili/nihai doğrulamadır (yerel
    merkle.verify_proof yalnızca hızlı ön-kontrol içindir).

    Parti zincirde yoksa BatchNotFoundError fırlatılır.
    """
    root = _normalize_root(merkle_root_bytes32)
    leaf = _normalize_root(leaf_bytes32)  # leaf de 32 bayt olmalı; aynı doğrulama
    proof_list: List[bytes] = []
    for entry in proof:
        proof_list.append(_normalize_root(entry))

    w3 = get_web3(config)
    contract = get_contract(w3, config)
    try:
        return bool(
            contract.functions.verifyInclusion(root, leaf, proof_list).call()
        )
    except _REVERT_EXCEPTIONS as exc:
        raise BatchNotFoundError(
            f"Merkle köküne ait parti zincirde bulunamadı (verifyInclusion): 0x{root.hex()}"
        ) from exc


def get_polygonscan_tx_url(config: Config, tx_hash: str) -> str:
    """
    Verilen TX Hash için ilgili ağın (mainnet/Amoy testnet) Polygonscan
    bağımsız doğrulama bağlantısını üretir (rapor 3.7.5, Adım 4).
    """
    if config.POLYGON_CHAIN_ID == 137:
        base = "https://polygonscan.com/tx/"
    else:
        # 80002 = Amoy Testnet ve diğer test ağları için varsayılan.
        base = "https://amoy.polygonscan.com/tx/"
    return base + tx_hash
