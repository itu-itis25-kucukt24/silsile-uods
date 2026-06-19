# -*- coding: utf-8 -*-
"""
tests/test_blockchain_service.py
==================================
blockchain_service.py'nin batch/Merkle fonksiyonlarını, GERÇEK bir yerel
EVM (eth-tester / py-evm) üzerinde, FİİLEN DERLENMİŞ sözleşmeyi dağıtarak
uçtan uca test eder. Bu, önceki sürümlere göre önemli bir iyileştirmedir:
gerçek Polygon erişimi olmadan da artık zincir mantığı gerçek EVM
yürütmesiyle doğrulanabilmektedir (sadece Python simülasyonu değil).

eth-tester kurulu değilse testler atlanır (skip), böylece minimum kurulumda
da test paketi çalışır.

Çalıştırma: pytest tests/test_blockchain_service.py -v
"""
import dataclasses
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

pytest.importorskip("eth_tester", reason="eth-tester kurulu değil; EVM entegrasyon testleri atlanıyor")

from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

import blockchain_service
import merkle
from config import config as base_config

_ABI_PATH = Path(__file__).resolve().parent.parent / "contracts" / "OpticalFormRegistry.abi.json"
_BYTECODE_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "OpticalFormRegistry.bytecode.txt"
)


def _leaf(label: str) -> bytes:
    return hashlib.sha256(label.encode("utf-8")).digest()


@pytest.fixture
def evm_setup(monkeypatch):
    """Yerel EVM kurar, sözleşmeyi dağıtır ve blockchain_service'in get_web3 /
    get_contract / get_institution_account fonksiyonlarını bu yerel ortama
    yönlendirir (monkeypatch). Böylece servis kodunun gerçek fonksiyonları
    test edilir, sadece ağ katmanı yerel EVM'e bağlanır."""
    w3 = Web3(EthereumTesterProvider())
    owner = w3.eth.accounts[0]
    w3.eth.default_account = owner

    with open(_ABI_PATH, encoding="utf-8") as f:
        abi = json.load(f)
    with open(_BYTECODE_PATH, encoding="utf-8") as f:
        bytecode = f.read().strip()

    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx_hash = Contract.constructor().transact()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    contract = w3.eth.contract(address=receipt.contractAddress, abi=abi)

    cfg = dataclasses.replace(
        base_config,
        POLYGON_RPC_URL="http://local-evm",
        CONTRACT_ADDRESS=receipt.contractAddress,
        INSTITUTION_PRIVATE_KEY="0x" + "11" * 32,
        POLYGON_CHAIN_ID=80002,
    )

    # Servis fonksiyonlarını yerel EVM'e yönlendir. send_batch_to_chain gibi
    # transact eden fonksiyonlar için, contract'ın default_account'u owner
    # olduğundan, doğrudan o hesapla işlem yapılır.
    monkeypatch.setattr(blockchain_service, "get_web3", lambda config: w3)
    monkeypatch.setattr(blockchain_service, "get_contract", lambda w3_, config: contract)

    class _FakeAccount:
        address = owner

    monkeypatch.setattr(
        blockchain_service, "get_institution_account", lambda config: _FakeAccount()
    )

    return cfg, w3, contract, owner


class TestBatchLifecycleOnRealEVM:
    def _send_batch_directly(self, contract, owner, root, count):
        """send_batch_to_chain, ham imzalı işlem üretip send_raw_transaction
        çağırır; eth-tester'da imzalama akışını taklit etmek yerine, burada
        sözleşmeyi doğrudan owner hesabıyla çağırarak partiyi zincire yazarız
        (test, get_batch/verify_inclusion OKUMA fonksiyonlarına odaklanır)."""
        tx = contract.functions.addBatchRoot(root, count).transact({"from": owner})
        contract.w3.eth.wait_for_transaction_receipt(tx)

    def test_get_batch_and_verify_inclusion(self, evm_setup):
        cfg, w3, contract, owner = evm_setup
        leaves = [_leaf(f"kayit-{i}") for i in range(8)]
        tree = merkle.build_merkle_tree(leaves)

        self._send_batch_directly(contract, owner, tree.root, len(leaves))

        # batch_exists_on_chain
        assert blockchain_service.batch_exists_on_chain(cfg, tree.root) is True

        # get_batch_from_chain
        batch = blockchain_service.get_batch_from_chain(cfg, tree.root)
        assert batch.record_count == 8
        assert batch.verifier_address == owner
        assert batch.timestamp > 0

        # verify_inclusion_on_chain: her yaprak için gerçek zincir doğrulaması
        for i, leaf in enumerate(leaves):
            proof = merkle.generate_proof(tree, i)
            assert blockchain_service.verify_inclusion_on_chain(
                cfg, tree.root, leaf, proof
            ) is True

    def test_verify_inclusion_rejects_wrong_leaf(self, evm_setup):
        cfg, w3, contract, owner = evm_setup
        leaves = [_leaf(f"x-{i}") for i in range(5)]
        tree = merkle.build_merkle_tree(leaves)
        self._send_batch_directly(contract, owner, tree.root, len(leaves))

        wrong = _leaf("hic-yok")
        proof = merkle.generate_proof(tree, 0)
        assert blockchain_service.verify_inclusion_on_chain(
            cfg, tree.root, wrong, proof
        ) is False

    def test_get_batch_nonexistent_raises(self, evm_setup):
        cfg, w3, contract, owner = evm_setup
        with pytest.raises(blockchain_service.BatchNotFoundError):
            blockchain_service.get_batch_from_chain(cfg, _leaf("hic-eklenmemis"))

    def test_batch_exists_false_for_unknown_root(self, evm_setup):
        cfg, w3, contract, owner = evm_setup
        assert blockchain_service.batch_exists_on_chain(cfg, _leaf("bilinmeyen")) is False

    def test_verify_inclusion_nonexistent_batch_raises(self, evm_setup):
        cfg, w3, contract, owner = evm_setup
        with pytest.raises(blockchain_service.BatchNotFoundError):
            blockchain_service.verify_inclusion_on_chain(
                cfg, _leaf("yok-parti"), _leaf("yaprak"), []
            )


class TestOddLeafCountsOnRealEVM:
    @pytest.mark.parametrize("n", [1, 2, 3, 5, 7, 9, 16, 33])
    def test_all_leaves_verify_for_various_sizes(self, evm_setup, n):
        cfg, w3, contract, owner = evm_setup
        leaves = [_leaf(f"n{n}-{i}") for i in range(n)]
        tree = merkle.build_merkle_tree(leaves)
        contract.functions.addBatchRoot(tree.root, n).transact({"from": owner})

        for i, leaf in enumerate(leaves):
            proof = merkle.generate_proof(tree, i)
            assert blockchain_service.verify_inclusion_on_chain(
                cfg, tree.root, leaf, proof
            ) is True


class TestNormalizationGuards:
    def test_non_32_byte_root_raises(self, evm_setup):
        cfg, w3, contract, owner = evm_setup
        with pytest.raises(blockchain_service.BlockchainServiceError):
            blockchain_service.batch_exists_on_chain(cfg, b"too-short")
