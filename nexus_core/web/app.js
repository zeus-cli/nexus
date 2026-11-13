(() => {
  const $ = (id) => document.getElementById(id);
  const state = { token: sessionStorage.getItem("nexus-token") || "" };
  const tokenInput = $("api-token");
  tokenInput.value = state.token;

  function notice(message, isError = false) {
    const node = $("notice");
    node.textContent = message;
    node.className = `notice visible${isError ? " error" : ""}`;
    window.clearTimeout(notice.timer);
    notice.timer = window.setTimeout(() => { node.className = "notice"; }, 5500);
  }

  function securityNotice(message, isError = false) {
    const node = $("security-notice");
    node.textContent = message;
    node.className = `notice visible${isError ? " error" : ""}`;
    window.clearTimeout(securityNotice.timer);
    securityNotice.timer = window.setTimeout(() => { node.className = "notice"; }, 5500);
  }

  function describeError(data, status) {
    const detail = data && data.detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object") {
      const parts = [detail.message || detail.error || `HTTP ${status}`];
      if (detail.justification) parts.push(detail.justification);
      if (detail.request_id) parts.push(`solicitação ${detail.request_id}`);
      return parts.filter(Boolean).join(" · ");
    }
    return `HTTP ${status}`;
  }

  async function api(path, options = {}) {
    if (!state.token) throw new Error("Informe o token local para continuar.");
    const response = await fetch(path, {
      ...options,
      headers: {
        "Authorization": `Bearer ${state.token}`,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(describeError(data, response.status));
    return data;
  }

  function setConnection(connected, label) {
    $("connection-dot").classList.toggle("online", connected);
    $("connection-label").textContent = label;
  }

  function textNode(tag, text, className) {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  async function refreshOverview() {
    const data = await api("/api/v1/status");
    $("status-value").textContent = data.status;
    $("status-detail").textContent = `${data.name} · ${data.version} · ${data.environment}`;
    $("autonomy-value").textContent = `${data.autonomy.level} / 4`;
    $("autonomy-detail").textContent = data.autonomy.name;
    $("events-value").textContent = String(data.events.total);
    $("memory-value").textContent = String(data.memory.total || 0);
    await refreshMemoryStats();
    $("sentinel-state").textContent = data.sentinel?.connected ? "online" : "offline";
    const sample = data.sentinel?.sample;
    $("sentinel-summary").textContent = sample
      ? JSON.stringify(sample, null, 2)
      : "Sem amostra recente. Inicie o serviço nexus-sentinel para ativar monitoramento Linux.";
    setConnection(true, data.sentinel?.connected ? "API + Sentinel conectados" : "API conectada");
    await Promise.all([refreshRecentEvents(), refreshProviderSummary(), refreshModels()]);
  }

  async function refreshRecentEvents() {
    const data = await api("/api/v1/events?limit=6");
    renderEvents($("recent-events"), data.items);
  }

  function renderEvents(target, items) {
    target.replaceChildren();
    if (!items.length) {
      target.append(textNode("div", "Nenhum evento registrado.", "empty"));
      return;
    }
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "event-row";
      row.append(
        textNode("time", new Date(item.created_at).toLocaleTimeString()),
        textNode("span", item.kind, "event-kind"),
        textNode("code", item.severity),
      );
      target.append(row);
    }
  }

  async function refreshEvents() {
    const data = await api("/api/v1/events?limit=100");
    renderEvents($("event-list"), data.items);
  }

  async function refreshProviderSummary() {
    const data = await api("/api/v1/models");
    const target = $("provider-summary");
    target.replaceChildren();
    const enabled = data.providers.filter((provider) => provider.enabled);
    if (!enabled.length) {
      target.append(textNode("div", "Nenhum provedor habilitado.", "empty"));
      return;
    }
    for (const provider of enabled) {
      const row = document.createElement("div");
      row.className = "usage-line";
      row.append(textNode("span", provider.name), textNode("span", provider.health.ok ? "online" : "indisponível"));
      target.append(row);
    }
  }

  async function refreshModels() {
    const data = await api("/api/v1/models");
    const routes = data.routes || [];
    const enabled = new Set(data.providers.filter((provider) => provider.enabled).map((provider) => provider.name));
    const selectModel = $("task-model");
    const selectedRoute = selectModel.value;
    selectModel.replaceChildren(new Option("Automático, conforme a cadeia configurada", ""));
    for (const route of routes) {
      if (!enabled.has(route.provider)) continue;
      const option = new Option(`${route.model} · ${route.provider}${route.task ? ` · ${route.task}` : ""}`, JSON.stringify({ provider: route.provider, model: route.model }));
      if (route.task && route.task !== $("task-kind").value) option.disabled = true;
      selectModel.add(option);
    }
    if ([...selectModel.options].some((option) => option.value === selectedRoute && !option.disabled)) selectModel.value = selectedRoute;
    const target = $("models-list");
    target.replaceChildren();
    for (const provider of data.providers) {
      const card = document.createElement("article");
      card.className = "model-card";
      const header = document.createElement("header");
      header.append(textNode("strong", provider.name), textNode("span", provider.health.ok ? "online" : (provider.health.state || "indisponível"), "badge"));
      card.append(header);
      card.append(textNode("p", `${provider.kind} · ${provider.authorized ? "autorizado" : "não autorizado"}`));
      card.append(textNode("p", `Configurados: ${provider.configured_models.join(", ") || "nenhum"}`));
      card.append(textNode("p", `Encontrados: ${provider.discovered_models.join(", ") || "—"}`));
      card.append(textNode("p", `Recursos do protocolo: ${Object.entries(provider.capabilities).filter(([, value]) => value).map(([key]) => key).join(", ") || "—"}`));
      target.append(card);
    }
    const usage = $("usage-list");
    usage.replaceChildren();
    const entries = Object.entries(data.usage);
    if (!entries.length) usage.append(textNode("div", "Nenhum uso registrado.", "empty"));
    for (const [model, counts] of entries) {
      const line = document.createElement("div");
      line.className = "usage-line";
      line.append(textNode("span", model), textNode("span", `${counts.requests} req · ${counts.prompt_tokens + counts.completion_tokens} tokens · $${Number(counts.estimated_cost_usd).toFixed(6)}`));
      usage.append(line);
    }
  }

  async function refreshTaskHistory() {
    const data = await api("/api/v1/events?limit=200");
    const items = data.items.filter((event) => event.kind.startsWith("agent.task."));
    const target = $("task-history");
    renderEvents(target, items);
  }

  async function refreshAutomation() {
    const data = await api("/api/v1/automation/actions");
    const target = $("automation-list");
    target.replaceChildren();
    if (!data.items.length) target.append(textNode("div", "Nenhum diagnóstico disponível.", "empty"));
    for (const action of data.items) {
      const card = document.createElement("article");
      card.className = "model-card";
      const heading = document.createElement("header");
      heading.append(textNode("strong", action.id), textNode("span", action.allowed ? "permitido" : `exige autonomia ${action.required_autonomy}`, "badge"));
      card.append(heading, textNode("p", action.description));
      const button = textNode("button", "Executar diagnóstico");
      button.className = "button quiet";
      button.type = "button";
      button.disabled = !action.allowed;
      button.addEventListener("click", async () => {
        $("automation-state").textContent = "executando";
        try {
          const result = await api(`/api/v1/automation/actions/${encodeURIComponent(action.id)}`, { method: "POST" });
          $("automation-result").textContent = `${result.stdout}${result.stderr ? `\n${result.stderr}` : ""}\n\ncódigo ${result.exit_code} · ${result.duration_ms} ms`;
          $("automation-state").textContent = "concluído";
        } catch (error) {
          $("automation-result").textContent = error.message;
          $("automation-state").textContent = "indisponível";
        }
      });
      card.append(button);
      target.append(card);
    }
  }

  async function refreshLogs() {
    const data = await api("/api/v1/logs?lines=500");
    $("log-content").textContent = data.items.join("\n") || "Log ainda vazio.";
  }

  async function refreshConfig() {
    const data = await api("/api/v1/config");
    $("config-content").textContent = JSON.stringify(data, null, 2);
  }

  async function refreshMemories(query = "") {
    const kind = $("memory-filter").value;
    const params = new URLSearchParams({ limit: "100", include_stats: "true" });
    if (kind) params.set("kind", kind);
    const url = query ? `/api/v1/memories/search?q=${encodeURIComponent(query)}&${params}` : `/api/v1/memories?${params}`;
    const data = await api(url);
    const target = $("memory-list");
    target.replaceChildren();
    if (!data.items.length) target.append(textNode("div", "Nenhuma memória encontrada.", "empty"));
    for (const item of data.items) {
      const card = document.createElement("article");
      card.className = "surface memory-card";
      card.append(textNode("h3", item.title || item.kind));
      card.append(textNode("p", item.content));
      const meta = document.createElement("div");
      meta.className = "memory-meta";
      meta.append(textNode("span", `${item.kind} · ${new Date(item.created_at).toLocaleString()}`));
      const remove = textNode("button", "Apagar");
      remove.className = "delete";
      remove.type = "button";
      remove.addEventListener("click", async () => {
        if (!window.confirm("Apagar esta memória permanentemente?")) return;
        try { await api(`/api/v1/memories/${encodeURIComponent(item.memory_id)}`, { method: "DELETE" }); await refreshMemories(query); await refreshMemoryStats(); notice("Memória apagada."); }
        catch (error) { notice(error.message, true); }
      });
      meta.append(remove);
      card.append(meta);
      target.append(card);
    }
  }

  async function refreshMemoryStats() {
    const data = await api("/api/v1/memories?limit=1&include_stats=true");
    const stats = data.stats || {};
    const map = {
      "mem-short-term": stats.short_term || 0,
      "mem-long-term": stats.long_term || 0,
      "mem-episodic": stats.episodic || 0,
      "mem-semantic": stats.semantic || 0,
    };
    for (const [id, value] of Object.entries(map)) {
      const el = $(id);
      if (el) el.textContent = String(value);
    }
  }

  async function refreshAgents() {
    const data = await api("/api/v1/agents");
    const target = $("agents-list");
    target.replaceChildren();
    if (!data.items.length) {
      target.append(textNode("div", "Nenhum agente registrado.", "empty"));
      return;
    }
    for (const item of data.items) {
      const card = document.createElement("article");
      card.className = "surface memory-card";
      card.append(textNode("h3", item.name || item.agent_id));
      card.append(textNode("p", `${item.role} · ${item.model_profile}`));
      const meta = document.createElement("div");
      meta.className = "memory-meta";
      meta.append(textNode("span", `estado: ${item.state || '—'}`));
      meta.append(textNode("span", `ID: ${item.agent_id.slice(0,8)}…`));
      card.append(meta);
      target.append(card);
    }
    const summary = $("agents-summary");
    summary.replaceChildren();
    const roles = {};
    for (const item of data.items) {
      roles[item.role] = (roles[item.role] || 0) + 1;
    }
    summary.append(textNode("p", Object.entries(roles).map(([r, c]) => `${c} ${r}`).join(" · ") || "nenhum agente"));
  }

  async function refreshSimulations() {
    const [worldsData, scenariosData] = await Promise.all([
      api("/api/v1/simulations/worlds"),
      api("/api/v1/simulations/scenarios"),
    ]);
    const worldsTarget = $("worlds-list");
    worldsTarget.replaceChildren();
    if (!worldsData.items.length) {
      worldsTarget.append(textNode("div", "Nenhum mundo registrado.", "empty"));
    }
    for (const w of worldsData.items) {
      const card = document.createElement("article");
      card.className = "surface memory-card";
      card.append(textNode("h3", w.name || w.world_id));
      card.append(textNode("p", `${w.paradigm} · ${w.status}`));
      worldsTarget.append(card);
    }
    const scenariosTarget = $("scenarios-list");
    scenariosTarget.replaceChildren();
    if (!scenariosData.items.length) {
      scenariosTarget.append(textNode("div", "Nenhum cenário registrado.", "empty"));
    }
    for (const s of scenariosData.items) {
      const card = document.createElement("article");
      card.className = "surface memory-card";
      card.append(textNode("h3", s.name || s.scenario_id));
      card.append(textNode("p", `${s.world_id} · ${s.is_baseline ? "baseline" : "alternativa"}`));
      scenariosTarget.append(card);
    }
  }

  async function refreshSecurity() {
    const [eventsData, approvalsData] = await Promise.all([
      api("/api/v1/events?limit=50"),
      api("/api/v1/approvals/pending"),
    ]);

    const approvalsTarget = $("pending-approvals");
    approvalsTarget.replaceChildren();
    if (!approvalsData.items.length) {
      approvalsTarget.append(textNode("div", "Nenhuma aprovação pendente.", "empty"));
    }
    for (const item of approvalsData.items) {
      const card = document.createElement("article");
      card.className = "model-card";
      const heading = document.createElement("header");
      heading.append(
        textNode("strong", item.tool_id || "solicitação"),
        textNode("span", item.risk_level || "risco não classificado", "badge"),
      );
      card.append(heading);
      card.append(textNode("p", item.justification || "Sem justificativa registrada."));
      const meta = document.createElement("div");
      meta.className = "approval-meta";
      meta.append(
        textNode("span", `tarefa ${item.task_id || "—"} · ${item.access_level || "—"} · ${item.execution_mode || "—"}`),
        textNode("span", `expira ${new Date(item.expires_at * 1000).toLocaleString()}`),
      );
      card.append(meta);

      const actions = document.createElement("div");
      actions.className = "form-footer";
      const decide = async (decision, label) => {
        try {
          await api(`/api/v1/approvals/${encodeURIComponent(item.id)}/decide`, {
            method: "POST",
            body: JSON.stringify({ decision }),
          });
          securityNotice(label);
          await refreshSecurity();
        } catch (error) {
          securityNotice(error.message, true);
        }
      };
      const approve = textNode("button", "Aprovar");
      approve.className = "button primary";
      approve.type = "button";
      approve.addEventListener("click", () => decide("approve", "Solicitação aprovada."));
      const deny = textNode("button", "Negar");
      deny.className = "button quiet";
      deny.type = "button";
      deny.addEventListener("click", () => decide("deny", "Solicitação negada."));
      actions.append(deny, approve);
      card.append(actions);
      approvalsTarget.append(card);
    }

    const target = $("security-events");
    target.replaceChildren();
    const securityEvents = eventsData.items.filter(e => e.kind.startsWith("security."));
    if (!securityEvents.length) {
      target.append(textNode("div", "Nenhum evento de segurança recente.", "empty"));
    }
    for (const ev of securityEvents) {
      const row = document.createElement("div");
      row.className = "event-row";
      row.append(
        textNode("time", new Date(ev.created_at).toLocaleTimeString()),
        textNode("span", ev.kind, "event-kind"),
        textNode("code", ev.severity),
      );
      target.append(row);
    }
  }

  $("save-token").addEventListener("click", async () => {
    state.token = tokenInput.value.trim();
    if (!state.token) { sessionStorage.removeItem("nexus-token"); setConnection(false, "token necessário"); return; }
    sessionStorage.setItem("nexus-token", state.token);
    try { await refreshOverview(); notice("Conexão local estabelecida."); }
    catch (error) { setConnection(false, "falha de autenticação"); notice(error.message, true); }
  });

  $("refresh").addEventListener("click", () => refreshOverview().catch((error) => notice(error.message, true)));
  $("events-refresh").addEventListener("click", () => refreshEvents().catch((error) => notice(error.message, true)));
  $("models-refresh").addEventListener("click", () => refreshModels().catch((error) => notice(error.message, true)));
  $("memory-refresh").addEventListener("click", () => refreshMemories().catch((error) => notice(error.message, true)));
  $("tasks-refresh").addEventListener("click", () => refreshTaskHistory().catch((error) => notice(error.message, true)));
  $("automation-refresh").addEventListener("click", () => refreshAutomation().catch((error) => notice(error.message, true)));
  $("agents-refresh").addEventListener("click", () => refreshAgents().catch((error) => notice(error.message, true)));
  $("simulations-refresh").addEventListener("click", () => refreshSimulations().catch((error) => notice(error.message, true)));
  $("security-refresh").addEventListener("click", () => refreshSecurity().catch((error) => notice(error.message, true)));
  $("logs-refresh").addEventListener("click", () => refreshLogs().catch((error) => notice(error.message, true)));
  $("config-refresh").addEventListener("click", () => refreshConfig().catch((error) => notice(error.message, true)));
  $("task-kind").addEventListener("change", () => refreshModels().catch((error) => notice(error.message, true)));
  $("notifications-enable").addEventListener("click", async () => {
    if (!("Notification" in window)) { notice("Este navegador não oferece notificações locais.", true); return; }
    const permission = await Notification.requestPermission();
    notice(permission === "granted" ? "Notificações ativadas." : "Permissão de notificação não concedida.", permission !== "granted");
  });

  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
    document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${button.dataset.panel}`));
    if (button.dataset.panel === "events") refreshEvents().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "models") refreshModels().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "memory") refreshMemories().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "tasks") refreshTaskHistory().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "automation") refreshAutomation().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "agents") refreshAgents().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "simulations") refreshSimulations().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "security") refreshSecurity().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "logs") refreshLogs().catch((error) => notice(error.message, true));
    if (button.dataset.panel === "config") refreshConfig().catch((error) => notice(error.message, true));
  }));
  document.querySelectorAll("[data-goto]").forEach((button) => button.addEventListener("click", () => document.querySelector(`[data-panel="${button.dataset.goto}"]`).click()));

  $("task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("task-state").textContent = "executando";
    $("task-state").className = "badge busy";
    $("task-result").textContent = "Aguardando o provedor configurado…";
    $("task-meta").textContent = "";
    try {
      const selection = $("task-model").value ? JSON.parse($("task-model").value) : {};
      const result = await api("/api/v1/tasks", { method: "POST", body: JSON.stringify({
        task: $("task-kind").value,
        prompt: $("task-prompt").value,
        system: $("task-system").value || null,
        ...selection,
      }) });
      const completion = result.final_completion;
      $("task-result").textContent = completion.text;
      $("task-state").textContent = "concluída";
      $("task-state").className = "badge done";
      $("task-meta").textContent = `${completion.provider}/${completion.model} · ${completion.latency_ms} ms · ${completion.prompt_tokens ?? "?"}/${completion.completion_tokens ?? "?"} tokens · custo ${completion.estimated_cost_usd ?? "não configurado"}`;
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("NEXUS · tarefa concluída", { body: `${completion.provider}/${completion.model}` });
      }
      await refreshOverview();
      await refreshTaskHistory();
    } catch (error) {
      $("task-result").textContent = error.message;
      $("task-state").textContent = "falhou";
      $("task-state").className = "badge error";
    }
  });

  $("memory-search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    refreshMemories($("memory-query").value).catch((error) => notice(error.message, true));
  });
  $("memory-add-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/v1/memories", { method: "POST", body: JSON.stringify({
        kind: $("memory-kind").value,
        title: $("memory-title").value,
        content: $("memory-content").value,
      }) });
      $("memory-content").value = "";
      await refreshMemories();
      notice("Memória salva.");
    } catch (error) { notice(error.message, true); }
  });

  if (state.token) refreshOverview().catch(() => setConnection(false, "falha de autenticação"));
})();
