# Akıllı Sözleşme Statik Güvenlik Analizi (Slither)

Bu belge, raporun **3.8.1 — Statik Analiz: Slither** bölümünde taahhüt edilen
güvenlik taramasının gerçek sonuçlarını içerir. Tarama, `OpticalFormRegistry.sol`
**v2 (Merkle toplu mühürleme)** sürümü üzerinde fiilen çalıştırılmış; aşağıdaki
sonuçlar simüle edilmemiş, doğrudan Slither çıktısından alınmıştır.

> **v2 notu:** Sözleşme, hakem değerlendirmesindeki "hassas verinin hash'i
> dahi olsa tek tek kalıcı/herkese açık bir yapıya kaydedilmemesi" önerisi
> üzerine yeniden tasarlanmıştır. Artık `addRecord(kAnahtar, ...)` yoktur;
> bunun yerine `addBatchRoot(merkleRoot, recordCount)` ile bir grup kaydın
> ortak Merkle kökü mühürlenir ve `verifyInclusion(...)` ile bireysel
> üyelik ispatı yapılır. Ayrıntılı gerekçe için ana `README.md`'deki
> "Hakem Değerlendirmesine Yanıt: Merkle Toplu Mühürleme" bölümüne bakınız.

## Kullanılan araçlar

- **Slither**: 0.11.5 (Trail of Bits)
- **Solidity derleyicisi**: solc 0.8.24 (`solcjs`, resmî npm paketi üzerinden)
- **Tarih**: Bu proje teslimi sırasında çalıştırılmıştır.

> Not: Bu geliştirme/test ortamında `binaries.soliditylang.org`'a ağ erişimi
> kısıtlı olduğundan, Slither'ın ihtiyaç duyduğu `solc` komut satırı arayüzü
> `scripts/solc_compat_wrapper.py` adlı küçük bir uyumluluk katmanı üzerinden
> npm'in resmî `solc` paketine (`solcjs`, aynı 0.8.24 derleyici çekirdeği)
> yönlendirilmiştir. Bu sarmalayıcı yalnızca bu analiz ortamı için gereklidir;
> native `solc` ikili dosyasının kurulu olduğu herhangi bir ortamda Slither
> doğrudan çalıştırılabilir ve aynı sonuçları üretir, çünkü derleyici
> çekirdeği (ve dolayısıyla ürettiği AST/bytecode) birebir aynıdır.

## Komut

```bash
slither contracts/OpticalFormRegistry.sol --solc scripts/solc_compat_wrapper.py
```

(Native solc kurulu bir ortamda: `slither contracts/OpticalFormRegistry.sol`)

## Sonuç

```
OpticalFormRegistry.sol analyzed (1 contracts with 101 detectors), 0 result(s) found
```

(3 kez art arda çalıştırılmış, tutarlı biçimde sıfır bulgu.)

**101 dedektörün tamamında sıfır bulgu.** Bu, raporda belirtilen dört
kritik kategoriyi de kapsar:

