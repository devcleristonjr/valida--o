(() => {
  const $ = (selector) => document.querySelector(selector);
  let photoBlob = null;
  let cameraStream = null;
  let lookupChallengeId = "";
  let credentialQrUrl = "";
  let photoPreviewUrl = "";
  let eventList = [];

  function digits(value) {
    return String(value || "").replace(/\D/g, "");
  }

  function formatCpf(value) {
    return digits(value).slice(0, 11)
      .replace(/^(\d{3})(\d)/, "$1.$2")
      .replace(/^(\d{3})\.(\d{3})(\d)/, "$1.$2.$3")
      .replace(/\.(\d{3})(\d)/, ".$1-$2");
  }

  function validCpf(value) {
    const cpf = digits(value);
    if (cpf.length !== 11 || /^(\d)\1{10}$/.test(cpf)) return false;
    for (let position = 9; position <= 10; position += 1) {
      let sum = 0;
      for (let index = 0; index < position; index += 1) {
        sum += Number(cpf[index]) * (position + 1 - index);
      }
      let digit = (sum * 10) % 11;
      if (digit === 10) digit = 0;
      if (digit !== Number(cpf[position])) return false;
    }
    return true;
  }

  async function request(url, options = {}) {
    const response = await fetch(url, { credentials: "same-origin", ...options });
    const contentType = response.headers.get("content-type") || "";
    const result = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) throw new Error(result?.error || "Não foi possível concluir a operação.");
    return result;
  }

  function setStatus(text, type = "") {
    const node = $("#eventStatus");
    node.textContent = text;
    node.className = `event-notice ${type}`;
  }

  function showPhotoPreview(file) {
    if (photoPreviewUrl) URL.revokeObjectURL(photoPreviewUrl);
    photoPreviewUrl = URL.createObjectURL(file);
    $("#eventPhotoPreview").src = photoPreviewUrl;
    $("#eventPhotoPreview").hidden = false;
  }

  function clearPhotoPreview() {
    if (photoPreviewUrl) URL.revokeObjectURL(photoPreviewUrl);
    photoPreviewUrl = "";
    $("#eventPhotoPreview").removeAttribute("src");
    $("#eventPhotoPreview").hidden = true;
  }

  function formatEvent(item) {
    const day = new Date(`${item.date}T12:00:00`).toLocaleDateString("pt-BR");
    const hours = item.end_time ? `${item.time}–${item.end_time}` : item.time;
    return `${item.name} · ${day} · ${hours}${item.location ? ` · ${item.location}` : ""}`;
  }

  async function loadEvents() {
    const registration = $("#registrationEvent");
    const lookup = $("#lookupEvent");
    eventList = await request("/api/events");
    registration.replaceChildren(new Option("Selecione um evento", ""));
    lookup.replaceChildren(new Option("Selecione o evento", ""));
    eventList.filter((item) => item.registration_open).forEach((item) => {
      const spots = item.available_spots === 0 ? " · Lista de espera"
        : item.available_spots === null ? "" : ` · ${item.available_spots} vaga(s)`;
      registration.add(new Option(`${formatEvent(item)}${spots}`, item.id));
    });
    eventList.forEach((item) => lookup.add(new Option(formatEvent(item), item.id)));
    if (!eventList.some((item) => item.registration_open)) {
      registration.disabled = true;
      $("#eventSubmit").disabled = true;
      setStatus("Não há eventos com inscrições abertas no momento.", "info");
    } else {
      registration.disabled = false;
      $("#eventSubmit").disabled = false;
      setStatus("");
    }
  }

  $("#eventCpf").addEventListener("input", (event) => {
    event.target.value = formatCpf(event.target.value);
  });
  $("#lookupCpf").addEventListener("input", (event) => {
    event.target.value = formatCpf(event.target.value);
  });
  $("#eventPhone").addEventListener("input", (event) => {
    const value = digits(event.target.value).slice(0, 11);
    event.target.value = value.length <= 10
      ? value.replace(/^(\d{2})(\d)/, "($1) $2").replace(/(\d{4})(\d)/, "$1-$2")
      : value.replace(/^(\d{2})(\d)/, "($1) $2").replace(/(\d{5})(\d)/, "$1-$2");
  });

  $("#startPhotoCamera").addEventListener("click", async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      $("#photoStatus").textContent = "Câmera indisponível neste navegador; selecione uma fotografia.";
      return;
    }
    try {
      cameraStream = await navigator.mediaDevices.getUserMedia({
        audio: false, video: { facingMode: { ideal: "user" } },
      });
      const video = $("#eventPhotoVideo");
      video.srcObject = cameraStream;
      video.hidden = false;
      await video.play();
      $("#startPhotoCamera").hidden = true;
      $("#capturePhoto").hidden = false;
      $("#photoStatus").textContent = "Centralize o rosto e selecione “Tirar foto”.";
    } catch (error) {
      $("#photoStatus").textContent = `Não foi possível acessar a câmera: ${error.message}`;
    }
  });

  $("#capturePhoto").addEventListener("click", () => {
    const video = $("#eventPhotoVideo");
    if (!video.videoWidth || !video.videoHeight) {
      $("#photoStatus").textContent = "A câmera ainda está iniciando. Tente novamente.";
      return;
    }
    const scale = Math.min(1, 1600 / Math.max(video.videoWidth, video.videoHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => {
      if (!blob) {
        $("#photoStatus").textContent = "Não foi possível preparar a fotografia.";
        return;
      }
      photoBlob = blob;
      $("#eventPhotoFile").value = "";
      showPhotoPreview(blob);
      $("#capturePhoto").hidden = true;
      $("#retakePhoto").hidden = false;
      stopCamera();
      $("#photoStatus").textContent = "Fotografia capturada.";
    }, "image/jpeg", 0.88);
  });

  $("#eventPhotoFile").addEventListener("change", (event) => {
    const [file] = event.target.files;
    if (!file) return;
    stopCamera();
    $("#eventPhotoVideo").hidden = true;
    $("#startPhotoCamera").hidden = false;
    $("#capturePhoto").hidden = true;
    photoBlob = null;
    showPhotoPreview(file);
    $("#retakePhoto").hidden = true;
    $("#photoStatus").textContent = file.size > 5 * 1024 * 1024
      ? "A fotografia deve ter até 5 MB."
      : "Fotografia selecionada.";
  });

  $("#retakePhoto").addEventListener("click", () => {
    photoBlob = null;
    clearPhotoPreview();
    $("#retakePhoto").hidden = true;
    $("#eventPhotoFile").value = "";
    $("#startPhotoCamera").hidden = false;
    $("#photoStatus").textContent = "";
  });

  async function showCredential(result) {
    const participant = result.participant;
    $("#credentialParticipant").textContent = participant.name;
    const eventDate = new Date(`${participant.event.date}T12:00:00`).toLocaleDateString("pt-BR");
    const eventHours = participant.event.end_time
      ? `${participant.event.time}–${participant.event.end_time}`
      : participant.event.time;
    $("#credentialDetails").textContent = [
      participant.category,
      `${participant.event.name} · ${eventDate} · ${eventHours}`,
      participant.event.location,
    ].filter(Boolean).join(" · ");
    $("#credentialCode").textContent = result.credential;
    $("#credentialPanel").hidden = false;
    $("#credentialPanel").scrollIntoView({ behavior: "smooth", block: "start" });
    if (credentialQrUrl) URL.revokeObjectURL(credentialQrUrl);
    try {
      const response = await fetch("/api/credentials/qr", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ credential: result.credential }),
      });
      const type = response.headers.get("content-type") || "";
      if (!response.ok) {
        const error = type.includes("application/json") ? await response.json() : null;
        throw new Error(error?.error || "Não foi possível gerar o QR Code.");
      }
      credentialQrUrl = URL.createObjectURL(await response.blob());
      $("#eventCredentialQr").src = credentialQrUrl;
    } catch (error) {
      $("#eventCredentialQr").alt = error.message;
      $("#eventCredentialQr").removeAttribute("src");
    }
  }

  $("#eventRegistrationForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const cpf = $("#eventCpf").value;
    if (!validCpf(cpf)) {
      $("#eventCpf").setCustomValidity("Informe um CPF válido.");
      $("#eventCpf").reportValidity();
      $("#eventCpf").setCustomValidity("");
      return;
    }
    const selectedPhoto = $("#eventPhotoFile").files[0];
    const photo = photoBlob || selectedPhoto;
    if (!photo || photo.size > 5 * 1024 * 1024) {
      $("#photoStatus").textContent = "Selecione ou tire uma fotografia de até 5 MB.";
      return;
    }
    if (!$("#eventConsent").checked) return;
    const button = $("#eventSubmit");
    button.disabled = true;
    button.textContent = "Salvando inscrição…";
    const payload = new FormData(form);
    payload.set("cpf", digits(cpf));
    payload.set("consent", "true");
    payload.set("photo", photo, photoBlob ? "fotografia.jpg" : selectedPhoto.name);
    try {
      const result = await request("/api/event-participants", { method: "POST", body: payload });
      const eventId = $("#registrationEvent").value;
      form.reset();
      $("#registrationEvent").value = eventId;
      photoBlob = null;
      clearPhotoPreview();
      $("#eventPhotoVideo").hidden = true;
      $("#startPhotoCamera").hidden = false;
      $("#capturePhoto").hidden = true;
      $("#retakePhoto").hidden = true;
      $("#photoStatus").textContent = "";
      if (result.registration_status === "waitlisted") {
        setStatus("Inscrição registrada na lista de espera. Avisaremos por e-mail se uma vaga for liberada.", "info");
      } else {
        setStatus("Inscrição concluída. Sua credencial foi gerada.", "good");
        await showCredential(result);
      }
      if (result.notification_warning) setStatus(result.notification_warning, "error");
    } catch (error) {
      setStatus(error.message);
    } finally {
      button.disabled = !eventList.some((item) => item.registration_open);
      button.textContent = "Finalizar e gerar credencial";
    }
  });

  $("#eventLookupForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const cpf = $("#lookupCpf").value;
    if (!validCpf(cpf)) {
      $("#lookupMessage").textContent = "Informe um CPF válido.";
      return;
    }
    try {
      const result = await request("/api/event-credentials/lookup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cpf: digits(cpf), event_id: $("#lookupEvent").value }),
      });
      lookupChallengeId = result.challenge_id;
      $("#lookupMessage").textContent = result.message;
      $("#lookupCodeLabel").hidden = false;
      $("#verifyLookupCode").hidden = false;
    } catch (error) {
      $("#lookupMessage").textContent = error.message;
    }
  });

  $("#verifyLookupCode").addEventListener("click", async () => {
    try {
      const result = await request("/api/event-credentials/lookup/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ challenge_id: lookupChallengeId, code: $("#lookupCode").value }),
      });
      $("#lookupMessage").textContent = "Credencial confirmada por e-mail.";
      await showCredential(result);
    } catch (error) {
      $("#lookupMessage").textContent = error.message;
    }
  });

  $("#printCredential").addEventListener("click", () => window.print());

  function stopCamera() {
    if (cameraStream) cameraStream.getTracks().forEach((track) => track.stop());
    cameraStream = null;
    const video = $("#eventPhotoVideo");
    if (video) video.srcObject = null;
  }

  window.addEventListener("pagehide", stopCamera);
  window.addEventListener("pagehide", () => {
    clearPhotoPreview();
    if (credentialQrUrl) URL.revokeObjectURL(credentialQrUrl);
  });
  loadEvents().catch((error) => {
    setStatus(error.message);
    $("#eventSubmit").disabled = true;
  });
})();
