(function () {
  "use strict";

  var card = document.getElementById("pending-card");
  if (!card) return;

  var recordId = card.getAttribute("data-record-id");
  var gatewayBase = card.getAttribute("data-gateway-base") || "";
  var POLL_MS = 4000;
  var timer = null;

  function setStep(name, state) {
    var el = document.querySelector('.process-step[data-step="' + name + '"]');
    if (!el) return;
    el.classList.remove("is-active", "is-done");
    var stateLabel = el.querySelector(".process-step__state");
    if (state === "done") {
      el.classList.add("is-done");
      if (stateLabel) stateLabel.textContent = "Tamam";
    } else if (state === "active") {
      el.classList.add("is-active");
      if (stateLabel) stateLabel.textContent = "Sürüyor";
    } else {
      if (stateLabel) stateLabel.textContent = "Bekliyor";
    }
  }

  function showSynced(payload) {
    var badge = document.getElementById("seal-badge");
    var caption = document.getElementById("seal-caption");
    var title = document.getElementById("status-title");
    var subtitle = document.getElementById("status-subtitle");

    if (badge) {
      badge.classList.remove("seal-badge--pending");
      badge.classList.add("seal-badge--synced");
      badge.innerHTML =
        '<svg class="seal-badge__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
        '<path d="M5 13l4 4L19 7" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
        '<span class="seal-badge__caption" id="seal-caption">Mühürlü</span>';
    }
    if (title) title.textContent = "Kayıt blokzincire mühürlendi";
    if (subtitle) subtitle.textContent = "Adım 6 / 6 tamamlandı — bu form artık kalıcı ve değiştirilemez olarak kayıtlıdır.";

    setStep("ipfs", "done");
    setStep("chain", "done");
    setStep("done", "done");

    var linksBlock = document.getElementById("synced-links");
    var linkIpfs = document.getElementById("link-ipfs");
    var linkTx = document.getElementById("link-tx");
    if (linksBlock && linkIpfs && linkTx && payload.ipfs_cid && payload.tx_hash) {
      linkIpfs.href = gatewayBase + "/" + payload.ipfs_cid;
      linkIpfs.textContent = payload.ipfs_cid;
      linkTx.href = payload.polygonscan_url || "#";
      linkTx.textContent = payload.tx_hash.slice(0, 18) + "…";
      linksBlock.style.display = "";
    }
  }

  function showFailed(payload) {
    setStep("ipfs", "active");
    var subtitle = document.getElementById("status-subtitle");
    if (subtitle && payload.hata_mesaji) {
      subtitle.textContent =
        "Bağlantı kesintisi ya da geçici bir hata oluştu (deneme " +
        payload.retry_count +
        "). Sistem bağlantı geri geldiğinde otomatik olarak yeniden deneyecek.";
    }
  }

  function poll() {
    fetch("/status/" + recordId, { headers: { Accept: "application/json" } })
      .then(function (res) {
        if (!res.ok) throw new Error("status isteği başarısız");
        return res.json();
      })
      .then(function (payload) {
        if (payload.durum === "SYNCED") {
          showSynced(payload);
          clearInterval(timer);
        } else if (payload.durum === "SYNCING") {
          setStep("ipfs", "active");
        } else if (payload.durum === "FAILED") {
          showFailed(payload);
        }
      })
      .catch(function () {
        /* Geçici ağ hatalarını yutar; bir sonraki periyotta yeniden denenir. */
      });
  }

  timer = setInterval(poll, POLL_MS);
  poll();
})();
