import { t } from "./i18n.js";
import { errorMessage } from "./app.js";
import { connectionField, connectionSelect } from "./connections.js";
import { lineValues, idValues } from "./authoring.js";
import { $, projectId, element, api, post, action, details, render } from "./app.js";

async function renderSources(pid, content, isCurrent) {
  const [records, initial] = await Promise.all([
    api(`/projects/${pid}/sources`),
    api("/source-defaults"),
  ]);
  if (!isCurrent()) return;
  content.append(
    element("h2", t("Documentation and catalog sources", "Источники документации и каталогов")),
    element("p", initial.notice, "notice"),
    element(
      "p",
      t(
        "Reading creates an immutable snapshot. A new run uses selected snapshots; there are no automatic updates, crawling, or tool calls.",
        "Чтение создаёт immutable snapshot. Новый run использует выбранные snapshots; автоматического обновления, crawl или tool calls нет.",
      ),
    ),
    action(t("Add source", "Добавить источник"), () => editSource(pid)),
  );
  for (const record of records) {
    const source = record.content;
    const box = element("section", "", "panel stack");
    const endpoint =
      source.kind === "mcp" && source.connection
        ? (
            await api(
              `/projects/${pid}/connections/${source.connection.id}?revision=${source.connection.revision}`,
            )
          ).content.endpoint
        : source.kind === "local_folder"
          ? `${initial.mounted_root}/${source.uri}`
          : source.uri;
    if (!isCurrent()) return;
    box.append(
      element(
        "p",
        t(
          () => `Exact read destination: ${endpoint}`,
          () => `Точное направление чтения: ${endpoint}`,
        ),
      ),
    );
    box.append(
      element("h3", `${source.name} · r${record.revision}`),
      element(
        "p",
        t(
          () =>
            `${source.kind} · ${source.uri} · purpose ${source.purpose} · priority ${source.priority} · ${source.enabled ? "enabled" : "disabled"}`,
          () =>
            `${source.kind} · ${source.uri} · purpose ${source.purpose} · priority ${source.priority} · ${source.enabled ? "включён" : "отключён"}`,
        ),
      ),
      element(
        "pre",
        JSON.stringify(
          {
            paths: source.paths,
            pages: source.pages,
            tool: source.tool,
            arguments: source.arguments,
            connection: source.connection,
            authority: source.authority,
            freshness: source.freshness,
            max_files: source.max_files,
            max_bytes: source.max_bytes,
          },
          null,
          2,
        ),
      ),
    );
    const consent = { revision: record.revision, expected_sha256: record.sha256, confirmed: true };
    box.append(
      action(
        t(
          () => `Edit source: ${source.name}`,
          () => `Редактировать источник: ${source.name}`,
        ),
        () => editSource(pid, record),
      ),
      action(
        t(
          () => `Accept displayed read scope: ${source.name}`,
          () => `Принять показанный read scope: ${source.name}`,
        ),
        async () => {
          await post(`/projects/${pid}/sources/${record.id}/grants`, consent);
          if (projectId === pid) await render();
        },
      ),
      action(
        t(
          () => `Revoke read scope: ${source.name}`,
          () => `Отозвать read scope: ${source.name}`,
        ),
        async () => {
          await api(`/projects/${pid}/sources/${record.id}/grants`, { method: "DELETE" });
          if (projectId === pid) await render();
        },
      ),
    );
    box.append(
      element(
        "p",
        t(
          () => `Approved: ${record.granted_scopes.join(", ") || "none"}`,
          () => `Принято: ${record.granted_scopes.join(", ") || "нет"}`,
        ),
      ),
    );
    box.append(
      action(
        t(
          () => `Read source to test: ${source.name}`,
          () => `Прочитать источник для проверки: ${source.name}`,
        ),
        async () => {
          const result = await post(`/projects/${pid}/sources/${record.id}/test`, consent);
          details(
            t("Live read of the selected source", "Реальное чтение выбранного источника"),
            result,
          );
        },
      ),
      action(
        t(
          () => `Import selected snapshots: ${source.name}`,
          () => `Загрузить выбранные snapshots: ${source.name}`,
        ),
        async () => {
          const result = await post(`/projects/${pid}/sources/${record.id}/ingest`, consent);
          if (projectId === pid) await render();
          details(
            t(
              "Import result · catalog not yet enabled",
              "Результат загрузки · каталог ещё не активирован",
            ),
            result,
          );
        },
      ),
    );
    if (record.capability_report)
      box.append(
        action(
          t(
            () => `Latest actual result: ${source.name}`,
            () => `Последний фактический результат: ${source.name}`,
          ),
          () =>
            details(
              `Source report · ${record.capability_report.tested_at}`,
              record.capability_report,
            ),
        ),
      );
    content.append(box);
  }
  const list = element("section");
  content.append(element("h3", t("Saved source snapshots", "Сохранённые source snapshots")), list);
  let offset = 0;
  async function page() {
    const snapshots = await api(`/projects/${pid}/source-snapshots?offset=${offset}&limit=25`);
    if (!isCurrent()) return;
    list.replaceChildren();
    for (const snapshot of snapshots) {
      const box = element("section", "", "panel stack");
      box.append(
        element("h3", snapshot.uri),
        element("p", `${snapshot.created_at} UTC`),
        element("pre", JSON.stringify(snapshot.metadata, null, 2)),
      );
      const raw = element(
        "a",
        t(
          () => `Download original bytes · ${snapshot.raw_sha256.slice(0, 12)}`,
          () => `Скачать исходные bytes · ${snapshot.raw_sha256.slice(0, 12)}`,
        ),
      );
      raw.href = `/api/v1/projects/${pid}/source-snapshots/${snapshot.id}/raw`;
      raw.download = "source-original.bin";
      box.append(raw);
      if (snapshot.document_id)
        box.append(element("p", `Document snapshot: ${snapshot.document_id}`));
      if (snapshot.catalog_import_id)
        box.append(
          action(t("Preview source catalog", "Preview каталога из источника"), async () => {
            const preview = await api(
              `/projects/${pid}/catalog-imports/${snapshot.catalog_import_id}`,
            );
            details("Catalog preview: explicit acceptance required", preview);
            if (!preview.issues.length)
              $("#detail-content").append(
                action(
                  t("Accept displayed catalog and scope", "Принять показанный каталог и scope"),
                  async () => {
                    await post(`/projects/${pid}/catalog-imports/${preview.id}/commit`, {
                      confirmed: true,
                      expected_sha256: preview.sha256,
                    });
                    $("#details").close();
                    if (projectId === pid) await render();
                  },
                ),
              );
          }),
        );
      list.append(box);
    }
    if (!snapshots.length)
      list.append(element("p", t("No snapshots yet.", "Snapshots пока нет."), "empty"));
    if (offset)
      list.append(
        action(t("Source snapshots: back", "Source snapshots: назад"), async () => {
          offset = Math.max(0, offset - 25);
          await page();
        }),
      );
    if (snapshots.length === 25)
      list.append(
        action(t("Source snapshots: next", "Source snapshots: далее"), async () => {
          offset += 25;
          await page();
        }),
      );
  }
  await page();
}

