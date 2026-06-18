# -*- coding: utf-8 -*-
"""
tests/test_merkle.py
======================
merkle.py için birim testleri: ağaç kurma, kanıt (proof) üretme/doğrulama,
tek/çift sayıda yaprak kenar durumları, bozulmuş veri tespiti.

Bu modülün GERÇEK EVM üzerinde (sadece Python simülasyonu değil)
doğrulanması için bkz. tests/test_blockchain_service.py.

Çalıştırma: pytest tests/test_merkle.py -v
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from merkle import (
    MerkleError,
    build_merkle_tree,
    bytes32_to_hex,
    generate_proof,
    hex_to_bytes32,
    verify_proof,
)


def _leaf(label: str) -> bytes:
    return hashlib.sha256(label.encode("utf-8")).digest()


class TestBuildMerkleTree:
    def test_empty_leaves_raises(self):
        with pytest.raises(MerkleError):
            build_merkle_tree([])

    def test_non_32_byte_leaf_raises(self):
        with pytest.raises(MerkleError):
            build_merkle_tree([b"too-short"])

    def test_single_leaf_root_equals_leaf(self):
        leaf = _leaf("tek-yaprak")
        tree = build_merkle_tree([leaf])
        assert tree.root == leaf

    def test_root_is_deterministic(self):
        leaves = [_leaf(f"x{i}") for i in range(10)]
        tree_a = build_merkle_tree(leaves)
        tree_b = build_merkle_tree(leaves)
        assert tree_a.root == tree_b.root

    def test_different_leaf_order_changes_root(self):
        leaves = [_leaf(f"y{i}") for i in range(5)]
        tree_a = build_merkle_tree(leaves)
        tree_b = build_merkle_tree(list(reversed(leaves)))
        assert tree_a.root != tree_b.root

    def test_root_hex_has_0x_prefix_and_64_chars(self):
        tree = build_merkle_tree([_leaf("a")])
        assert tree.root_hex.startswith("0x")
        assert len(tree.root_hex) == 66  # "0x" + 64 hex


class TestProofRoundtrip:
    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 33, 64, 100])
    def test_every_leaf_proof_verifies(self, n):
        leaves = [_leaf(f"kayit-{n}-{i}") for i in range(n)]
        tree = build_merkle_tree(leaves)
        for i, leaf in enumerate(leaves):
            proof = generate_proof(tree, i)
            assert verify_proof(leaf, proof, tree.root), (
                f"n={n} leaf_index={i} için doğrulama başarısız"
            )

    def test_invalid_leaf_index_raises(self):
        tree = build_merkle_tree([_leaf("a"), _leaf("b")])
        with pytest.raises(MerkleError):
            generate_proof(tree, 5)
        with pytest.raises(MerkleError):
            generate_proof(tree, -1)


class TestTamperDetection:
    def test_wrong_leaf_fails_verification(self):
        leaves = [_leaf(f"z{i}") for i in range(6)]
        tree = build_merkle_tree(leaves)
        proof = generate_proof(tree, 2)
        assert not verify_proof(_leaf("baska-bir-deger"), proof, tree.root)

    def test_tampered_proof_entry_fails_verification(self):
        leaves = [_leaf(f"w{i}") for i in range(6)]
        tree = build_merkle_tree(leaves)
        proof = list(generate_proof(tree, 1))
        if proof:
            proof[0] = _leaf("bozulmus-deger")
        assert not verify_proof(leaves[1], proof, tree.root)

    def test_proof_from_different_tree_fails(self):
        tree_a = build_merkle_tree([_leaf(f"a{i}") for i in range(8)])
        tree_b = build_merkle_tree([_leaf(f"b{i}") for i in range(8)])
        proof_from_a = generate_proof(tree_a, 3)
        assert not verify_proof(tree_a.leaves[3], proof_from_a, tree_b.root)

    def test_truncated_proof_fails_for_multi_leaf_tree(self):
        leaves = [_leaf(f"q{i}") for i in range(8)]
        tree = build_merkle_tree(leaves)
        proof = generate_proof(tree, 0)
        assert len(proof) > 0
        assert not verify_proof(leaves[0], proof[:-1], tree.root)


class TestHexConversion:
    def test_roundtrip_with_0x_prefix(self):
        leaf = _leaf("hex-test")
        hex_str = "0x" + leaf.hex()
        assert hex_to_bytes32(hex_str) == leaf
        assert bytes32_to_hex(leaf) == hex_str

    def test_roundtrip_without_0x_prefix(self):
        leaf = _leaf("hex-test-2")
        assert hex_to_bytes32(leaf.hex()) == leaf

    def test_wrong_length_raises(self):
        with pytest.raises(MerkleError):
            hex_to_bytes32("0xdeadbeef")
        with pytest.raises(MerkleError):
            bytes32_to_hex(b"short")
