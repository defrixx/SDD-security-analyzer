import { t } from "./i18n.js";
import { errorMessage } from "./app.js";
import { $, projectId, element, setStatus, api, post, action, details, render } from "./app.js";

/** @overload
 * @param {HTMLElement} form
 * @param {string} label
 * @param {string | number | boolean | null | undefined} value
 * @param {"textarea"} type
 * @returns {HTMLTextAreaElement}
 */
/** @overload
 * @param {HTMLElement} form
 * @param {string} label
 * @param {string | number | boolean | null | undefined} value
 * @param {string} [type]
 * @returns {HTMLInputElement}
 */
/** @param {HTMLElement} form
 * @param {string} label
 * @param {string | number | boolean | null | undefined} value
 * @param {string} [type]
 * @returns {HTMLInputElement | HTMLTextAreaElement}
 */
function connectionField(form, label, value, type = "text") {
  const id = `field-${crypto.randomUUID()}`;
  const title = element("label", label);
  title.htmlFor = id;
  const input = element(type === "textarea" ? "textarea" : "input");
  input.id = id;
  if (input instanceof HTMLInputElement) input.type = type;
  if (type === "checkbox" && input instanceof HTMLInputElement) input.checked = Boolean(value);
  else input.value = String(value ?? "");
  if (type === "password") input.autocomplete = "new-password";
  form.append(title, input);
  return input;
}
function connectionSelect(form, label, value, options) {
  const id = `field-${crypto.randomUUID()}`;
  const title = element("label", label);
  title.htmlFor = id;
  const input = element("select");
  input.id = id;
  options.forEach(([key, text]) => {
    input.add(new Option(text, key));
  });
  input.value = value;
  form.append(title, input);
  return input;
}
async function renderConnections(pid, content, isCurrent) {
  const [records, policy] = await Promise.all([
    api(`/projects/${pid}/connections`),
    api("/operator/network-policy"),
  ]);
  if (!isCurrent()) return;
  content.append(
    element("h2", t("LLM, LM Studio, and read-only MCP", "LLM, LM Studio и read-only MCP")),
  );
  const network = element("section", "", "panel stack");
  network.append(
    element("h3", t("External requests", "Внешние обращения")),
    element(
      "p",
      policy.allow_external_egress
        ? t(
            "Operator policy permits external endpoints. Project data requires separate scope approval.",
            "Операторская политика допускает внешние endpoints. Для данных проекта требуется отдельное принятие scope.",
          )
        : t(
            "External endpoints are disabled. Local addresses are allowed individually in each profile.",
            "Внешние endpoints отключены. Локальные адреса разрешаются точечно в каждом профиле.",
          ),
    ),
  );
  network.append(
    action(
      policy.allow_external_egress
        ? t("Disable external requests", "Отключить внешние обращения")
        : t("Allow external requests", "Принять возможность внешних обращений"),
      async () => {
        await post(
          "/operator/network-policy",
          { allow_external_egress: !policy.allow_external_egress, confirmed: true },
          "PUT",
        );
        if (projectId === pid) await render();
      },
    ),
  );
  content.append(network);
  content.append(action(t("Add connection", "Добавить подключение"), () => editConnection(pid)));
  if (!records.length)
    content.append(
      element(
        "p",
        t(
          "No connections. Start a local model server and add an LM Studio profile.",
          "Нет подключений. Запустите локальный сервер модели и добавьте профиль LM Studio.",
        ),
        "empty",
      ),
    );
  for (const record of records) {
    const profile = record.content;
    const panel = element("section", "", "panel");
    panel.append(
      element("h3", `${profile.name} · revision ${record.revision}`),
      element(
        "p",
        t(
          `${profile.adapter} · ${profile.endpoint} · ${profile.model || "no model selected"}`,
          `${profile.adapter} · ${profile.endpoint} · ${profile.model || "модель не выбрана"}`,
        ),
      ),
    );
    panel.append(
      element(
        "p",
        t(
          `Configuration scope: ${profile.data_scopes.join(", ") || "not set"}. Approved: ${record.granted_scopes.join(", ") || "none"}.`,
          `Scope конфигурации: ${profile.data_scopes.join(", ") || "не задан"}. Принято: ${record.granted_scopes.join(", ") || "нет"}.`,
        ),
      ),
    );
    const test = record.capability_report;
    panel.append(
      element(
        "p",
        test
          ? t(
              `Last test: ${test.tested_at} UTC · ${test.result.status}`,
              `Последний тест: ${test.tested_at} UTC · ${test.result.status}`,
            )
          : t("Connection has not been tested yet.", "Подключение ещё не проверялось."),
      ),
    );
    panel.append(
      action(t("Edit / new version", "Редактировать / новая версия"), () =>
        editConnection(pid, record),
      ),
      action(t("Test connection", "Проверить подключение"), async () => {
        setStatus(
          t(
            "Sending a live synthetic request from the backend…",
            "Выполняется реальный синтетический запрос из backend…",
          ),
        );
        const result = await post(`/projects/${pid}/connections/${record.id}/test`);
        if (projectId === pid) await render();
        details(t("Live test result", "Результат реальной проверки"), result);
      }),
    );
    if (test)
      panel.append(
        action(
          t("View report / model and tool list", "Посмотреть отчёт / список моделей и tools"),
          () => details("Capability report", test),
        ),
      );
    if (
      profile.secret_ref?.startsWith("file:") ||
      Object.values(profile.secret_headers || {}).some((ref) => ref.startsWith("file:"))
    )
      panel.append(
        action(
          t("Reload mounted credentials", "Перечитать смонтированные credentials"),
          async () => {
            await post(`/projects/${pid}/connections/${record.id}/reload-credentials`);
            if (projectId === pid) await render();
          },
        ),
      );
    const grant = element("div", "", "notice");
    grant.append(
      element(
        "p",
        t(
          `Destination: ${profile.endpoint}. Data classes: ${profile.data_scopes.join(", ") || "none selected"}. The operator cannot guarantee the provider's retention policy or that a local proxy will not forward data.`,
          `Направление: ${profile.endpoint}. Передаваемые классы: ${profile.data_scopes.join(", ") || "не выбраны"}. Оператор не гарантирует политику хранения данных провайдера или отсутствие дальнейшей пересылки локальным proxy.`,
        ),
      ),
    );
    grant.append(
      action(
        t("Accept displayed destination and scope", "Принять показанное направление и scope"),
        async () => {
          await post(`/projects/${pid}/connections/${record.id}/grants`, {
            confirmed: true,
            revision: record.revision,
            expected_sha256: record.sha256,
            data_scopes: profile.data_scopes,
          });
          if (projectId === pid) await render();
        },
      ),
    );
    panel.append(grant);
    panel.append(
      action(t("Revoke scopes for all revisions", "Отозвать scopes всех revisions"), async () => {
        await api(`/projects/${pid}/connections/${record.id}/grants`, { method: "DELETE" });
        if (projectId === pid) await render();
      }),
    );
    if (profile.kind === "mcp")
      panel.append(
        action(t("Bounded read-only call", "Ограниченный read-only вызов"), () =>
          mcpCallDialog(pid, record),
        ),
      );
    content.append(panel);
  }
}

