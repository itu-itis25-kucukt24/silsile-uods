# -*- coding: utf-8 -*-
"""
merkle.py
==========
TOPLU (BATCH) MÜHÜRLEME — hakem değerlendirmesine doğrudan yanıt.

Önceki tasarımda her kayıt için ayrı bir `addRecord(kAnahtar, ipfsCID)`
çağrısı yapılıyor, yani her öğrencinin kimlik-türevli k anahtarı (hash'lenmiş
olsa da) TEK TEK ve KALICI olarak herkese açık zincire yazılıyordu. Hakem
notu haklı olarak şunu vurguluyor: özetten (hash) gerçek veriye ulaşmak
hesaplama açısından imkânsıza yakın olsa da, hassas verinin HERHANGİ BİR
türevinin dahi kalıcı/herkese açık bir yapıya tek tek kaydedilmesi genel
kabul gören bir uygulama değildir (özellikle KURUM_TUZU bir gün sızarsa,
TÜM geçmiş kayıtlar aynı anda ve geri alınamaz biçimde ifşa olur).

Bu modül, tam bir sıfır bilgi ispatı (zk-SNARK/zk-STARK) sistemi YERİNE
(devre tasarımı + güvenilir kurulum [trusted setup] gerektirdiğinden bu
ölçekte gerçekçi değildir — bkz. README "Gelecek Çalışma" notu), aynı
ailenin İSPAT TEKNİĞİNİ (Merkle ağacı tabanlı üyelik kanıtı — Semaphore,
Tornado Cash, zk-rollup'ların TÜMÜNÜN temel yapı taşı) kullanan, daha
hafif ama gerçek bir kriptografik gizlilik kazancı sağlayan bir yöntem
uygular:

  - Bir senkronizasyon turunda biriken TÜM kayıtların h_local değerleri
    (yaprak/leaf) bir Merkle ağacında birleştirilir.
  - Zincire YALNIZCA tek bir kök (merkleRoot) yazılır — bu kök, tek başına
    HİÇBİR kaydın kimliğini veya h_local değerini ifşa etmez.
  - Belirli bir kaydın bu köke dahil olduğunu ispatlamak için, o kaydın
    "Merkle kanıtı" (proof — kardeş düğüm hash'leri listesi) gerekir; bu
    kanıt yalnızca kaydın GERÇEK sahibine (kurum/öğrenci) verilir, ASLA
    toplu olarak yayınlanmaz/IPFS'e/zincire yazılmaz.
  - Sonuç: zincirde dolaşan TEK kamuya açık veri, anonim bir kök hash'tir;
    bireysel kayıtların var olduğunu ispatlamak hâlâ kriptografik olarak
    mümkündür (`OpticalFormRegistry.verifyInclusion`), ama bunu yapabilmek
    için kanıtı elinde bulunduran kişi olmak gerekir.

Yaprak/düğüm birleştirme fonksiyonu olarak keccak256 kullanılır (Solidity'nin
yerel EVM opcode'u — sha256'ya kıyasla gas maliyeti çok daha düşüktür).
Sıralı çift (sorted-pair) yöntemi: her seviyede iki düğüm, hangisi
küçükse önce olacak şekilde birleştirilir; bu sayede ispat sırasında
"sol mu sağ mı" bilgisi taşımaya gerek kalmaz (OpenZeppelin'in
MerkleProof kütüphanesiyle aynı yaklaşım).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from web3 import Web3


class MerkleError(Exception):
    """Merkle ağacı oluşturma/doğrulama hatalarında fırlatılır."""


def _combine(a: bytes, b: bytes) -> bytes:
    """İki 32 baytlık düğümü, küçük olan önce gelecek şekilde keccak256 ile birleştirir."""
    if a <= b:
        return Web3.keccak(a + b)
    return Web3.keccak(b + a)


@dataclass(frozen=True)
class MerkleTree:
    """Oluşturulmuş bir Merkle ağacı: tüm seviyeler bellekte tutulur
    (proof üretimi için gereklidir). leaves sırası, dışarıdan verilen
    sırayla AYNI tutulur — proof üretimi orijinal indekse göre yapılır."""

    leaves: Tuple[bytes, ...]
    levels: Tuple[Tuple[bytes, ...], ...]  # levels[0] == leaves, levels[-1] == (root,)

    @property
    def root(self) -> bytes:
        return self.levels[-1][0]

    @property
    def root_hex(self) -> str:
        return "0x" + self.root.hex()


def build_merkle_tree(leaves: Sequence[bytes]) -> MerkleTree:
    """
    Verilen yaprak listesinden bir Merkle ağacı kurar.

    Tek (odd) sayıda düğüm içeren bir seviyede, son düğüm bir ÜST seviyeye
    DEĞİŞTİRİLMEDEN taşınır (kendisiyle çiftlenmez) — bu, proof üretiminde
    "bu seviyede kardeşim yok" durumunun doğru ele alınmasını sağlar.

    En az 1 yaprak gereklidir; tek yapraklı bir ağacın kökü, o yaprağın
    kendisidir (hiçbir birleştirme adımı uygulanmaz).
    """
    if not leaves:
        raise MerkleError("Merkle ağacı en az bir yaprak içermelidir.")
    for leaf in leaves:
        if not isinstance(leaf, bytes) or len(leaf) != 32:
            raise MerkleError(f"Her yaprak tam olarak 32 bayt olmalıdır, alınan: {leaf!r}")

    levels: List[Tuple[bytes, ...]] = [tuple(leaves)]
    current = list(leaves)
    while len(current) > 1:
        next_level: List[bytes] = []
        i = 0
        while i < len(current):
            if i + 1 < len(current):
                next_level.append(_combine(current[i], current[i + 1]))
                i += 2
            else:
                # Tek kalan düğüm: değiştirilmeden bir üst seviyeye taşınır.
                next_level.append(current[i])
                i += 1
        levels.append(tuple(next_level))
        current = next_level

    return MerkleTree(leaves=tuple(leaves), levels=tuple(levels))


def generate_proof(tree: MerkleTree, leaf_index: int) -> List[bytes]:
    """
    `leaf_index` konumundaki yaprak için, köke kadar olan kardeş düğümler
    listesini (proof) döndürür. Bu liste, `verify_proof()` ile veya
    sözleşmenin `verifyInclusion()` fonksiyonuyla doğrulanabilir.
    """
    if not (0 <= leaf_index < len(tree.leaves)):
        raise MerkleError(f"Geçersiz yaprak indeksi: {leaf_index}")

    proof: List[bytes] = []
    index = leaf_index
    for level in tree.levels[:-1]:  # son seviye (kök) için kardeş aranmaz
        is_right_edge_odd_one_out = (index == len(level) - 1) and (len(level) % 2 == 1)
        if is_right_edge_odd_one_out:
            # Bu düğüm bu seviyede kardeşsiz taşınmış; bu seviye için proof'a
            # hiçbir şey eklenmez (build_merkle_tree ile birebir tutarlı).
            pass
        else:
            sibling_index = index + 1 if index % 2 == 0 else index - 1
            proof.append(level[sibling_index])
        index //= 2

    return proof


def verify_proof(leaf: bytes, proof: Sequence[bytes], root: bytes) -> bool:
    """
    `OpticalFormRegistry.verifyInclusion()` ile BİREBİR aynı algoritma
    (Python tarafında yerel/hızlı ön-doğrulama için; nihai/yetkili doğrulama
    her zaman zincir üzerinde `verifyInclusion()` çağrısıyla yapılmalıdır).
    """
    computed = leaf
    for sibling in proof:
        computed = _combine(computed, sibling)
    return computed == root


def hex_to_bytes32(value_hex: str) -> bytes:
    """'0x' önekli ya da öneksiz 64 hex karakterlik bir değeri 32 bayta çevirir."""
    cleaned = value_hex[2:] if value_hex.startswith("0x") else value_hex
    if len(cleaned) != 64:
        raise MerkleError(f"32 bayta (64 hex karakter) çevrilemedi: {value_hex!r}")
    return bytes.fromhex(cleaned)


def bytes32_to_hex(value: bytes) -> str:
    if len(value) != 32:
        raise MerkleError(f"32 bayt bekleniyordu, alınan uzunluk: {len(value)}")
    return "0x" + value.hex()
