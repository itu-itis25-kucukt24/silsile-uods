(function () {
  "use strict";

  var dropzone = document.getElementById("dropzone");
  var input = document.getElementById("optik_form");
  var filenameEl = document.getElementById("dropzone-filename");
  var guideWrap = document.getElementById("camera-guide-wrap");
  var previewImg = document.getElementById("camera-guide-preview");
  var canvas = document.getElementById("camera-guide-canvas");

  if (!dropzone || !input) return;

  ["dragenter", "dragover"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      dropzone.classList.add("is-dragover");
    });
  });

  ["dragleave", "drop"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) {
      e.preventDefault();
      dropzone.classList.remove("is-dragover");
    });
  });

  dropzone.addEventListener("drop", function (e) {
    var files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) {
      input.files = files;
      handleFile(files[0]);
    }
  });

  input.addEventListener("change", function () {
    if (input.files && input.files[0]) handleFile(input.files[0]);
  });

  function handleFile(file) {
    if (filenameEl) filenameEl.textContent = file.name + " (" + Math.round(file.size / 1024) + " KB)";
    if (!previewImg || !guideWrap || !canvas) return;

    var url = URL.createObjectURL(file);
    previewImg.onload = function () {
      drawGuide();
      URL.revokeObjectURL(url);
    };
    previewImg.src = url;
    guideWrap.classList.add("is-active");
  }

  function drawGuide() {
    var rect = previewImg.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;
    var ctx = canvas.getContext("2d");
    if (!ctx) return;

    var margin = Math.min(rect.width, rect.height) * 0.07;
    var bracketLen = Math.min(rect.width, rect.height) * 0.1;
    var corners = [
      [margin, margin, 1, 1],
      [rect.width - margin, margin, -1, 1],
      [margin, rect.height - margin, 1, -1],
      [rect.width - margin, rect.height - margin, -1, -1],
    ];

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#c9bfa8";
    ctx.lineWidth = 3;
    ctx.lineCap = "round";

    corners.forEach(function (c) {
      var x = c[0], y = c[1], dx = c[2], dy = c[3];
      ctx.beginPath();
      ctx.moveTo(x, y + dy * bracketLen);
      ctx.lineTo(x, y);
      ctx.lineTo(x + dx * bracketLen, y);
      ctx.stroke();
    });

    ctx.fillStyle = "rgba(255,255,255,0.85)";
    ctx.font = "12px IBM Plex Mono, monospace";
    ctx.fillText("4 referans kare bu köşelere yakın olmalı", margin, rect.height - margin / 2);
  }

  window.addEventListener("resize", function () {
    if (guideWrap.classList.contains("is-active")) drawGuide();
  });
})();