/** @param {string} pid
 * @param {RegistryRecord<import('./api.generated').APIConnectionProfile> | null} [record]
 */
function editConnection(pid, record = null) {
  const content = $("#detail-content");
  content.replaceChildren(
    element(
      "h2",
      record
        ? t("New connection version", "Новая версия подключения")
        : t("Add connection", "Добавить подключение"),
    ),
  );
  const form = element("form", "", "stack");
  /** @type {Partial<import('./api.generated').APIConnectionProfile>} */
  const profile = record?.content || {};
  const preset = connectionSelect(form, t("Profile", "Профиль"), profile.adapter || "lm_studio", [
    ["lm_studio", "LM Studio (Docker → host)"],
    ["openai_compatible", "OpenAI-compatible / gateway"],
    ["streamable_http", "MCP Streamable HTTP"],
  ]);
  const name = connectionField(form, t("Name", "Название"), profile.name || "LM Studio");
  name.required = true;
  const endpoint = connectionField(
    form,
    "Base URL / MCP endpoint",
    profile.endpoint || "http://host.docker.internal:1234/v1",
  );
  endpoint.required = true;
  form.append(
    element(
      "p",
      t(
        "Container localhost means the container itself. LM Studio on the host is usually available at host.docker.internal; the application does not expose a LAN listener.",
        "Container localhost — сам контейнер. LM Studio на хосте обычно доступна через host.docker.internal; приложение не открывает listener в LAN.",
      ),
      "notice",
    ),
  );
  const addresses = connectionField(
    form,
    t(
      "Explicitly allowed private/loopback IPs for this endpoint (comma-separated)",
      "Явно разрешённые private/loopback IP для этого endpoint (через запятую)",
    ),
    (profile.local_addresses || []).join(", "),
  );
  const dns = element("pre", t("DNS has not been checked yet.", "DNS ещё не проверялся."));
  form.append(
    action(t("Show DNS from backend", "Показать DNS из backend"), async () => {
      dns.textContent = JSON.stringify(
        await post("/connection-diagnostics/resolve", { endpoint: endpoint.value }),
        null,
        2,
      );
    }),
    dns,
  );
  const model = connectionField(
    form,
    t(
      "Exact model ID (from capability report / model list)",
      "Точный model ID (из capability report / списка моделей)",
    ),
    profile.model || "",
  );
  const auth = connectionSelect(form, t("Authentication", "Авторизация"), profile.auth || "none", [
    ["none", t("No authentication", "Без auth")],
    ["bearer", "Static bearer token"],
  ]);
  const token = connectionField(
    form,
    t(
      "New token — memory only until restart (blank: keep secret reference)",
      "Новый token — только в памяти до restart (пусто: оставить secret reference)",
    ),
    "",
    "password",
  );
  const file = connectionField(
    form,
    t(
      "Use explicitly mounted file:llm_api_key",
      "Использовать явно смонтированный file:llm_api_key",
    ),
    profile.secret_ref === "file:llm_api_key",
    "checkbox",
  );
  const headers = connectionField(
    form,
    t("Additional non-secret headers (JSON)", "Дополнительные несекретные headers (JSON)"),
    JSON.stringify(profile.headers || {}, null, 2),
    "textarea",
  );
  const secretHeaders = connectionField(
    form,
    t(
      "Secret headers → secret references (JSON; do not paste key values here)",
      "Секретные headers → secret references (JSON; значения ключей сюда не вставлять)",
    ),
    JSON.stringify(profile.secret_headers || {}, null, 2),
    "textarea",
  );
  const timeout = connectionField(
    form,
    t("Timeout, seconds", "Timeout, секунд"),
    profile.timeout || 30,
    "number",
  );
  timeout.min = "1";
  timeout.max = "300";
  const output = connectionField(
    form,
    "Max output tokens",
    profile.max_output_tokens || 2048,
    "number",
  );
  const context = connectionField(
    form,
    t(
      "Context budget (UTF-8 input bytes + output token reserve; explicit limit)",
      "Context budget (UTF-8 bytes входа + резерв output tokens; явный лимит)",
    ),
    profile.context_budget || 16000,
    "number",
  );
  const temperature = connectionField(
    form,
    t("Temperature (blank: omit)", "Temperature (пусто: не передавать)"),
    profile.temperature,
    "number",
  );
  temperature.step = "0.1";
  const reasoning = connectionSelect(
    form,
    t(
      "Reasoning effort (separate verified API parameter)",
      "Reasoning effort (отдельный проверяемый параметр API)",
    ),
    profile.reasoning_effort || "",
    [
      ["", t("Omit", "Не передавать")],
      ...["none", "minimal", "low", "medium", "high", "xhigh"].map((key) => [key, key]),
    ],
  );
  const retries = connectionField(
    form,
    t("Maximum retries (0–2)", "Максимум retry (0–2)"),
    profile.retries ?? 2,
    "number",
  );
  const mode = connectionSelect(form, "JSON contract", profile.output_mode || "json_schema", [
    ["json_schema", "Structured output JSON Schema"],
    [
      "prompt_json",
      t(
        "Explicit degraded mode: prompt JSON + server validation",
        "Явный degraded mode: prompt JSON + server validation",
      ),
    ],
  ]);
  const calls = connectionField(
    form,
    t("Maximum MCP calls", "Максимум MCP calls"),
    profile.max_calls || 100,
    "number",
  );
  const bytes = connectionField(
    form,
    t("Maximum response/MCP bytes", "Максимум response/MCP bytes"),
    profile.max_bytes || 2097152,
    "number",
  );
  const tools = connectionField(
    form,
    "MCP allowlist (JSON mapping: input_schema, read_only_confirmed, argument_rules)",
    JSON.stringify(profile.allowed_tools || {}, null, 2),
    "textarea",
  );
  tools.rows = 8;
  form.append(
    element(
      "p",
      t(
        "MCP descriptions/annotations are untrusted. Select tools manually using the actual discovery schema. The server may have its own network access; read-only mapping does not isolate its implementation.",
        "MCP descriptions/annotations недоверенны. Выбирайте tools вручную по реальной схеме из discovery. Сервер может иметь собственную сеть; read-only mapping не изолирует его реализацию.",
      ),
      "notice",
    ),
  );
  const scopes = {};
  for (const [key, label] of [
    ["delta", "Delta"],
    ["baseline_fragments", t("Baseline fragments", "Фрагменты baseline")],
    ["catalog", t("Catalog", "Каталог")],
    ["skill_outputs", t("Skill outputs", "Выходы skills")],
    [
      "source_queries",
      t("Read-only source / MCP arguments", "Аргументы read-only источников / MCP"),
    ],
  ])
    scopes[key] = connectionField(
      form,
      t(`Allowed data class: ${label}`, `Разрешаемый класс: ${label}`),
      (profile.data_scopes || []).some((scope) => scope === key),
      "checkbox",
    );
  const enabled = connectionField(
    form,
    t(
      "Enable profile (data transfer still requires separate scope approval)",
      "Включить профиль (передача данных требует ещё отдельного принятия scope)",
    ),
    profile.enabled || false,
    "checkbox",
  );
  const submit = element(
    "button",
    t("Save new immutable version", "Сохранить новую неизменяемую версию"),
  );
  submit.type = "submit";
  const message = element("p", "", "error");
  message.setAttribute("role", "alert");
  form.append(submit, message);
  preset.addEventListener("change", () => {
    if (preset.value === "lm_studio") {
      name.value = "LM Studio";
      endpoint.value = "http://host.docker.internal:1234/v1";
    }
    if (preset.value === "streamable_http") {
      name.value = "Read-only MCP";
      endpoint.value = "http://host.docker.internal:8000/mcp";
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      let secretRef = profile.secret_ref || null;
      if (auth.value === "none") secretRef = null;
      else if (file.checked) secretRef = "file:llm_api_key";
      else if (token.value) {
        const result = await post(`/projects/${pid}/secrets`, { value: token.value });
        token.value = "";
        secretRef = result.secret_ref;
      }
      const value = {
        name: name.value,
        kind: preset.value === "streamable_http" ? "mcp" : "llm",
        adapter: preset.value,
        endpoint: endpoint.value,
        local_addresses: addresses.value
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
        model: model.value,
        auth: auth.value,
        secret_ref: secretRef,
        headers: JSON.parse(headers.value),
        secret_headers: JSON.parse(secretHeaders.value),
        timeout: Number(timeout.value),
        max_output_tokens: Number(output.value),
        context_budget: Number(context.value),
        temperature: temperature.value === "" ? null : Number(temperature.value),
        reasoning_effort: reasoning.value || null,
        retries: Number(retries.value),
        output_mode: mode.value,
        max_calls: Number(calls.value),
        max_bytes: Number(bytes.value),
        allowed_tools: preset.value === "streamable_http" ? JSON.parse(tools.value) : {},
        data_scopes: Object.keys(scopes).filter((key) => scopes[key].checked),
        enabled: enabled.checked,
      };
      if (record)
        await post(
          `/projects/${pid}/connections/${record.id}`,
          { expected_revision: record.revision, profile: value },
          "PUT",
        );
      else await post(`/projects/${pid}/connections`, value);
      $("#details").close();
      if (projectId === pid) await render();
    } catch (error) {
      message.textContent = errorMessage(error);
    } finally {
      submit.disabled = false;
    }
  });
  content.append(form);
  if (!$("#details").open) $("#details").showModal();
}