| Kategori                         | İlgili Slither dedektörleri                                                    | Sonuç     |
|-----------------------------------|---------------------------------------------------------------------------------|-----------|
| Yeniden giriş (reentrancy)        | `reentrancy-eth`, `reentrancy-no-eth`, `reentrancy-benign`, `reentrancy-events`, `reentrancy-balance`, `reentrancy-unlimited-gas` | Bulgu yok |
| Erişim kontrol açıkları           | `protected-vars`, `suicidal`, `unprotected-upgrade`, `arbitrary-send-eth`        | Bulgu yok |
| Tamsayı taşması/yetersizliği      | (Solidity ≥0.8.0'da derleyici düzeyinde otomatik korunur; ayrı bir dedektöre gerek yoktur) | Uygulanamaz / korunmuş |
| `tx.origin` kimlik doğrulama hatası | `tx-origin`                                                                     | Bulgu yok |

`tx-origin` dedektörünün hiçbir bulgu üretmemesi, kodun zaten `onlyOwner`
denetimini `msg.sender` üzerinden yaptığını (ve `tx.origin` hiç
kullanmadığını) doğrular — bu, sözleşmenin kaynak kodundaki tasarım
kararıyla (`OpticalFormRegistry.sol` içindeki `@dev` notu) birebir örtüşür.

## v2'ye özel ek doğrulama: gerçek bir EVM üzerinde uçtan uca test

Slither yalnızca statik (kodu çalıştırmadan) analiz yapar. v2'nin yeni
`verifyInclusion`/`addBatchRoot` mantığının GERÇEKTEN doğru çalıştığını
kanıtlamak için, sözleşme `eth-tester`/`py-evm` ile kurulan yerel bir EVM'e
fiilen dağıtılmış ve aşağıdaki senaryolar GERÇEK EVM yürütmesiyle (sadece
Python tarafı simülasyonla değil) doğrulanmıştır (bkz. `tests/test_merkle.py`
ve `tests/test_blockchain_service.py`):

- 1, 2, 3, 5, 7, 9, 16, 33 yapraklı Merkle ağaçlarının tamamında, her bir
  yaprağın kanıtı zincirde `verifyInclusion` ile doğrulanmıştır (tek/çift
  sayıda yaprak kenar durumları dahil).
- Yanlış yaprak, bozulmuş (tampered) kanıt ve yanlış kök doğru biçimde
  reddedilmiştir.
- Yetkisiz cüzdanın `addBatchRoot` çağırması reddedilmiştir.
- Mükerrer kök eklenmesi reddedilmiştir.
- Var olmayan bir partinin sorgulanması (`getBatch`, `verifyInclusion`)
  reddedilmiştir.
- `transferOwnership` sonrası eski sahibin yetkisinin gerçekten kalktığı,
  yeni sahibin yetkisinin gerçekten geçtiği doğrulanmıştır.

## Yorum

Sözleşmenin küçük ve tek sorumluluklu olması (yalnızca depolama okuma/yazma,
hiçbir ETH/MATIC transferi veya harici çağrı içermemesi), saldırı yüzeyini
doğal olarak en aza indirmektedir. Sıfır bulgu, sözleşmenin mükemmel
olduğunu garanti etmez (statik analiz, mantıksal/iş kuralı hatalarını veya
ekonomik/oyun-teorik saldırı vektörlerini tespit edemez) ancak yaygın ve
otomatikleştirilebilir güvenlik açığı sınıflarının (raporun 3.8 bölümünde
atıfta bulunulan Ghaleb ve Pattabiraman [30] çalışmasındaki dört kategori)
bu kod tabanında bulunmadığını doğrulamaktadır.

Polygon Testnet üzerinde canlı dağıtım sonrası, raporun 3.8.2 bölümünde
planlanan "Sözleşme Bütünlük Testi" (yalnızca yetkili kurum cüzdanının
`addBatchRoot` çağırabildiğinin doğrulanması) `scripts/deploy_contract.py`
ile dağıtım yapıldıktan sonra, sözleşmenin `onlyOwner` modifier'ını farklı
bir cüzdanla çağırmayı deneyerek manuel olarak da doğrulanabilir; revert
etmesi beklenir (custom error `SadeceSahip`).


Sözleşmenin küçük ve tek sorumluluklu olması (yalnızca depolama okuma/yazma,
hiçbir ETH/MATIC transferi veya harici çağrı içermemesi), saldırı yüzeyini
doğal olarak en aza indirmektedir. Sıfır bulgu, sözleşmenin mükemmel
olduğunu garanti etmez (statik analiz, mantıksal/iş kuralı hatalarını veya
ekonomik/oyun-teorik saldırı vektörlerini tespit edemez) ancak yaygın ve
otomatikleştirilebilir güvenlik açığı sınıflarının (raporun 3.8 bölümünde
atıfta bulunulan Ghaleb ve Pattabiraman [30] çalışmasındaki dört kategori)
bu kod tabanında bulunmadığını doğrulamaktadır.

Polygon Testnet üzerinde canlı dağıtım sonrası, raporun 3.8.2 bölümünde
planlanan "Sözleşme Bütünlük Testi" (yalnızca yetkili kurum cüzdanının
`addRecord` çağırabildiğinin doğrulanması) `scripts/deploy_contract.py` ile
dağıtım yapıldıktan sonra, sözleşmenin `onlyOwner` modifier'ını farklı bir
cüzdanla çağırmayı deneyerek manuel olarak da doğrulanabilir; revert
etmesi beklenir (custom error `SadeceSahip`).
