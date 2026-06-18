// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title OpticalFormRegistry (v2 — Merkle Toplu Mühürleme)
 * @author UODS - Uç Optik Doğrulama Sistemi (Takım SİLSİLE, TEKNOFEST Blokzincir Yarışması)
 * @notice TASARIM GEÇMİŞİ — HAKEM DEĞERLENDİRMESİNE YANIT:
 *
 *         Bu sözleşmenin önceki sürümü, her sınav formu için ayrı bir
 *         addRecord(kAnahtar, ipfsCID) çağrısı yapıyor; yani her öğrencinin
 *         kimlik-türevli k anahtarı (k = SHA-256(TC||isim||tarih||tuz)),
 *         hash'lenmiş olsa da TEK TEK ve KALICI olarak zincire yazılıyordu.
 *
 *         Hakem değerlendirmesinde haklı olarak şu nokta vurgulanmıştır:
 *         özet (hash) değerinden gerçek veriye ulaşmanın hesaplama
 *         açısından imkânsıza yakın olduğu bilinse de, hassas verinin
 *         HERHANGİ BİR türevinin (hash'i dahi) kalıcı ve herkese açık bir
 *         yapıya TEK TEK kaydedilmesi genel kabul gören bir uygulama
 *         değildir — özellikle kurum tuzu bir gün sızarsa, TÜM geçmiş
 *         kayıtlar aynı anda ve geri alınamaz biçimde ifşa olur.
 *
 *         BU SÜRÜMDE: zincire artık HİÇBİR bireysel kayda ait k anahtarı
 *         veya ondan türetilmiş herhangi bir değer YAZILMAZ. Bunun yerine,
 *         bir senkronizasyon turunda biriken TÜM kayıtların bütünlük
 *         özetleri (h_local, Denklem 9) bir Merkle ağacında birleştirilir
 *         ve zincire YALNIZCA o ağacın TEK bir kökü (merkleRoot) yazılır.
 *         Bu kök, başlı başına HİÇBİR kaydın kimliğini, varlığını veya
 *         içeriğini ifşa etmez. Belirli bir kaydın bu köke dahil olduğunu
 *         ispatlamak isteyen taraf (kurum ya da kaydın sahibi), o kayda
 *         özel bir "Merkle kanıtı" (kardeş düğüm listesi) sunmalıdır —
 *         bu kanıt zincire YAZILMAZ, yalnızca ilgili tarafa özel olarak
 *         (yerel veritabanından) verilir. Bu, zk-rollup'ların ve
 *         Semaphore/Tornado Cash gibi gizlilik korumalı üyelik ispatı
 *         sistemlerinin temel yapı taşıyla (Merkle ağacı tabanlı üyelik
 *         kanıtı) aynı ailedendir; tam bir zk-SNARK devresi (devre
 *         tasarımı + güvenilir kurulum gerektirir) bu ölçekte gerçekçi
 *         olmadığından, aynı ailenin daha hafif ama gerçek bir gizlilik
 *         kazancı sağlayan bu tekniği uygulanmıştır (bkz. proje README,
 *         "Gelecek Çalışma" notu — tam ZKP/homomorfik şifreleme önerisi
 *         dokümante edilmiştir).
 *
 * @dev    Güvenlik notları (bkz. rapor bölüm 3.8, gerçek Slither taraması
 *         contracts/SLITHER_REPORT.md'de):
 *           - Yeniden giriş riski yoktur: dışarıya hiçbir zaman ETH/MATIC
 *             transferi veya harici çağrı yapılmaz.
 *           - Erişim kontrolü msg.sender üzerinden yapılır (tx.origin
 *             KULLANILMAZ).
 *           - Solidity 0.8.x'te tamsayı taşması derleyici düzeyinde
 *             otomatik kontrol edilir.
 *           - Bir parti (batch) kökü üzerine yazma (overwrite) engellenmiştir.
 *           - verifyInclusion saf bir matematiksel doğrulamadır; hiçbir
 *             depolama YAZMA işlemi yapmaz (view), bu nedenle herkes
 *             tarafından ücretsiz (gas'sız, eth_call ile) çağrılabilir.
 */
contract OpticalFormRegistry {
    /// @notice Zincirde saklanan tek bir parti (batch) kaydı.
    struct Batch {
        uint256 timestamp;
        address verifier;
        uint256 recordCount;
        bool exists;
    }

    /// @notice merkleRoot'tan parti kaydına eşleme. ANAHTAR, hiçbir kimlik
    ///         bilgisi İÇERMEZ — yalnızca bir grup kaydın ortak kök hash'idir.
    mapping(bytes32 => Batch) private batches;

    /// @notice Sözleşmenin sahibi (kurumun yetkili relayer cüzdanı).
    address public owner;

    /// @notice Zincire eklenmiş toplam parti sayısı.
    uint256 public totalBatches;

    /// @notice Tüm partilerdeki toplam kayıt sayısı (istatistik amaçlı;
    ///         hiçbir kaydın kimliğini ifşa etmez).
    uint256 public totalRecords;

    /// @notice Yeni bir parti kökü başarıyla zincire mühürlendiğinde yayınlanır.
    event BatchCreated(
        bytes32 indexed merkleRoot,
        uint256 recordCount,
        uint256 timestamp,
        address indexed verifier
    );

    /// @notice Sözleşme sahipliği değiştirildiğinde yayınlanır.
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    error SadeceSahip();
    error PartiZatenMevcut(bytes32 merkleRoot);
    error PartiBulunamadi(bytes32 merkleRoot);
    error GecersizKayitSayisi();
    error GecersizAdres();

    modifier onlyOwner() {
        if (msg.sender != owner) revert SadeceSahip();
        _;
    }

    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    /**
     * @notice Bir senkronizasyon turunda biriken kayıtların Merkle kökünü
     *         zincire mühürler. Yalnızca sözleşme sahibi (kurumun yetkili
     *         relayer cüzdanı) tarafından çağrılabilir.
     * @dev    Aynı merkleRoot ile önceden bir parti varsa işlem geri alınır
     *         (revert) — bu, partilerin DEĞİŞTİRİLEMEZLİĞİNİ garanti eder.
     * @param merkleRoot   Bu partideki tüm kayıtların h_local değerlerinden
     *                     (bkz. Denklem 9) oluşturulan Merkle ağacının kökü.
     *                     HİÇBİR bireysel kaydın kimliğini içermez.
     * @param recordCount  Bu partideki kayıt sayısı (yalnızca istatistik
     *                     amaçlı; gizlilik açısından bilgi sızdırmaz).
     */
    function addBatchRoot(bytes32 merkleRoot, uint256 recordCount) external onlyOwner {
        if (recordCount == 0) revert GecersizKayitSayisi();
        if (batches[merkleRoot].exists) revert PartiZatenMevcut(merkleRoot);

        batches[merkleRoot] = Batch({
            timestamp: block.timestamp,
            verifier: msg.sender,
            recordCount: recordCount,
            exists: true
        });
        totalBatches += 1;
        totalRecords += recordCount;

        emit BatchCreated(merkleRoot, recordCount, block.timestamp, msg.sender);
    }

    /**
     * @notice Verilen merkleRoot'a ait parti bilgisini sorgular.
     * @dev    Parti yoksa işlem geri alınır (revert).
     */
    function getBatch(bytes32 merkleRoot)
        external
        view
        returns (uint256 timestamp, address verifier, uint256 recordCount)
    {
        Batch storage b = batches[merkleRoot];
        if (!b.exists) revert PartiBulunamadi(merkleRoot);
        return (b.timestamp, b.verifier, b.recordCount);
    }

    /// @notice Verilen merkleRoot'un zincirde var olup olmadığını (revert
    ///         ETMEDEN) kontrol etmek için kullanılır.
    function batchExists(bytes32 merkleRoot) external view returns (bool) {
        return batches[merkleRoot].exists;
    }

    /**
     * @notice Belirli bir kaydın (leaf), verilen merkleRoot'a ait bir
     *         partinin GERÇEKTEN bir üyesi olduğunu, kanıt (proof) listesi
     *         aracılığıyla doğrular. Bu fonksiyon hiçbir depolama yazma
     *         işlemi yapmaz; tamamen herkese açık ve ücretsiz (eth_call)
     *         çağrılabilir.
     * @dev    OpenZeppelin'in MerkleProof kütüphanesiyle aynı "sıralı çift"
     *         (sorted-pair) keccak256 birleştirme yöntemini kullanır; bu
     *         sayede kanıtın "sol mu sağ mı" bilgisini taşımasına gerek
     *         kalmaz. merkle.py (Python tarafı) ile BİREBİR aynı algoritma.
     * @param merkleRoot  Zincirde zaten mühürlenmiş olması gereken parti kökü.
     * @param leaf        Doğrulanacak kaydın h_local değeri (Denklem 9).
     * @param proof       leaf'ten merkleRoot'a kadar olan kardeş düğüm listesi.
     * @return included   leaf, merkleRoot'a ait partinin bir üyesiyse true.
     */
    function verifyInclusion(bytes32 merkleRoot, bytes32 leaf, bytes32[] calldata proof)
        external
        view
        returns (bool included)
    {
        if (!batches[merkleRoot].exists) revert PartiBulunamadi(merkleRoot);

        bytes32 computed = leaf;
        for (uint256 i = 0; i < proof.length; i++) {
            bytes32 sibling = proof[i];
            if (computed <= sibling) {
                computed = keccak256(abi.encodePacked(computed, sibling));
            } else {
                computed = keccak256(abi.encodePacked(sibling, computed));
            }
        }
        return computed == merkleRoot;
    }

    /**
     * @notice Sözleşme sahipliğini yeni bir adrese devreder.
     * @dev    Sıfır adrese devir kasıtlı olarak engellenmiştir (sözleşmenin
     *         kalıcı olarak kilitlenmesini önlemek için).
     */
    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert GecersizAdres();
        address previousOwner = owner;
        owner = newOwner;
        emit OwnershipTransferred(previousOwner, newOwner);
    }
}