/** @param {string} pid
 * @param {RegistryRecord<import('./api.generated').APISourceDefinition> | null} [record]
 */
async function editSource(pid, record = null) {
  const [connections, initial] = await Promise.all([
    api(`/projects/${pid}/connections`),
    api("/source-defaults"),
  ]);
  /** @type {Partial<import('./api.generated').APISourceDefinition>} */
  const source = record?.content || {};
  const box = $("#detail-content");
  box.replaceChildren(element("h2", t("Source version", "Версия источника")));
  const form = element("form", "", "stack");
  const name = connectionField(
    form,
    t("Source name", "Название источника"),
    source.name || t("Product documentation", "Документация продукта"),
  );
  const kind = connectionSelect(
    form,
    t("Source type", "Тип источника"),
    source.kind || "local_folder",
    [
      ["local_folder", t("Selected folder inside /sources", "Выбранная папка внутри /sources")],
      ["https", t("Specified HTTP(S) pages", "Указанные HTTP(S)-страницы")],
      ["mcp", t("Read-only MCP through broker", "Read-only MCP через broker")],
    ],
  );
  const uri = connectionField(
    form,
    t("URI / relative root inside /sources", "URI / относительный корень внутри /sources"),
    source.uri || ".",
  );
  const purpose = connectionSelect(
    form,
    t("Source purpose", "Назначение источника"),
    source.purpose || "master_spec",
    [
      ["master_spec", t("Current state / baseline", "Текущее состояние / baseline")],
      ["context", t("Additional context", "Дополнительный контекст")],
      [
        "catalog",
        t("Catalog — preview only until accepted", "Каталог — только preview до принятия"),
      ],
    ],
  );
  const priority = connectionField(
    form,
    t(
      "Source priority (does not permit hiding conflicts)",
      "Приоритет источника (не разрешает скрывать конфликты)",
    ),
    source.priority ?? 50,
    "number",
  );
  const authority = connectionField(
    form,
    t("Authority / purpose", "Авторитетность / назначение"),
    source.authority || "user_selected_source",
  );
  const required = connectionField(
    form,
    t("Required source for the selected run", "Обязательный источник для выбранного run"),
    source.required ?? true,
    "checkbox",
  );
  const paths = connectionField(
    form,
    t(
      "Selected relative files, one per line",
      "Выбранные относительные файлы, по одному на строку",
    ),
    (source.paths || []).join("\n"),
    "textarea",
  );
  const directoryConsent = connectionField(
    form,
    t(
      "I choose to list files in the specified folder when the file list is empty",
      "Я выбираю перечисление файлов указанной папки, если список файлов пуст",
    ),
    false,
    "checkbox",
  );
  const recursive = connectionField(
    form,
    t(
      "Explicitly traverse subfolders of the selected root",
      "Явно обходить подпапки выбранного корня",
    ),
    source.recursive || false,
    "checkbox",
  );
  const exclusions = connectionField(
    form,
    t("Exclusions, one per line", "Exclusions, по одному на строку"),
    (source.exclusions || initial.default_exclusions).join("\n"),
    "textarea",
  );
  form.append(
    element(
      "p",
      t(
        "Secret files, .git, fixtures/QA/expected/oracle, and symlinks are always excluded. Paths outside the mount are inaccessible. To select another host folder, use a read-only Compose mount, then accept its scope in the UI.",
        "Secret files, .git, fixtures/QA/expected/oracle и symlinks имеют обязательные исключения. Пути вне mount недоступны. Для выбора другой host-папки используйте read-only Compose mount, затем примите scope в UI.",
      ),
      "notice",
    ),
  );
  const pages = connectionField(
    form,
    t(
      "Additional exact pages on the same origin, one per line",
      "Дополнительные точные страницы того же origin, по одной на строку",
    ),
    (source.pages || []).join("\n"),
    "textarea",
  );
  const addresses = connectionField(
    form,
    t(
      "Allowed private/loopback source IPs (comma-separated)",
      "Разрешённые private/loopback IP источника (через запятую)",
    ),
    (source.local_addresses || []).join(", "),
  );
  const auth = connectionSelect(
    form,
    t("HTTP(S) source authentication", "Авторизация источника HTTP(S)"),
    source.auth || "none",
    [
      ["none", t("No authentication", "Без auth")],
      ["bearer", t("Static bearer via secret reference", "Static bearer через secret reference")],
    ],
  );
  const token = connectionField(
    form,
    t("New source token — memory only", "Новый source token — только в памяти"),
    "",
    "password",
  );
  const file = connectionField(
    form,
    t(
      "Source uses separately mounted file:llm_api_key",
      "Source использует отдельно смонтированный file:llm_api_key",
    ),
    source.secret_ref === "file:llm_api_key",
    "checkbox",
  );
  const mcps = connections.filter((item) => item.content.kind === "mcp");
  const connection = connectionSelect(
    form,
    "Pinned MCP connection revision",
    source.connection?.id || "",
    [
      ["", t("Select a tested MCP profile", "Выберите проверенный MCP профиль")],
      ...mcps.map((item) => [item.id, `${item.content.name} · r${item.revision}`]),
    ],
  );
  const tool = connectionField(
    form,
    t("Exact allowlisted MCP tool name", "Точное allowlisted MCP tool name"),
    source.tool || "",
  );
  const argumentsField = connectionField(
    form,
    t("Visible typed MCP arguments (JSON)", "Видимые typed MCP arguments (JSON)"),
    JSON.stringify(source.arguments || {}, null, 2),
    "textarea",
  );
  const catalogFormat = connectionSelect(
    form,
    t("Catalog format in MCP text result", "Формат каталога в MCP text result"),
    source.catalog_format || "json",
    [
      ["json", "JSON"],
      ["yaml", "YAML"],
    ],
  );
  const timeout = connectionField(
    form,
    t("Total read timeout, seconds", "Общий timeout чтения, секунд"),
    source.timeout || 30,
    "number",
  );
  const count = connectionField(
    form,
    t("File / page limit", "Лимит файлов / страниц"),
    source.max_files || 100,
    "number",
  );
  const maxBytes = connectionField(
    form,
    t(
      "Single-file limit, bytes (docs ≤ 1 MiB; catalog ≤ 32 MiB)",
      "Лимит одного файла, bytes (docs ≤ 1 MiB; catalog ≤ 32 MiB)",
    ),
    source.max_bytes || 1048576,
    "number",
  );
  const totalBytes = connectionField(
    form,
    t("Total read limit, bytes (docs ≤ 10 MiB)", "Лимит всего чтения, bytes (docs ≤ 10 MiB)"),
    source.max_total_bytes || 10485760,
    "number",
  );
  const enabled = connectionField(
    form,
    t(
      "Enable source (reading requires separate scope approval)",
      "Включить источник (чтение требует отдельного принятия scope)",
    ),
    source.enabled || false,
    "checkbox",
  );
  const message = element("p", "", "error");
  message.setAttribute("role", "alert");
  const submit = element(
    "button",
    t("Save new source version", "Сохранить новую version источника"),
  );
  submit.type = "submit";
  form.append(submit, message);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      if (
        kind.value === "local_folder" &&
        !lineValues(paths.value).length &&
        !directoryConsent.checked
      )
        throw new Error(
          t(
            "Select files or explicitly allow listing the specified folder.",
            "Выберите файлы или явно разрешите перечисление указанной папки.",
          ),
        );
      let secret = source.secret_ref || null;
      if (kind.value === "https" && auth.value === "bearer" && token.value)
        secret = (await post(`/projects/${pid}/secrets`, { value: token.value })).secret_ref;
      if (file.checked) secret = "file:llm_api_key";
      const selected = mcps.find((item) => item.id === connection.value);
      const value = {
        name: name.value,
        kind: kind.value,
        uri: uri.value,
        purpose: purpose.value,
        catalog_format: catalogFormat.value,
        priority: Number(priority.value),
        authority: authority.value,
        required: required.checked,
        freshness: "pinned_manual_refresh",
        enabled: enabled.checked,
        paths: kind.value === "local_folder" ? lineValues(paths.value) : [],
        recursive: kind.value === "local_folder" && recursive.checked,
        exclusions: lineValues(exclusions.value),
        pages: kind.value === "https" ? lineValues(pages.value) : [],
        crawl_depth: 0,
        local_addresses: kind.value === "https" ? idValues(addresses.value) : [],
        auth: kind.value === "https" ? auth.value : "none",
        secret_ref: kind.value === "https" && auth.value === "bearer" ? secret : null,
        connection:
          kind.value === "mcp" && selected
            ? { id: selected.id, revision: selected.revision }
            : null,
        tool: kind.value === "mcp" ? tool.value : "",
        arguments: kind.value === "mcp" ? JSON.parse(argumentsField.value) : {},
        timeout: Number(timeout.value),
        max_files: Number(count.value),
        max_bytes: Number(maxBytes.value),
        max_total_bytes: Number(totalBytes.value),
      };
      await post(
        `/projects/${pid}/sources${record ? `/${record.id}` : ""}`,
        record ? { expected_revision: record.revision, source: value } : value,
        record ? "PUT" : "POST",
      );
      token.value = "";
      $("#details").close();
      if (projectId === pid) await render();
    } catch (error) {
      message.textContent = errorMessage(error);
    } finally {
      submit.disabled = false;
    }
  });
  box.append(form);
  if (!$("#details").open) $("#details").showModal();
}

export { renderSources, editSource };