function mcpCallDialog(pid, record) {
  const content = $("#detail-content");
  content.replaceChildren(element("h2", "Read-only MCP call"));
  const form = element("form", "", "stack");
  const allowed = Object.keys(record.content.allowed_tools);
  if (!allowed.length) {
    content.append(
      element(
        "p",
        t(
          "Run discovery first, then create a new profile version with an explicit read-only mapping.",
          "Сначала выполните discovery, затем создайте новую версию профиля с явным read-only mapping.",
        ),
      ),
    );
    $("#details").showModal();
    return;
  }
  const tool = connectionSelect(
    form,
    t("Allowed tool", "Разрешённый tool"),
    allowed[0],
    allowed.map((name) => [name, name]),
  );
  const schema = element("pre", JSON.stringify(record.content.allowed_tools[tool.value], null, 2));
  tool.addEventListener("change", () => {
    schema.textContent = JSON.stringify(record.content.allowed_tools[tool.value], null, 2);
  });
  form.append(schema);
  const args = connectionField(
    form,
    t(
      "JSON arguments (validated against schema and scope)",
      "Аргументы JSON (будут проверены схемой и scope)",
    ),
    "{}",
    "textarea",
  );
  const submit = element(
    "button",
    t(
      "Execute selected bounded read-only call",
      "Выполнить выбранный ограниченный read-only вызов",
    ),
  );
  submit.type = "submit";
  const result = element("pre", t("Call has not been executed yet.", "Вызов ещё не выполнен."));
  result.setAttribute("aria-live", "polite");
  form.append(submit, result);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      result.textContent = JSON.stringify(
        await post(`/projects/${pid}/connections/${record.id}/tools/call`, {
          revision: record.revision,
          tool: tool.value,
          arguments: JSON.parse(args.value),
        }),
        null,
        2,
      );
    } catch (error) {
      result.textContent = errorMessage(error);
    } finally {
      submit.disabled = false;
    }
  });
  content.append(form);
  if (!$("#details").open) $("#details").showModal();
}

export { connectionField, connectionSelect, renderConnections, editConnection, mcpCallDialog };
