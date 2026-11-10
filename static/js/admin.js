(() => {
  const $ = (selector) => document.querySelector(selector);
  let csrfToken = "";
  let audiences = [];
  let detector;
  let cameraStream;
  let activeCamera = false;
  let refreshTimer;
  let isAdmin = false;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    if (csrfToken && !["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
      headers.set("X-CSRF-Token", csrfToken);
    }
    const response = await fetch(url, { credentials: "same-origin", ...options, headers });
    const type = response.headers.get("content-type") || "";
    const body = type.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const error = new Error(body?.error || body?.message || "Não foi possível concluir a operação.");
      error.status = response.status;
      error.code = body?.status;
      throw error;
    }
    return body;
  }

  function setNotice(element, text, good = false) {
    element.textContent = text;
    element.style.color = good ? "var(--green)" : "var(--red)";
  }

  async function initialize() {
    try {
      const current = await api("/api/admin/me");
      csrfToken = current.csrf_token;
      showAdmin(current.username, current.role);
      await reload();
      refreshTimer = window.setInterval(() => reload().catch(console.error), 15000);
    } catch {
      $("#loginPanel").hidden = false;
      $("#adminApp").hidden = true;
    }
  }

  function showAdmin(username, role) {
    isAdmin = role === "admin";
    $("#loginPanel").hidden = true;
    $("#adminApp").hidden = false;
    $("#adminName").textContent = `${username} (${isAdmin ? "Administrador" : "Recepção"})`;
    document.querySelectorAll(".admin-only").forEach((section) => { section.hidden = !isAdmin; });
  }

  $("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      const result = await api("/api/admin/login", {
        method: "POST",
        body: JSON.stringify({ username: data.get("username"), password: data.get("password") }),
      });
      csrfToken = result.csrf_token;
      showAdmin(result.username, result.role);
      setNotice($("#loginMessage"), "");
      await reload();
      refreshTimer = window.setInterval(() => reload().catch(console.error), 15000);
    } catch (error) {
      setNotice($("#loginMessage"), error.message);
    }
  });

  $("#logoutButton").addEventListener("click", async () => {
    try {
      await api("/api/admin/logout", { method: "POST", body: "{}" });
      window.clearInterval(refreshTimer);
      csrfToken = "";
      $("#adminApp").hidden = true;
      $("#loginPanel").hidden = false;
    } catch (error) {
      alert(error.message);
    }
  });

  async function reload() {
    await loadAudiences();
    const tasks = [loadDashboard()];
    if (isAdmin) tasks.push(loadPeople(), loadHistory());
    await Promise.all(tasks);
  }

  async function loadAudiences() {
    const selectedFilter = $("#filterAudience").value;
    audiences = await api("/api/admin/audiences");
    const options = ['<option value="">Todas as audiências</option>', ...audiences.map((item) =>
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.date)}</option>`)];
    $("#filterAudience").innerHTML = options.join("");
    $("#filterAudience").value = audiences.some((item) => item.id === selectedFilter) ? selectedFilter : "";
    const previous = $("#dashboardAudience").value;
    $("#dashboardAudience").innerHTML = options.join("");
    $("#dashboardAudience").value = audiences.some((item) => item.id === previous) ? previous : "";
    if (!audiences.length) {
      const empty = document.createElement("p");
      empty.className = "audiences-empty";
      empty.textContent = "Nenhuma audiência cadastrada. Crie uma audiência no formulário acima para disponibilizar inscrições.";
      $("#audiencesList").replaceChildren(empty);
      return;
    }
    $("#audiencesList").replaceChildren(...audiences.map((item) => {
      const chip = document.createElement("div");
      chip.className = "audience-chip";
      const details = document.createElement("div");
      details.className = "audience-details";
      const label = document.createElement("strong");
      label.textContent = item.name;
      const schedule = document.createElement("span");
      schedule.textContent = `${new Date(`${item.date}T12:00:00`).toLocaleDateString("pt-BR")} · ${item.time}`;
      const state = document.createElement("span");
      state.className = `audience-status ${item.active ? "open" : "closed"}`;
      state.textContent = item.active ? "Inscrições abertas" : "Inscrições encerradas";
      details.append(label, schedule, state);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "secondary";
      toggle.textContent = item.active ? "Encerrar inscrições" : "Reabrir inscrições";
      toggle.setAttribute("aria-label", `${toggle.textContent}: ${item.name}`);
      toggle.addEventListener("click", async () => {
        try {
          await api(`/api/admin/audiences/${encodeURIComponent(item.id)}`, {
            method: "PATCH", body: JSON.stringify({ active: !item.active }),
          });
          await reload();
        } catch (error) {
          setNotice($("#audienceMessage"), error.message);
        }
      });
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "Editar";
      edit.className = "secondary";
      edit.addEventListener("click", () => openAudienceEdit(item));
      chip.append(details, edit, toggle);
      return chip;
    }));
  }

  function openAudienceEdit(audience) {
    const form = $("#audienceEditForm");
    form.elements.namedItem("id").value = audience.id;
    form.elements.namedItem("name").value = audience.name;
    form.elements.namedItem("date").value = audience.date;
    form.elements.namedItem("time").value = audience.time;
    setNotice($("#audienceEditMessage"), "");
    $("#audienceEditDialog").showModal();
  }

  $("#cancelAudienceEdit").addEventListener("click", () => $("#audienceEditDialog").close());
  $("#audienceEditForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const id = values.id;
    delete values.id;
    try {
      await api(`/api/admin/audiences/${encodeURIComponent(id)}`, {
        method: "PATCH", body: JSON.stringify(values),
      });
      $("#audienceEditDialog").close();
      setNotice($("#audienceMessage"), "Audiência atualizada.", true);
      await reload();
    } catch (error) {
      setNotice($("#audienceEditMessage"), error.message);
    }
  });

  $("#audienceForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    try {
      await api("/api/admin/audiences", { method: "POST", body: JSON.stringify(values) });
      form.reset();
      setNotice($("#audienceMessage"), "Audiência cadastrada.", true);
      await reload();
    } catch (error) {
      setNotice($("#audienceMessage"), error.message);
    }
  });

  async function loadDashboard() {
    const id = $("#dashboardAudience").value;
    const data = await api(`/api/admin/dashboard${id ? `?audience_id=${encodeURIComponent(id)}` : ""}`);
    $("#metricRegistered").textContent = data.registered;
    $("#metricPresent").textContent = data.present;
    $("#metricAbsent").textContent = data.absent;
    $("#metricEntries").textContent = data.validated_entries;
    $("#metricInvalid").textContent = data.invalid_attempts;
    $("#metricPercent").textContent = `${data.attendance_percent}%`;
    $("#categoryStats").innerHTML = Object.entries(data.by_category).map(([name, count]) =>
      `<div class="stat-line"><span>${escapeHtml(name)}</span><strong>${count}</strong></div>`).join("") || "Sem registros.";
    $("#cityStats").innerHTML = data.by_city.map((item) =>
      `<div class="stat-line"><span>${escapeHtml(item.city)}</span><strong>${item.count}</strong></div>`).join("") || "Sem registros.";
    $("#recentEntries").innerHTML = data.recent_entries.map((item) =>
      `<div class="recent-line"><span>${escapeHtml(item.name)} · ${escapeHtml(item.method)}</span><time>${new Date(item.at).toLocaleString("pt-BR")}</time></div>`).join("") || "Nenhuma entrada.";
  }

  function participantFilters() {
    const params = new URLSearchParams();
    if ($("#searchPeople").value.trim()) params.set("q", $("#searchPeople").value.trim());
    if ($("#filterAudience").value) params.set("audience_id", $("#filterAudience").value);
    if ($("#filterDate").value) params.set("date", $("#filterDate").value);
    if ($("#filterStatus").value) params.set("status", $("#filterStatus").value);
    return params.toString();
  }

  async function loadPeople() {
    const query = participantFilters();
    const people = await api(`/api/admin/participants${query ? `?${query}` : ""}`);
    const rows = people.map((item) => {
      const situation = item.status !== "active" ? "cancelled" : item.checked_at ? "present" : "absent";
      const text = situation === "present" ? "Presente" : situation === "cancelled" ? "Cancelada" : "Ausente";
      return `<tr>
        <td data-label="Nome / CPF"><strong>${escapeHtml(item.name)}</strong><br>${escapeHtml(item.cpf)}</td>
        <td data-label="Categoria">${escapeHtml(item.category)}</td>
        <td data-label="Município">${escapeHtml(item.city)}</td>
        <td data-label="Cargo">${escapeHtml(item.role)}</td>
        <td data-label="Audiência">${escapeHtml(item.audience.name)}<br>${escapeHtml(item.audience.date)} ${escapeHtml(item.audience.time)}</td>
        <td data-label="Entrada">${item.checked_at ? escapeHtml(new Date(item.checked_at).toLocaleString("pt-BR")) : "—"}</td>
        <td data-label="Situação"><span class="status ${situation}">${text}</span></td>
        <td data-label="Ações"><div class="row-actions">
          <button data-action="edit" data-id="${escapeHtml(item.id)}">Editar</button>
          <button data-action="credential" data-id="${escapeHtml(item.id)}">Credencial</button>
          <button data-action="release" data-id="${escapeHtml(item.id)}">Liberar</button>
          <button data-action="cancel" data-id="${escapeHtml(item.id)}">Cancelar</button>
          <button data-action="delete" data-id="${escapeHtml(item.id)}">Excluir dados</button>
        </div></td></tr>`;
    });
    $("#peopleRows").innerHTML = rows.join("") || '<tr class="empty-row"><td colspan="8">Nenhum participante encontrado.</td></tr>';
    window.peopleById = Object.fromEntries(people.map((person) => [person.id, person]));
  }

  async function loadHistory() {
    const data = await api("/api/admin/history");
    const rows = [
      ...data.access.map((item) => ({ ...item, group: "Acesso" })),
      ...data.admin.map((item) => ({ ...item, group: "Administração", outcome: item.action })),
    ].sort((a, b) => new Date(b.at) - new Date(a.at)).slice(0, 100);
    $("#historyRows").innerHTML = rows.map((item) =>
      `<div class="history-line"><span><strong>${escapeHtml(item.group)}</strong> · ${escapeHtml(item.name || item.outcome)} · ${escapeHtml(item.detail || item.details || "")}</span><time>${new Date(item.at).toLocaleString("pt-BR")}</time></div>`).join("") || "Sem movimentações.";
  }

  $("#dashboardAudience").addEventListener("change", () => loadDashboard().catch(showError));
  ["searchPeople", "filterAudience", "filterDate", "filterStatus"].forEach((id) => {
    $(`#${id}`).addEventListener(id === "searchPeople" ? "input" : "change", debounce(() => {
      loadPeople().catch(showError);
      loadDashboard().catch(showError);
    }, 250));
  });
  $("#refreshPeople").addEventListener("click", () => reload().catch(showError));
  $("#peopleRows").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const person = window.peopleById?.[button.dataset.id];
    if (!person) return;
    if (button.dataset.action === "edit") openEdit(person);
    if (button.dataset.action === "credential") {
      try {
        const data = await api(`/api/admin/participants/${encodeURIComponent(person.id)}/credential`);
        $("#credentialName").textContent = `${data.participant.name} · ${data.participant.audience.name}`;
        $("#credentialCode").textContent = data.credential;
        const response = await fetch("/api/credentials/qr", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ credential: data.credential }),
        });
        if (!response.ok) throw new Error("Não foi possível gerar o QR Code.");
        const qrUrl = URL.createObjectURL(await response.blob());
        $("#credentialQr").src = qrUrl;
        $("#credentialDialog").showModal();
      } catch (error) { alert(error.message); }
    }
    if (button.dataset.action === "release") {
      const reason = prompt(`Justificativa para liberar a entrada de ${person.name}:`);
      if (!reason) return;
      try {
        await api(`/api/admin/participants/${encodeURIComponent(person.id)}/manual-check-in`, {
          method: "POST", body: JSON.stringify({ reason }),
        });
        await reload();
      } catch (error) { alert(error.message); }
    }
    if (button.dataset.action === "cancel") {
      const reason = prompt(`Motivo para cancelar a credencial de ${person.name}:`);
      if (!reason) return;
      try {
        await api(`/api/admin/participants/${encodeURIComponent(person.id)}/cancel`, {
          method: "POST", body: JSON.stringify({ reason }),
        });
        await reload();
      } catch (error) { alert(error.message); }
    }
    if (button.dataset.action === "delete") {
      const reason = prompt(`Justificativa para excluir definitivamente os dados de ${person.name}:`);
      if (!reason) return;
      if (!confirm("A exclusão apagará fotografia, contatos, CPF e credencial. Essa ação não pode ser desfeita. Continuar?")) return;
      try {
        await api(`/api/admin/participants/${encodeURIComponent(person.id)}`, {
          method: "DELETE", body: JSON.stringify({ reason }),
        });
        await reload();
      } catch (error) { alert(error.message); }
    }
  });

  function openEdit(person) {
    const form = $("#editForm");
    for (const [key, value] of Object.entries({
      id: person.id, name: person.name, cpf: person.cpf, category: person.category,
      city: person.city, role: person.role, organization: person.organization || "",
      phone: person.phone, email: person.email || "",
    })) form.elements.namedItem(key).value = value;
    const image = $("#editPhoto");
    image.hidden = false;
    image.src = `/api/admin/participants/${encodeURIComponent(person.id)}/photo`;
    $("#editDialog").showModal();
  }

  $("#cancelEdit").addEventListener("click", () => $("#editDialog").close());
  $("#closeCredential").addEventListener("click", () => $("#credentialDialog").close());
  $("#editForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const id = values.id;
    delete values.id;
    try {
      await api(`/api/admin/participants/${encodeURIComponent(id)}`, {
        method: "PATCH", body: JSON.stringify(values),
      });
      $("#editDialog").close();
      await reload();
    } catch (error) {
      setNotice($("#editMessage"), error.message);
    }
  });

  document.querySelectorAll("[data-export]").forEach((button) => button.addEventListener("click", () => {
    const params = new URLSearchParams(participantFilters());
    params.set("format", button.dataset.export);
    window.location.assign(`/api/admin/export?${params}`);
  }));

  $("#scanForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = $("#scanCode").value.trim();
    if (!code) return;
    const usedCamera = activeCamera;
    try {
      const result = await api("/api/admin/check-in", {
        method: "POST", body: JSON.stringify({ code, method: usedCamera ? "qr" : "usb" }),
      });
      showScan(
        "Entrada liberada",
        "good",
        `${result.participant.name} · ${result.participant.audience.name}`,
      );
      await reload();
    } catch (error) {
      if (error.code === "duplicate") {
        showScan("Entrada já registrada", "duplicate", "A liberação foi bloqueada para esta credencial.");
      } else if (error.code === "cancelled") {
        showScan("Credencial cancelada", "bad", "Entrada não autorizada. Encaminhe à equipe responsável.");
      } else if (error.code === "invalid" || error.status === 404) {
        showScan("Credencial inválida", "bad", "Código não encontrado. Confira a credencial e tente novamente.");
      } else {
        showScan("Não foi possível validar", "bad", error.message);
      }
    } finally {
      $("#scanCode").value = "";
      if (!usedCamera) $("#scanCode").focus();
    }
  });

  function showScan(message, type, detail = "") {
    const node = $("#scanMessage");
    const heading = document.createElement("strong");
    heading.textContent = message;
    const description = document.createElement("span");
    description.textContent = detail;
    node.replaceChildren(heading, description);
    node.className = `scan-message ${type}`;
  }

  $("#cameraButton").addEventListener("click", startScanner);
  $("#stopCameraButton").addEventListener("click", stopScanner);

  async function startScanner() {
    if (typeof window.BarcodeDetector !== "function" && typeof window.jsQR !== "function") {
      showScan("Leitura por câmera indisponível neste navegador. Use leitor USB/Bluetooth ou digite o código.", "warn");
      return;
    }
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      showScan(
        "A câmera precisa de uma conexão segura",
        "warn",
        "Abra o painel por HTTPS para liberar a câmera no iPhone. Também é possível digitar o código.",
      );
      return;
    }
    try {
      detector = null;
      if (typeof window.BarcodeDetector === "function") {
        try {
          detector = new window.BarcodeDetector({ formats: ["qr_code"] });
        } catch (error) {
          if (typeof window.jsQR !== "function") throw error;
        }
      }
      cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
      $("#scannerVideo").srcObject = cameraStream;
      await $("#scannerVideo").play();
      activeCamera = true;
      $("#cameraButton").hidden = true;
      $("#stopCameraButton").hidden = false;
      scanLoop();
    } catch (error) {
      const message = error.name === "NotAllowedError" || error.name === "PermissionDeniedError"
        ? "Permita o acesso à câmera nas configurações do Safari e tente novamente."
        : error.name === "NotFoundError" || error.name === "DevicesNotFoundError"
          ? "Não foi encontrada uma câmera disponível neste aparelho."
          : error.name === "NotReadableError" || error.name === "TrackStartError"
            ? "A câmera está em uso por outro aplicativo. Feche-o e tente novamente."
            : `Não foi possível ativar a câmera: ${error.message}`;
      showScan("Câmera indisponível", "bad", message);
      stopScanner();
    }
  }

  async function scanLoop() {
    if (!activeCamera) return;
    try {
      let decoded = "";
      if (detector) {
        const codes = await detector.detect($("#scannerVideo"));
        decoded = codes[0]?.rawValue || "";
      } else {
        const video = $("#scannerVideo");
        const canvas = scanLoop.canvas || (scanLoop.canvas = document.createElement("canvas"));
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (video.videoWidth) {
          const scale = Math.min(1, 900 / Math.max(video.videoWidth, video.videoHeight));
          canvas.width = Math.round(video.videoWidth * scale);
          canvas.height = Math.round(video.videoHeight * scale);
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
          decoded = window.jsQR(
            context.getImageData(0, 0, canvas.width, canvas.height).data,
            canvas.width,
            canvas.height,
          )?.data || "";
        }
      }
      if (decoded) {
        $("#scanCode").value = decoded;
        $("#scanForm").requestSubmit();
        stopScanner();
        return;
      }
    } catch (error) {
      showScan(`Falha na leitura do QR Code: ${error.message}`, "bad");
      stopScanner();
      return;
    }
    window.setTimeout(scanLoop, 350);
  }

  function stopScanner() {
    activeCamera = false;
    if (cameraStream) cameraStream.getTracks().forEach((track) => track.stop());
    cameraStream = null;
    $("#scannerVideo").srcObject = null;
    $("#cameraButton").hidden = false;
    $("#stopCameraButton").hidden = true;
  }

  window.addEventListener("pagehide", stopScanner);

  function debounce(callback, delay) {
    let timer;
    return () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(callback, delay);
    };
  }

  function showError(error) {
    setNotice($("#peopleMessage"), error.message);
  }

  initialize();
})();
