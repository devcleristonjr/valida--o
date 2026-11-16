(() => {
  const $ = (selector) => document.querySelector(selector);
  const token = document.body.dataset.inviteToken;
  let audiences = [];
  let events = [];

  function digits(value) {
    return String(value || "").replace(/\D/g, "");
  }

  function formatCpf(value) {
    return digits(value).slice(0, 11)
      .replace(/^(\d{3})(\d)/, "$1.$2")
      .replace(/^(\d{3})\.(\d{3})(\d)/, "$1.$2.$3")
      .replace(/\.(\d{3})(\d)/, ".$1-$2");
  }

  async function request(url, options = {}) {
    const response = await fetch(url, { credentials: "same-origin", ...options });
    const contentType = response.headers.get("content-type") || "";
    const result = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) throw new Error(result?.error || "Não foi possível concluir a operação.");
    return result;
  }

  function setStatus(message, type = "error") {
    const status = $("#inviteStatus");
    status.textContent = message;
    status.className = `event-notice ${type}`;
  }

  function formatActivity(item) {
    const date = new Date(`${item.date}T12:00:00`).toLocaleDateString("pt-BR");
    const time = item.end_time ? `${item.time}–${item.end_time}` : item.time;
    const location = item.location ? ` · ${item.location}` : "";
    return `${item.name} · ${date} · ${time}${location}`;
  }

  async function loadActivities() {
    [audiences, events] = await Promise.all([
      request("/api/audiences"),
      request("/api/events"),
    ]);
    events = events.filter((item) => item.registration_open);
  }

  function updateActivityOptions() {
    const select = $("#inviteActivity");
    const type = $("#inviteActivityType").value;
    const options = type === "audience" ? audiences : type === "event" ? events : [];
    select.replaceChildren(new Option("Selecione uma atividade", ""));
    options.forEach((item) => select.add(new Option(formatActivity(item), item.id)));
    select.disabled = options.length === 0;
    if (type && options.length === 0) {
      select.replaceChildren(new Option("Nenhuma atividade aberta", ""));
    }
  }

  $("#inviteActivityType").addEventListener("change", updateActivityOptions);

  $("#inviteRegistrationForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = $("#inviteSubmit");
    submit.disabled = true;
    submit.textContent = "Salvando cadastro…";
    const payload = new FormData(form);
    payload.set("activity_type", $("#inviteActivityType").value);
    payload.set("cpf", digits($("#inviteCpf").value));
    payload.set("consent", "true");
    try {
      const result = await request(`/api/cadastro/${encodeURIComponent(token)}`, {
        method: "POST",
        body: payload,
      });
      $("#inviteFormPanel").hidden = true;
      $("#inviteSuccess").hidden = false;
      if (result.registration_status === "waitlisted") {
        $("#inviteSuccessTitle").textContent = "Inscrição na lista de espera";
        $("#inviteSuccessMessage").textContent =
          "Sua inscrição foi registrada. Avisaremos por e-mail se uma vaga for liberada.";
      } else {
        $("#inviteSuccessTitle").textContent = "Cadastro concluído";
        $("#inviteSuccessMessage").textContent = "Sua inscrição está confirmada. Guarde sua credencial:";
        $("#inviteCredential").textContent = result.credential || "";
      }
      if (result.notification_warning) {
        setStatus(result.notification_warning, "error");
      } else {
        setStatus("Cadastro concluído com sucesso.", "good");
      }
    } catch (error) {
      setStatus(error.message);
    } finally {
      submit.disabled = false;
      submit.textContent = "Finalizar cadastro";
    }
  });

  async function initialize() {
    try {
      const invite = await request(`/api/cadastro/${encodeURIComponent(token)}`);
      $("#inviteCpf").value = formatCpf(invite.cpf);
      await loadActivities();
      $("#inviteFormPanel").hidden = false;
      setStatus("", "info");
      if (!audiences.length && !events.length) {
        $("#inviteSubmit").disabled = true;
        setStatus("Não há audiências ou eventos com inscrições abertas no momento.", "info");
      }
    } catch (error) {
      $("#inviteFormPanel").hidden = true;
      setStatus(error.message);
    }
  }

  initialize();
})();
