(() => {
  const $ = (selector) => document.querySelector(selector);
  let csrfToken = "";
  let audiences = [];
  let events = [];
  let detector;
  let cameraStream;
  let activeCamera = false;
  let refreshTimer;
  let isAdmin = false;
  let isInviteManager = false;
  let workflowData = null;
  let workflowView = 0;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  function cpfDigits(value) {
    return String(value || "").replace(/\D/g, "");
  }

  function formatCpf(value) {
    return cpfDigits(value).slice(0, 11)
      .replace(/^(\d{3})(\d)/, "$1.$2")
      .replace(/^(\d{3})\.(\d{3})(\d)/, "$1.$2.$3")
      .replace(/\.(\d{3})(\d)/, ".$1-$2");
  }

  function validCpf(value) {
    const cpf = cpfDigits(value);
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

  async function loadNotificationStatus() {
    const data = await api("/api/admin/notifications");
    return data;
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
      const message = body?.error || body?.message || (
        response.status >= 500
          ? `Erro interno do servidor (HTTP ${response.status}). Consulte o log da aplicação.`
          : "Não foi possível concluir a operação."
      );
      const error = new Error(message);
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

  function setAppNotice(text, type = "error") {
    const notice = $("#appNotice");
    notice.textContent = text;
    notice.className = text ? `app-notice ${type}` : "app-notice";
    notice.hidden = !text;
  }

  function activityStatusLabel(status) {
    return ({
      preparing: "Em preparação",
      open: "Inscrições abertas",
      closed: "Inscrições encerradas",
      cancelled: "Cancelada",
      completed: "Concluída",
      archived: "Arquivada",
    })[status] || "Situação desconhecida";
  }

  function startRefreshTimer() {
    window.clearInterval(refreshTimer);
    refreshTimer = window.setInterval(() => reload().catch(showError), 15000);
  }

  async function initialize() {
    let current;
    try {
      current = await api("/api/admin/me");
    } catch (error) {
      $("#loginPanel").hidden = false;
      $("#adminApp").hidden = true;
      if (error.status !== 401) setNotice($("#loginMessage"), error.message);
      return;
    }
    csrfToken = current.csrf_token;
    showAdmin(current.username, current.role);
    try {
      await reload();
    } catch (error) {
      showError(error);
    }
    startRefreshTimer();
  }

  function showAdmin(username, role) {
    isAdmin = role === "admin";
    isInviteManager = isAdmin || role === "organizer";
    $("#loginPanel").hidden = true;
    $("#adminApp").hidden = false;
    const roleLabel = { admin: "Administrador", organizer: "Organizador", reception: "Recepção" }[role] || "Usuário";
    $("#adminName").textContent = `${username} (${roleLabel})`;
    document.querySelectorAll(".admin-only").forEach((section) => { section.hidden = !isAdmin; });
    document.querySelectorAll(".invite-manager-only").forEach((section) => {
      section.hidden = !isInviteManager;
    });
    activateAdminTab(isAdmin ? "audienceAdminTab" : isInviteManager ? "invitesAdminTab" : "receptionAdminTab");
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
      try {
        await reload();
      } catch (error) {
        showError(error);
      }
      startRefreshTimer();
    } catch (error) {
      setNotice($("#loginMessage"), error.message);
    }
  });

  $("#registrationInviteCpf").addEventListener("input", (event) => {
    event.target.value = formatCpf(event.target.value);
  });

  async function submitRegistrationInvite(replaceActive = false) {
    const cpfField = $("#registrationInviteCpf");
    const cpf = cpfDigits(cpfField.value);
    if (!validCpf(cpf)) {
      cpfField.setCustomValidity("Informe um CPF válido.");
      cpfField.reportValidity();
      cpfField.setCustomValidity("");
      return;
    }

    const button = $("#createRegistrationInviteButton");
    button.disabled = true;
    button.textContent = "Gerando link…";
    $("#replaceRegistrationInviteButton").hidden = true;
    try {
      const result = await api("/api/admin/convites", {
        method: "POST",
        body: JSON.stringify({ cpf, replace_active: replaceActive }),
      });
      $("#registrationInviteUrl").value = new URL(result.url, window.location.origin).href;
      $("#registrationInviteResult").hidden = false;
      $("#copyRegistrationInviteButton").hidden = false;
      $("#replaceRegistrationInviteButton").hidden = true;
      const expiresAt = new Date(result.expires_at).toLocaleString("pt-BR");
      setNotice($("#registrationInviteMessage"), `Link gerado. Válido até ${expiresAt}.`, true);
    } catch (error) {
      if (error.status === 409 && !replaceActive) {
        $("#registrationInviteUrl").value = "";
        $("#registrationInviteResult").hidden = false;
        $("#copyRegistrationInviteButton").hidden = true;
        $("#replaceRegistrationInviteButton").hidden = false;
      } else {
        $("#registrationInviteResult").hidden = true;
      }
      setNotice($("#registrationInviteMessage"), error.message);
    } finally {
      button.disabled = false;
      button.textContent = "Gerar link";
    }
  }

  $("#registrationInviteForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitRegistrationInvite();
  });

  $("#replaceRegistrationInviteButton").addEventListener("click", async () => {
    if (!window.confirm("Gerar outro link invalida imediatamente o convite ativo. Deseja continuar?")) return;
    await submitRegistrationInvite(true);
  });

  $("#copyRegistrationInviteButton").addEventListener("click", async () => {
    const field = $("#registrationInviteUrl");
    try {
      await navigator.clipboard.writeText(field.value);
      setNotice($("#registrationInviteMessage"), "Link copiado.", true);
    } catch (_error) {
      field.select();
      setNotice($("#registrationInviteMessage"), "Link selecionado. Copie-o manualmente.");
    }
  });

  $("#logoutButton").addEventListener("click", async () => {
    try {
      await api("/api/admin/logout", { method: "POST", body: "{}" });
      window.clearInterval(refreshTimer);
      csrfToken = "";
      $("#adminApp").hidden = true;
      $("#loginPanel").hidden = false;
      setAppNotice("");
    } catch (error) {
      alert(error.message);
    }
  });

  async function reload() {
    if (isInviteManager && !isAdmin) {
      $("#lastUpdated").textContent = `Atualizado às ${new Date().toLocaleTimeString("pt-BR")}`;
      setAppNotice("");
      return;
    }
    await loadAudiences();
    const tasks = [loadDashboard()];
    let notificationTask;
    if (isAdmin) {
      await loadEvents();
      tasks.push(loadPeople(), loadHistory(), loadEventPeople());
      notificationTask = loadNotificationStatus();
    }
    await Promise.all(tasks);
    $("#lastUpdated").textContent = `Atualizado às ${new Date().toLocaleTimeString("pt-BR")}`;
    setAppNotice("");
    if (notificationTask) {
      const notifications = await notificationTask;
      if (notifications.failed) {
        setAppNotice(
          `${notifications.failed} e-mail(s) não foram enviados. Verifique a configuração SMTP ou execute novamente o processamento de notificações.`,
        );
      }
    }
  }

  function activateAdminTab(tabId) {
    const selectedButton = document.querySelector(`[data-admin-tab="${tabId}"]`);
    if (!selectedButton || selectedButton.hidden) return;
    document.querySelectorAll("[data-admin-tab]").forEach((tab) => {
      const selected = tab === selectedButton;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    document.querySelectorAll(".admin-tab-content").forEach((panel) => {
      panel.hidden = panel.id !== tabId;
    });
    if (tabId !== "receptionAdminTab") stopScanner();
    if (tabId === "eventAdminTab") loadEventPeople().catch(showError);
    if (tabId === "workflowAdminTab") loadWorkflowForSelectedEvent().catch(showError);
    if (tabId === "receptionAdminTab") $("#scanCode").focus({ preventScroll: true });
  }

  document.querySelectorAll("[data-admin-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.hidden) return;
      activateAdminTab(button.dataset.adminTab);
    });
    button.addEventListener("keydown", (event) => {
      const tabs = [...document.querySelectorAll("[data-admin-tab]:not([hidden])")];
      const currentIndex = tabs.indexOf(button);
      const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!direction) return;
      event.preventDefault();
      const next = tabs[(currentIndex + direction + tabs.length) % tabs.length];
      activateAdminTab(next.dataset.adminTab);
      next.focus();
    });
  });

  $("#refreshAllButton").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Atualizando…";
    try {
      await reload();
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
      button.textContent = "Atualizar dados";
    }
  });

  $("#processNotificationsButton").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Processando…";
    try {
      const result = await api("/api/admin/notifications/process", { method: "POST", body: "{}" });
      setAppNotice(
        result.failed
          ? `${result.sent} e-mail(s) enviados; ${result.failed} ainda pendente(s). Verifique o SMTP.`
          : `${result.sent} e-mail(s) enviados. ${result.queued} lembrete(s) foram enfileirados.`,
        result.failed ? "error" : "success",
      );
      await reload();
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
      button.textContent = "Processar e-mails";
    }
  });

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
      state.className = `audience-status ${item.status === "open" ? "open" : "closed"}`;
      const capacity = item.capacity === null
        ? `${item.confirmed} confirmados`
        : `${item.confirmed}/${item.capacity} vagas · ${item.available_spots} disponíveis`;
      state.textContent = `${activityStatusLabel(item.status)} · ${capacity} · ${item.waitlisted} na espera`;
      details.append(label, schedule, state);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "secondary";
      toggle.textContent = item.status === "open" ? "Encerrar inscrições" : "Abrir inscrições";
      toggle.disabled = ["cancelled", "archived", "completed"].includes(item.status);
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
    form.elements.namedItem("capacity").value = audience.capacity ?? "";
    form.elements.namedItem("status").value = audience.status;
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

  async function loadEvents() {
    const selectedId = $("#eventRosterSelect").value;
    events = await api("/api/admin/events");
    const options = ['<option value="">Selecione um evento</option>', ...events.map((item) =>
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.date)}</option>`) ];
    $("#eventRosterSelect").innerHTML = options.join("");
    $("#eventRosterSelect").value = events.some((item) => item.id === selectedId) ? selectedId : "";
    const workflowSelectedId = $("#workflowEventSelect").value;
    $("#workflowEventSelect").innerHTML = options.join("");
    $("#workflowEventSelect").value = events.some((item) => item.id === workflowSelectedId) ? workflowSelectedId : "";
    if (!events.length) {
      const empty = document.createElement("p");
      empty.className = "audiences-empty";
      empty.textContent = "Nenhum evento cadastrado. Crie um evento acima para abrir inscrições.";
      $("#eventsList").replaceChildren(empty);
      $("#eventPeopleRows").innerHTML = '<tr class="empty-row"><td colspan="7">Cadastre um evento para consultar inscritos.</td></tr>';
      updateEventMetrics();
      return;
    }
    $("#eventsList").replaceChildren(...events.map((item) => {
      const chip = document.createElement("div");
      chip.className = "audience-chip";
      const details = document.createElement("div");
      details.className = "audience-details";
      const title = document.createElement("strong");
      title.textContent = item.name;
      const date = document.createElement("span");
      const hours = item.end_time ? `${item.time}–${item.end_time}` : item.time;
      date.textContent = `${new Date(`${item.date}T12:00:00`).toLocaleDateString("pt-BR")} · ${hours} · ${item.location}`;
      const status = document.createElement("span");
      status.className = `audience-status ${item.status === "open" ? "open" : "closed"}`;
      const capacity = item.capacity === null
        ? `${item.confirmed} confirmados`
        : `${item.confirmed}/${item.capacity} vagas · ${item.available_spots} disponíveis`;
      status.textContent = `${activityStatusLabel(item.status)} · ${capacity} · ${item.waitlisted} na espera`;
      details.append(title, date, status);
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "secondary";
      edit.textContent = "Editar";
      edit.addEventListener("click", () => openEventEdit(item));
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "secondary";
      toggle.textContent = item.status === "open" ? "Encerrar inscrições" : "Abrir inscrições";
      toggle.disabled = ["cancelled", "archived", "completed"].includes(item.status);
      toggle.addEventListener("click", async () => {
        try {
          await api(`/api/admin/events/${encodeURIComponent(item.id)}`, {
            method: "PATCH", body: JSON.stringify({ active: !item.active }),
          });
          await reload();
        } catch (error) {
          setNotice($("#eventMessage"), error.message);
        }
      });
      const select = document.createElement("button");
      select.type = "button";
      select.className = "primary";
      select.textContent = "Ver inscritos";
      select.addEventListener("click", () => {
        $("#eventRosterSelect").value = item.id;
        loadEventPeople().catch(showError);
        $("#eventRosterSelect").focus();
      });
      chip.append(details, edit, toggle, select);
      return chip;
    }));
  }

  function openEventEdit(item) {
    const form = $("#eventEditForm");
    for (const [key, value] of Object.entries({
      id: item.id, name: item.name, date: item.date, time: item.time,
      end_time: item.end_time || "", location: item.location,
      capacity: item.capacity ?? "", status: item.status,
    })) form.elements.namedItem(key).value = value;
    setNotice($("#eventEditMessage"), "");
    $("#eventEditDialog").showModal();
  }

  $("#eventForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    try {
      const created = await api("/api/admin/events", {
        method: "POST", body: JSON.stringify(values),
      });
      form.reset();
      $("#eventRosterSelect").value = created.id;
      setNotice($("#eventMessage"), "Evento cadastrado. As inscrições já estão disponíveis.", true);
      await reload();
    } catch (error) {
      setNotice($("#eventMessage"), error.message);
    }
  });

  $("#cancelEventEdit").addEventListener("click", () => $("#eventEditDialog").close());
  $("#eventEditForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const id = values.id;
    delete values.id;
    try {
      await api(`/api/admin/events/${encodeURIComponent(id)}`, {
        method: "PATCH", body: JSON.stringify(values),
      });
      $("#eventEditDialog").close();
      setNotice($("#eventMessage"), "Evento atualizado.", true);
      await reload();
    } catch (error) {
      setNotice($("#eventEditMessage"), error.message);
    }
  });

  function eventParticipantFilters() {
    const params = new URLSearchParams();
    if ($("#eventRosterSelect").value) params.set("event_id", $("#eventRosterSelect").value);
    if ($("#searchEventPeople").value.trim()) params.set("q", $("#searchEventPeople").value.trim());
    if ($("#filterEventStatus").value) params.set("status", $("#filterEventStatus").value);
    return params.toString();
  }

  function updateEventMetrics(summary = {}) {
    $("#eventRegistered").textContent = summary.registered || 0;
    $("#eventPresent").textContent = summary.present || 0;
    $("#eventAbsent").textContent = summary.absent || 0;
    $("#eventCancelled").textContent = summary.cancelled || 0;
    $("#eventWaitlisted").textContent = summary.waitlisted || 0;
  }

  async function loadEventPeople() {
    const eventId = $("#eventRosterSelect").value;
    if (!eventId) {
      window.eventPeopleById = {};
      updateEventMetrics();
      $("#eventResultCount").textContent = "Selecione um evento para consultar os inscritos.";
      $("#eventPeopleRows").innerHTML = '<tr class="empty-row"><td colspan="7">Selecione um evento para consultar os inscritos.</td></tr>';
      return;
    }
    const query = eventParticipantFilters();
    const [people, summary] = await Promise.all([
      api(`/api/admin/event-participants?${query}`),
      api(`/api/admin/events/${encodeURIComponent(eventId)}/dashboard`),
    ]);
    updateEventMetrics(summary);
    $("#eventResultCount").textContent = `${summary.registered} confirmados · ${summary.waitlisted || 0} na lista de espera · ${
      summary.available_spots === null ? "sem limite de vagas" : `${summary.available_spots} vagas disponíveis`
    }`;
    $("#eventPeopleRows").innerHTML = people.map((person) => {
      const situation = person.status === "waitlisted"
        ? "waitlisted" : person.status !== "active" ? "cancelled" : person.checked_at ? "present" : "absent";
      const text = situation === "present" ? "Presente"
        : situation === "waitlisted" ? "Lista de espera"
          : situation === "cancelled" ? "Cancelada" : "Ausente";
      return `<tr>
        <td data-label="Nome / CPF"><strong>${escapeHtml(person.name)}</strong><br>${escapeHtml(person.cpf)}</td>
        <td data-label="Categoria">${escapeHtml(person.category)}</td>
        <td data-label="Município">${escapeHtml(person.city)}</td>
        <td data-label="Cargo">${escapeHtml(person.role)}</td>
        <td data-label="Entrada">${person.checked_at ? escapeHtml(new Date(person.checked_at).toLocaleString("pt-BR")) : "—"}</td>
        <td data-label="Situação"><span class="status ${situation}">${text}</span></td>
        <td data-label="Ações"><div class="row-actions">
          <button data-event-action="credential" data-id="${escapeHtml(person.id)}" ${person.status !== "active" ? "disabled" : ""}>Credencial</button>
          <button data-event-action="cancel" data-id="${escapeHtml(person.id)}" ${situation === "cancelled" ? "disabled" : ""}>Cancelar</button>
          <button data-event-action="delete" data-id="${escapeHtml(person.id)}">Excluir dados</button>
        </div></td></tr>`;
    }).join("") || '<tr class="empty-row"><td colspan="7">Nenhum participante encontrado.</td></tr>';
    $("#eventResultCount").textContent += ` · ${people.length} ${people.length === 1 ? "registro exibido" : "registros exibidos"}`;
    window.eventPeopleById = Object.fromEntries(people.map((person) => [person.id, person]));
  }

  $("#eventRosterSelect").addEventListener("change", () => loadEventPeople().catch(showError));
  ["searchEventPeople", "filterEventStatus"].forEach((id) => {
    $(`#${id}`).addEventListener(id === "searchEventPeople" ? "input" : "change", debounce(
      () => loadEventPeople().catch(showError), 250,
    ));
  });
  $("#refreshEventPeople").addEventListener("click", () => loadEventPeople().catch(showError));
  $("#clearEventFilters").addEventListener("click", () => {
    $("#searchEventPeople").value = "";
    $("#filterEventStatus").value = "";
    loadEventPeople().catch(showError);
  });
  $("#exportEventCsv").addEventListener("click", () => {
    const eventId = $("#eventRosterSelect").value;
    if (eventId) window.location.assign(`/api/admin/events/${encodeURIComponent(eventId)}/export`);
  });
  $("#printEventList").addEventListener("click", () => {
    document.body.classList.add("print-event-list");
    window.print();
  });
  $("#printAudienceList").addEventListener("click", () => {
    document.body.classList.add("print-audience-list");
    window.print();
  });
  window.addEventListener("afterprint", () => document.body.classList.remove("print-event-list"));
  window.addEventListener("afterprint", () => document.body.classList.remove("print-audience-list"));
  $("#eventPeopleRows").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-event-action]");
    if (!button) return;
    const person = window.eventPeopleById?.[button.dataset.id];
    if (!person) return;
    if (button.dataset.eventAction === "credential") {
      try {
        const data = await api(`/api/admin/event-participants/${encodeURIComponent(person.id)}/credential`);
        $("#credentialName").textContent = `${data.participant.name} · ${data.participant.event.name}`;
        $("#credentialCode").textContent = data.credential;
        const response = await fetch("/api/credentials/qr", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ credential: data.credential }),
        });
        if (!response.ok) throw new Error("Não foi possível gerar o QR Code.");
        $("#credentialQr").src = URL.createObjectURL(await response.blob());
        $("#credentialDialog").showModal();
      } catch (error) { alert(error.message); }
    }
    if (button.dataset.eventAction === "cancel") {
      const reason = prompt(`Motivo para cancelar a credencial de ${person.name}:`);
      if (!reason) return;
      try {
        await api(`/api/admin/event-participants/${encodeURIComponent(person.id)}/cancel`, {
          method: "POST", body: JSON.stringify({ reason }),
        });
        await reload();
      } catch (error) { alert(error.message); }
    }
    if (button.dataset.eventAction === "delete") {
      const reason = prompt(`Justificativa para excluir definitivamente os dados de ${person.name}:`);
      if (!reason) return;
      if (!confirm("A exclusão apagará fotografia, contatos, CPF e credencial do evento. Continuar?")) return;
      try {
        await api(`/api/admin/event-participants/${encodeURIComponent(person.id)}`, {
          method: "DELETE", body: JSON.stringify({ reason }),
        });
        await reload();
      } catch (error) { alert(error.message); }
    }
  });

  async function loadDashboard() {
    const id = $("#dashboardAudience").value;
    const data = await api(`/api/admin/dashboard${id ? `?audience_id=${encodeURIComponent(id)}` : ""}`);
    $("#metricRegistered").textContent = data.registered;
    $("#metricPresent").textContent = data.present;
    $("#metricAbsent").textContent = data.absent;
    $("#metricWaitlisted").textContent = data.waitlisted || 0;
    $("#metricCancelled").textContent = data.cancelled || 0;
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
      const situation = item.status === "waitlisted"
        ? "waitlisted" : item.status !== "active" ? "cancelled" : item.checked_at ? "present" : "absent";
      const text = situation === "present" ? "Presente"
        : situation === "waitlisted" ? "Lista de espera"
          : situation === "cancelled" ? "Cancelada" : "Ausente";
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
    $("#peopleResultCount").textContent = `${people.length} ${people.length === 1 ? "participante encontrado" : "participantes encontrados"}`;
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
  $("#clearPeopleFilters").addEventListener("click", () => {
    $("#searchPeople").value = "";
    $("#filterAudience").value = "";
    $("#filterDate").value = "";
    $("#filterStatus").value = "";
    reload().catch(showError);
  });
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
        `${result.participant.name} · ${result.participant.audience?.name || result.participant.event?.name}`,
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

  const workflowStages = [
    ["demand", "Demanda"],
    ["analysis", "Análise"],
    ["agenda", "Agenda"],
    ["technical_note", "Nota Técnica"],
    ["ceremonial", "Cerimonial"],
    ["secom", "SECOM"],
    ["report", "Relatórios"],
  ];

  function workflowTextField(key, label, value, type = "text") {
    const safeValue = escapeHtml(value || "");
    if (type === "textarea") {
      return `<label>${escapeHtml(label)}<textarea data-workflow-field="${escapeHtml(key)}" rows="4">${safeValue}</textarea></label>`;
    }
    if (type === "json") {
      return `<label>${escapeHtml(label)}<textarea data-workflow-field="${escapeHtml(key)}" data-workflow-json="true" rows="5">${escapeHtml(JSON.stringify(value || [], null, 2))}</textarea></label>`;
    }
    if (type === "select") {
      return "";
    }
    return `<label>${escapeHtml(label)}<input data-workflow-field="${escapeHtml(key)}" type="${type}" value="${safeValue}"></label>`;
  }

  function workflowSelectField(key, label, value, options) {
    return `<label>${escapeHtml(label)}<select data-workflow-field="${escapeHtml(key)}">${options.map((option) =>
      `<option value="${escapeHtml(option)}" ${option === value ? "selected" : ""}>${escapeHtml(option)}</option>`
    ).join("")}</select></label>`;
  }

  function renderWorkflowStepper() {
    $("#workflowStepper").innerHTML = workflowStages.map(([key, label], index) => {
      const status = workflowData?.stage_status?.[key]?.status || "pending";
      const state = status === "completed" || index < workflowData.current_stage
        ? "done" : index === workflowData.current_stage ? "current" : "";
      return `<button type="button" class="workflow-step ${state} ${index === workflowView ? "view" : ""}" data-workflow-view="${index}">
        <span>${index + 1}</span><strong>${escapeHtml(label)}</strong>
      </button>`;
    }).join("");
  }

  function renderWorkflowStage() {
    if (!workflowData) return;
    const [key] = workflowStages[workflowView];
    const stage = workflowData[key] || {};
    let fields = "";
    if (key === "demand") {
      fields = [
        workflowTextField("requester", "Solicitante", stage.requester),
        workflowTextField("origin", "Origem / documento", stage.origin),
        workflowTextField("received_at", "Recebido em", stage.received_at, "date"),
        workflowTextField("description", "Descrição da demanda", stage.description, "textarea"),
      ].join("");
    } else if (key === "analysis") {
      fields = [
        workflowSelectField("relevance", "Pertinência institucional", stage.relevance, ["", "Alta", "Média", "Baixa"]),
        workflowSelectField("decision", "Decisão", stage.decision, ["Em análise", "Aprovado", "Aprovado com ajustes", "Recusado"]),
        workflowTextField("opinion", "Parecer técnico e institucional", stage.opinion, "textarea"),
        workflowTextField("adjustments", "Motivo ou ajustes exigidos", stage.adjustments, "textarea"),
      ].join("");
    } else if (key === "agenda") {
      fields = [
        workflowTextField("team", "Equipe responsável", stage.team),
        workflowTextField("transport", "Transporte / deslocamento", stage.transport),
        workflowTextField("security", "Segurança e saúde", stage.security),
        workflowTextField("lodging", "Hospedagem", stage.lodging),
        workflowTextField("political", "Organização política", stage.political, "textarea"),
        workflowTextField("logistics", "Logística do local", stage.logistics, "textarea"),
        workflowTextField("checklist", "Checklist operacional", stage.checklist, "json"),
      ].join("");
    } else if (key === "technical_note") {
      fields = [
        workflowTextField("population", "População", stage.population),
        workflowTextField("idh", "IDH", stage.idh),
        workflowTextField("gdp", "PIB", stage.gdp),
        workflowTextField("economy", "Principais atividades econômicas", stage.economy),
        workflowTextField("investments", "Investimentos do Estado", stage.investments, "textarea"),
        workflowTextField("local_demands", "Demandas locais", stage.local_demands, "textarea"),
        workflowTextField("trip_report", "Relatório de viagem", stage.trip_report, "textarea"),
      ].join("");
    } else if (key === "ceremonial") {
      fields = [
        workflowTextField("route", "Roteiro", stage.route, "json"),
        workflowTextField("plaques", "Placas", stage.plaques, "json"),
        workflowTextField("acts", "Atos", stage.acts, "json"),
        workflowTextField("visits", "Visitas realizadas", stage.visits, "textarea"),
        workflowTextField("return_note", "Retorno da realização", stage.return_note, "textarea"),
      ].join("");
    } else if (key === "secom") {
      fields = [
        workflowTextField("brief", "Pauta / orientação de cobertura", stage.brief, "textarea"),
        workflowTextField("deliverables", "Entregáveis", stage.deliverables, "json"),
        workflowTextField("release", "Texto / release publicado", stage.release, "textarea"),
      ].join("");
    } else {
      fields = [
        workflowTextField("authorities", "Autoridades presentes", stage.authorities, "json"),
        workflowTextField("geography", "Geografia política", stage.geography, "textarea"),
        workflowTextField("assessment", "Avaliação geral", stage.assessment, "textarea"),
      ].join("");
    }
    const status = workflowData.stage_status?.[key] || { status: "pending", owner: "", due_at: "" };
    const statusOptions = ["pending", "in_progress", "completed", "blocked"];
    $("#workflowStageContent").innerHTML = `
      <div class="workflow-fields">${fields}</div>
      <div class="workflow-stage-control">
        ${workflowSelectField("workflow_status", "Situação da etapa", status.status, statusOptions)}
        <label>Responsável<input data-workflow-status="owner" value="${escapeHtml(status.owner)}"></label>
        <label>Prazo<input data-workflow-status="due_at" type="date" value="${escapeHtml(status.due_at)}"></label>
      </div>`;
  }

  function workflowSectionPayload() {
    const [key] = workflowStages[workflowView];
    const payload = {};
    $("#workflowStageContent").querySelectorAll("[data-workflow-field]").forEach((field) => {
      let value = field.value;
      if (field.dataset.workflowJson === "true") {
        try {
          value = value.trim() ? JSON.parse(value) : [];
        } catch (error) {
          throw new Error("Informe uma lista válida em JSON. Use colchetes e aspas duplas.");
        }
      }
      payload[field.dataset.workflowField] = value;
    });
    const statusNode = $("#workflowStageContent").querySelector('[data-workflow-field="workflow_status"]');
    const status = {
      [key]: {
        status: statusNode?.value || "pending",
        owner: $("#workflowStageContent").querySelector('[data-workflow-status="owner"]')?.value || "",
        due_at: $("#workflowStageContent").querySelector('[data-workflow-status="due_at"]')?.value || "",
      },
    };
    return { key, payload, status };
  }

  async function saveWorkflow({ advance = false, close = false } = {}) {
    if (!workflowData) return;
    const eventId = $("#workflowEventSelect").value;
    const section = workflowSectionPayload();
    const allStatus = {};
    workflowStages.forEach(([stageKey]) => {
      allStatus[stageKey] = workflowData.stage_status?.[stageKey] || { status: "pending", owner: "", due_at: "" };
    });
    allStatus[section.key] = section.status[section.key];
    const updated = await api(`/api/admin/events/${encodeURIComponent(eventId)}/workflow`, {
      method: "PATCH",
      body: JSON.stringify({
        [section.key]: section.payload,
        stage_status: allStatus,
        advance,
        close,
      }),
    });
    workflowData = updated;
    renderWorkflowStepper();
    renderWorkflowStage();
    $("#workflowReport").textContent = updated.consolidated_report || "";
    setNotice($("#workflowMessage"), close ? "Fluxo aprovado e encerrado." : advance ? "Etapa concluída e fluxo avançado." : "Etapa salva.", true);
  }

  async function loadWorkflowForSelectedEvent() {
    const eventId = $("#workflowEventSelect").value;
    if (!eventId) {
      $("#workflowEmpty").hidden = false;
      $("#workflowApp").hidden = true;
      return;
    }
    workflowData = await api(`/api/admin/events/${encodeURIComponent(eventId)}/workflow`);
    workflowView = workflowData.current_stage;
    $("#workflowEmpty").hidden = true;
    $("#workflowApp").hidden = false;
    renderWorkflowStepper();
    renderWorkflowStage();
    $("#workflowReport").textContent = workflowData.consolidated_report || "";
    $("#workflowClose").disabled = workflowData.closed;
    $("#workflowAdvance").disabled = workflowData.closed || workflowData.current_stage >= workflowStages.length - 1;
  }

  $("#workflowEventSelect").addEventListener("change", () => loadWorkflowForSelectedEvent().catch(showError));
  $("#workflowStepper").addEventListener("click", (event) => {
    const button = event.target.closest("[data-workflow-view]");
    if (!button) return;
    workflowView = Number(button.dataset.workflowView);
    renderWorkflowStepper();
    renderWorkflowStage();
  });
  $("#workflowForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await saveWorkflow();
    } catch (error) {
      setNotice($("#workflowMessage"), error.message);
    }
  });
  $("#workflowPrevious").addEventListener("click", () => {
    if (workflowView > 0) {
      workflowView -= 1;
      renderWorkflowStepper();
      renderWorkflowStage();
    }
  });
  $("#workflowAdvance").addEventListener("click", async () => {
    try {
      await saveWorkflow({ advance: true });
      if (workflowView < workflowStages.length - 1) workflowView += 1;
      renderWorkflowStepper();
      renderWorkflowStage();
    } catch (error) {
      setNotice($("#workflowMessage"), error.message);
    }
  });
  $("#workflowClose").addEventListener("click", async () => {
    if (!confirm("Aprovar e encerrar o fluxo institucional deste evento?")) return;
    try {
      await saveWorkflow({ close: true });
    } catch (error) {
      setNotice($("#workflowMessage"), error.message);
    }
  });
  $("#copyWorkflowReport").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#workflowReport").textContent);
      setNotice($("#workflowMessage"), "Relatório copiado.", true);
    } catch (error) {
      setNotice($("#workflowMessage"), "Não foi possível copiar automaticamente.");
    }
  });

  window.addEventListener("pagehide", stopScanner);

  function debounce(callback, delay) {
    let timer;
    return () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(callback, delay);
    };
  }

  function showError(error) {
    setAppNotice(error.message || "Não foi possível atualizar os dados.");
  }

  initialize();
})();
