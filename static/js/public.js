(() => {
  const $ = (selector) => document.querySelector(selector);
  let photoBlob = null;
  let photoUrl = null;
  let stream = null;
  let audiences = [];
  let selectedAudience = null;
  let lastCredential = null;
  let lookupChallengeId = null;

  function normalizeDigits(value) {
    return String(value || "").replace(/\D/g, "");
  }

  function formatCpf(value) {
    const digits = normalizeDigits(value).slice(0, 11);
    return digits
      .replace(/^(\d{3})(\d)/, "$1.$2")
      .replace(/^(\d{3})\.(\d{3})(\d)/, "$1.$2.$3")
      .replace(/\.(\d{3})(\d)/, ".$1-$2");
  }

  function validCpf(value) {
    const cpf = normalizeDigits(value);
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

  function setStatus(message, bad = false) {
    const status = $("#selfieStatus");
    if (status) {
      status.textContent = message;
      status.style.color = bad ? "#b12b42" : "";
    }
  }

  async function loadAudiences() {
    const response = await request("/api/audiences");
    audiences = response;
    const inputName = $("#audienceName");
    const inputDate = $("#audienceDate");
    const inputTime = $("#audienceTime");
    if (!inputName || !inputDate || !inputTime) return;
    inputName.readOnly = true;
    inputDate.readOnly = true;
    inputTime.readOnly = true;

    const wrapper = document.createElement("label");
    wrapper.className = "full";
    wrapper.textContent = "Selecione a audiência *";
    const select = document.createElement("select");
    select.className = "field";
    select.id = "audienceSelect";
    select.required = true;
    const placeholder = new Option("Selecione uma audiência", "");
    select.add(placeholder);
    audiences.forEach((audience) => {
      const option = new Option(
        `${audience.name} · ${new Date(`${audience.date}T12:00:00`).toLocaleDateString("pt-BR")} · ${audience.time}`,
        audience.id,
      );
      select.add(option);
    });
    wrapper.append(select);
    const schedule = inputName.closest(".audience-schedule");
    schedule?.prepend(wrapper);

    const requestedId = new URLSearchParams(location.search).get("audience");
    const initial = audiences.find((item) => item.id === requestedId)
      || (audiences.length === 1 ? audiences[0] : null);
    if (initial) select.value = initial.id;
    select.addEventListener("change", () => selectAudience(select.value));
    if (select.value) selectAudience(select.value);
    else if (!audiences.length) {
      showAudienceMessage(
        "Não há audiências abertas. A equipe responsável deve cadastrar ou ativar uma audiência na área administrativa.",
        true,
      );
      $("#registerForm button[type=submit]").disabled = true;
    } else {
      showAudienceMessage(
        "Selecione uma audiência; assunto, data e horário serão preenchidos automaticamente.",
      );
      $("#registerForm button[type=submit]").disabled = true;
    }
  }

  function selectAudience(id) {
    selectedAudience = audiences.find((item) => item.id === id) || null;
    $("#audienceName").value = selectedAudience?.name || "";
    $("#audienceDate").value = selectedAudience?.date || "";
    $("#audienceTime").value = selectedAudience?.time || "";
    $("#registerForm button[type=submit]").disabled = !selectedAudience;
    if (selectedAudience) showAudienceMessage("");
  }

  function showAudienceMessage(text, includeAdminLink = false) {
    let node = $("#audienceAvailability");
    if (!node) {
      node = document.createElement("p");
      node.id = "audienceAvailability";
      node.className = "notice";
      $("#registerForm .audience-schedule")?.append(node);
    }
    node.replaceChildren(document.createTextNode(text));
    if (includeAdminLink) {
      const link = document.createElement("a");
      link.href = "/admin";
      link.textContent = " Acessar área administrativa.";
      node.append(link);
    }
  }

  function installPrivacyNotice() {
    const actions = $("#registerForm .formactions");
    if (!actions || $("#privacyConsent")) return;
    const container = document.createElement("div");
    container.className = "full privacy-consent";
    container.style.cssText = "grid-column:1/-1;margin-top:8px;padding:14px;border:1px solid #e9e9ef;border-radius:12px;background:#fff8f9";
    const text = document.createElement("p");
    text.textContent = "A SERIN utilizará seus dados de contato e a fotografia exclusivamente para organizar esta audiência, identificar visualmente participantes e registrar a presença. Não há reconhecimento biométrico automatizado. O acesso é restrito à equipe autorizada. O prazo de retenção e o canal formal para exercício dos direitos LGPD devem seguir a política institucional vigente.";
    text.style.cssText = "font-size:12px;line-height:1.55;margin-bottom:10px";
    const label = document.createElement("label");
    label.style.cssText = "display:flex;align-items:flex-start;gap:8px;color:#424554;font-size:12px";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = "privacyConsent";
    checkbox.required = true;
    checkbox.style.cssText = "width:auto;margin-top:3px";
    const labelText = document.createElement("span");
    labelText.textContent = "Li e estou ciente do aviso de privacidade e do uso da fotografia para identificação visual.";
    label.append(checkbox, labelText);
    container.append(text, label);
    actions.before(container);
  }

  function applyMasks() {
    $("#personDocument")?.addEventListener("input", (event) => {
      event.target.value = formatCpf(event.target.value);
    });
    $("#myCredentialDocument")?.addEventListener("input", (event) => {
      event.target.value = formatCpf(event.target.value);
    });
    $("#personPhone")?.addEventListener("input", (event) => {
      const digits = normalizeDigits(event.target.value).slice(0, 11);
      if (digits.length <= 10) {
        event.target.value = digits.replace(/^(\d{2})(\d)/, "($1) $2").replace(/(\d{4})(\d)/, "$1-$2");
      } else {
        event.target.value = digits.replace(/^(\d{2})(\d)/, "($1) $2").replace(/(\d{5})(\d)/, "$1-$2");
      }
    });
  }

  window.startSelfieCamera = async function startSelfieCamera() {
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus("Este navegador não disponibiliza a câmera. Acesse por HTTPS e permita o uso da câmera.", true);
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: "user" }, width: { ideal: 1280 }, height: { ideal: 960 } },
      });
      const video = $("#selfieVideo");
      video.srcObject = stream;
      await video.play();
      $("#selfieStage").classList.add("active");
      $("#selfieStage").classList.remove("captured");
      $("#selfieStart").hidden = true;
      $("#selfieCapture").hidden = false;
      $("#selfieRetake").hidden = true;
      setStatus("Centralize o rosto e toque em “Tirar foto”.");
    } catch (error) {
      setStatus(`Não foi possível acessar a câmera: ${error.message}`, true);
    }
  };

  window.captureSelfie = function captureSelfie() {
    const video = $("#selfieVideo");
    if (!video.videoWidth || !video.videoHeight) {
      setStatus("A câmera ainda está iniciando. Tente novamente em alguns segundos.", true);
      return;
    }
    const canvas = document.createElement("canvas");
    const maxDimension = 1600;
    const scale = Math.min(1, maxDimension / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => {
      if (!blob) {
        setStatus("Não foi possível preparar a fotografia. Tente novamente.", true);
        return;
      }
      photoBlob = blob;
      if (photoUrl) URL.revokeObjectURL(photoUrl);
      photoUrl = URL.createObjectURL(blob);
      $("#selfiePreview").src = photoUrl;
      $("#selfiePreview").hidden = false;
      $("#selfieStage").classList.remove("active");
      $("#selfieStage").classList.add("captured");
      $("#selfieCapture").hidden = true;
      $("#selfieRetake").hidden = false;
      stopCamera();
      setStatus("Fotografia capturada. Confira a imagem ou tire novamente.");
    }, "image/jpeg", 0.88);
  };

  window.retakeSelfie = function retakeSelfie() {
    photoBlob = null;
    $("#selfiePreview").hidden = true;
    $("#selfieStage").classList.remove("captured");
    window.startSelfieCamera();
  };

  function stopCamera() {
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = null;
    const video = $("#selfieVideo");
    if (video) video.srcObject = null;
  }

  window.addEventListener("pagehide", stopCamera);

  window.registerPerson = async function registerPerson(event) {
    event.preventDefault();
    const form = $("#registerForm");
    if (!selectedAudience) {
      alert("Selecione uma audiência disponível.");
      return;
    }
    const cpf = $("#personDocument").value;
    if (!validCpf(cpf)) {
      $("#personDocument").setCustomValidity("Informe um CPF válido.");
      $("#personDocument").reportValidity();
      $("#personDocument").setCustomValidity("");
      return;
    }
    if (!photoBlob) {
      setStatus("Tire e confira a fotografia antes de finalizar o cadastro.", true);
      return;
    }
    const consent = $("#privacyConsent");
    if (!consent?.checked) {
      alert("É necessário confirmar a ciência do aviso de privacidade.");
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = "Salvando cadastro…";
    const payload = new FormData();
    payload.set("audience_id", selectedAudience.id);
    payload.set("cpf", normalizeDigits(cpf));
    payload.set("category", $("#personCategory").value);
    payload.set("name", $("#personName").value.trim());
    payload.set("city", $("#personCity").value.trim());
    payload.set("role", $("#personRole").value.trim());
    payload.set("organization", $("#personOrg")?.value.trim() || "");
    payload.set("phone", $("#personPhone").value.trim());
    payload.set("email", $("#personEmail").value.trim());
    payload.set("consent", "true");
    payload.set("photo", photoBlob, "fotografia.jpg");
    try {
      const result = await request("/api/participants", { method: "POST", body: payload });
      const phone = $("#personPhone").value.trim();
      lastCredential = { ...result, phone };
      renderCredential(result.credential, result.participant);
      form.reset();
      $("#personDocument").value = "";
      $("#personPhone").value = "";
      photoBlob = null;
      $("#selfiePreview").hidden = true;
      $("#selfieStage").classList.remove("captured", "active");
      $("#selfieStart").hidden = false;
      $("#selfieCapture").hidden = true;
      $("#selfieRetake").hidden = true;
      $("#privacyConsent").checked = false;
      if ($("#audienceSelect")) {
        $("#audienceSelect").value = selectedAudience.id;
        selectAudience(selectedAudience.id);
      }
      setStatus("Cadastro concluído. A credencial digital foi criada.");
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = !selectedAudience;
      button.textContent = "Finalizar e gerar QR Code";
    }
  };

  async function renderCredential(token, participant) {
    const container = $("#credential");
    const modal = $("#audienceCredentialModal");
    if (!container || !modal) return;
    container.replaceChildren();
    const wrap = document.createElement("div");
    wrap.className = "qrwrap";
    const qr = document.createElement("div");
    qr.className = "qrbox";
    const details = document.createElement("div");
    const date = new Date(`${participant.audience.date}T12:00:00`).toLocaleDateString("pt-BR");
    const header = document.createElement("div");
    header.className = "eyebrow";
    header.textContent = "COMPROVANTE DIGITAL";
    const title = document.createElement("h2");
    title.textContent = participant.name;
    const lines = [
      `${participant.category} · ${participant.city}`,
      participant.role,
      participant.audience.name,
      `${date} · ${participant.audience.time}`,
      token,
    ];
    details.append(header, title);
    lines.forEach((line) => {
      const text = document.createElement("p");
      text.textContent = line;
      details.append(text);
    });
    const actions = document.createElement("div");
    actions.className = "actions";
    actions.style.marginTop = "12px";
    const share = document.createElement("button");
    share.type = "button";
    share.className = "btn primary";
    share.textContent = "Compartilhar pelo WhatsApp";
    share.addEventListener("click", () => window.shareCredentialWhatsApp(token));
    const print = document.createElement("button");
    print.type = "button";
    print.className = "btn";
    print.textContent = "Imprimir comprovante";
    print.addEventListener("click", () => window.print());
    actions.append(share, print);
    details.append(actions);
    wrap.append(qr, details);
    container.append(wrap);
    modal.hidden = false;
    try {
      const response = await fetch("/api/credentials/qr", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ credential: token }),
      });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || "Não foi possível gerar o QR Code.");
      }
      const objectUrl = URL.createObjectURL(await response.blob());
      const img = document.createElement("img");
      img.alt = "QR Code da credencial";
      img.width = 170;
      img.height = 170;
      img.src = objectUrl;
      qr.append(img);
    } catch (error) {
      qr.textContent = error.message;
    }
  }

  window.showCredential = function showCredential(code) {
    if (lastCredential?.credential === code) {
      renderCredential(code, lastCredential.participant);
    }
  };

  window.shareCredentialWhatsApp = function shareCredentialWhatsApp(token) {
    const participant = lastCredential?.participant;
    if (!participant) return;
    const phone = normalizeDigits(lastCredential.phone);
    const message = [
      `Comprovante de audiência: ${participant.audience.name}`,
      `Data: ${participant.audience.date} às ${participant.audience.time}`,
      `Participante: ${participant.name}`,
      `Código da credencial: ${token}`,
    ].join("\n");
    const destination = phone ? `https://wa.me/55${phone}?` : "https://wa.me/?";
    window.open(`${destination}text=${encodeURIComponent(message)}`, "_blank", "noopener,noreferrer");
  };

  window.lookupCredential = async function lookupCredential() {
    const message = $("#credentialLookupMessage");
    const cpf = $("#myCredentialDocument").value;
    if (!validCpf(cpf)) {
      message.textContent = "Informe um CPF válido.";
      return;
    }
    try {
      const result = await request("/api/credentials/lookup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cpf: normalizeDigits(cpf) }),
      });
      lookupChallengeId = result.challenge_id;
      message.textContent = result.message;
      let codeInput = $("#credentialOtp");
      if (!codeInput) {
        codeInput = document.createElement("input");
        codeInput.id = "credentialOtp";
        codeInput.inputMode = "numeric";
        codeInput.maxLength = 6;
        codeInput.placeholder = "Código de 6 dígitos enviado ao e-mail cadastrado";
        $("#myCredentialDocument").after(codeInput);
        const verify = document.createElement("button");
        verify.id = "verifyCredentialOtp";
        verify.className = "btn";
        verify.type = "button";
        verify.textContent = "Confirmar código";
        codeInput.after(verify);
        verify.addEventListener("click", verifyLookup);
      }
    } catch (error) {
      message.textContent = error.message;
    }
  };

  async function verifyLookup() {
    const message = $("#credentialLookupMessage");
    try {
      const result = await request("/api/credentials/lookup/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ challenge_id: lookupChallengeId, code: $("#credentialOtp").value }),
      });
      lastCredential = result;
      await renderCredential(result.credential, result.participant);
      message.textContent = "Credencial confirmada por e-mail.";
    } catch (error) {
      message.textContent = error.message;
    }
  }

  window.printCredential = () => window.print();

  function onReady() {
    installPrivacyNotice();
    applyMasks();
    loadAudiences().catch((error) => {
      showAudienceMessage(error.message);
      $("#registerForm button[type=submit]").disabled = true;
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", onReady, { once: true });
  else onReady();
})();
