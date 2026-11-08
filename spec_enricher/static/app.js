import { t, getLanguage, initializeLanguage } from "./i18n.js";
initializeLanguage();
import { renderConnections, connectionSelect } from "./connections.js";
import { renderConfiguration } from "./configuration.js";
import { selectedRuns, closeRunEvents, renderNewRun, renderHistory, renderRun } from "./runs.js";
import { editRequirement, authoringPanel } from "./authoring.js";
import { renderSources } from "./sources.js";
import { renderSettings } from "./settings.js";
import { purgeDialog, removeCatalogBinding } from "./lifecycle.js";
import { domainPanel, updateDialog, requirementDetails } from "./catalog-management.js";
// @ts-check
/** @template {keyof UIElements} K
 * @param {K} selector
 * @returns {UIElements[K]}
 */
function $(selector) {
  const value = document.querySelector(selector);
  if (!value && selector !== "#page-error")
    throw new Error(
      t(
        `Missing required UI control: ${selector}`,
        `Отсутствует обязательный UI control: ${selector}`,
      ),
    );
  return /** @type {UIElements[K]} */ (value);
}
let csrf = "";
let projectId = "";
let projects = [];
let projectListGeneration = 0;
let view = "overview";
let renderGeneration = 0;
/** @type {HTMLButtonElement | null} */
let dialogReturnFocus = null;
function setProject(value) {
  projectListGeneration += 1;
  projectId = value;
}
function setView(value) {
  view = value;
}
/** @param {unknown} value @returns {string} */
function errorMessage(value) {
  return value instanceof Error
    ? value.message
    : t("The action could not be completed.", "Не удалось выполнить действие.");
}

/** @template {keyof HTMLElementTagNameMap} K
 * @param {K} tag
 * @param {string | number} [text]
 * @param {string} [className]
 * @returns {HTMLElementTagNameMap[K]}
 */
function element(tag, text = "", className = "") {
  const result = document.createElement(tag);
  result.textContent = String(text);
  // Prompts/artifacts can exceed the CSS height limit. Their scroll area must
  // remain reachable with the keyboard, including in Safari.
  if (tag === "pre") result.tabIndex = 0;
  if (className) result.className = className;
  return result;
}
function setStatus(text) {
  $("#status").textContent = text;
}
async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers: { "X-UI-Language": getLanguage(), "X-CSRF-Token": csrf, ...(options.headers || {}) },
  });
  const scoped =
    path.match(/^\/projects\/([^/?]+)/) || path.match(/^\/requirement-drafts\/project\/([^/?]+)/);
  const checkScope = () => {
    if (scoped && scoped[1] !== projectId)
      throw new DOMException(
        t("Project changed; late response hidden.", "Проект изменён; поздний ответ скрыт."),
        "AbortError",
      );
  };
  checkScope();
  if (response.status === 401) {
    $("#login").showModal();
    throw new Error(t("Local sign-in is required.", "Требуется локальный вход."));
  }
  if (!response.ok) {
    const body = await response.json();
    checkScope();
    throw new Error(
      body.message || t("The request could not be completed.", "Не удалось выполнить запрос."),
    );
  }
  const body = await response.json();
  checkScope();
  return body;
}
function post(path, value = {}, method = "POST") {
  return api(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  });
}
function action(text, callback) {
  const button = element("button", text);
  button.type = "button";
  button.addEventListener("click", async () => {
    if (!document.querySelector("dialog[open]")) dialogReturnFocus = button;
    button.disabled = true;
    try {
      await callback();
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}
function showError(error) {
  if (error?.name === "AbortError") return;
  const text =
    error instanceof Error
      ? errorMessage(error)
      : t("The action could not be completed.", "Не удалось выполнить действие.");
  setStatus(text);
  let alert = $("#page-error");
  if (!alert) {
    alert = element("p", "", "error");
    alert.id = "page-error";
    alert.setAttribute("role", "alert");
    $("#content").prepend(alert);
  }
  alert.textContent = text;
}
function details(title, data) {
  const content = $("#detail-content");
  content.replaceChildren(
    element("h2", title),
    element("pre", typeof data === "string" ? data : JSON.stringify(data, null, 2)),
  );
  if (!$("#details").open) $("#details").showModal();
}
async function refreshProjects() {
  const generation = ++projectListGeneration;
  const nextProjects = await api("/projects");
  if (generation !== projectListGeneration) return;
  projects = nextProjects;
  const select = $("#project");
  select.replaceChildren(new Option(t("Select a product", "Выберите продукт"), ""));
  projects.forEach((project) => {
    select.add(new Option(project.name, project.id));
  });
  if (!projects.some((project) => project.id === projectId)) projectId = "";
  select.value = projectId;
}
async function render() {
  closeRunEvents();
  const generation = ++renderGeneration;
  const pid = projectId;
  const content = $("#content");
  content.replaceChildren();
  document.querySelectorAll("button").forEach((button) => {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const current = projects.find((project) => project.id === pid);
  if (view === "settings") {
    setStatus(t("Loading…", "Загрузка…"));
    try {
      await renderSettings(content, () => generation === renderGeneration);
      if (generation === renderGeneration) setStatus(t("Ready", "Готово"));
    } catch (error) {
      if (generation === renderGeneration) showError(error);
    }
    return;
  }
  if (!current) {
    content.append(
      element("h2", t("Create a product", "Создайте продукт")),
      element(
        "p",
        t(
          "Each product has separate documents, requirements, and history.",
          "Документы, требования и история каждого продукта хранятся отдельно.",
        ),
      ),
      action(t("Create product", "Создать продукт"), () => $("#project-dialog").showModal()),
    );
    return;
  }
  setStatus(t("Loading…", "Загрузка…"));
  try {
    if (view === "overview") {
      content.append(
        element("h2", current.name),
        element(
          "p",
          current.description ||
            t("The product scope has not been described yet.", "Область продукта пока не описана."),
        ),
      );
      const panel = element("section", "", "panel");
      panel.append(
        element("h3", t("Prepare a security review", "Подготовка security review")),
        element(
          "p",
          t(
            "Upload current product documentation, add a requirements catalog, and prepare a Markdown delta.",
            "Загрузите текущую документацию продукта, добавьте каталог требований и подготовьте delta в Markdown.",
          ),
        ),
      );
      panel.append(
        action(t("Upload documentation", "Загрузить документацию"), () => {
          view = "documents";
          return render();
        }),
        action(t("Add requirements", "Добавить требования"), () => {
          view = "catalogs";
          return render();
        }),
      );
      content.append(
        panel,
        element(
          "p",
          t(
            "Analysis results describe the specification. The product implementation and operational controls are not verified.",
            "Результаты анализа относятся к спецификации. Реализация и эксплуатационные контроли оцениваемого продукта не проверяются.",
          ),
          "notice",
        ),
      );
    } else if (view === "documents") {
      content.append(element("h2", t("Product documentation", "Документация продукта")));
      const form = element("form", "", "stack panel");
      const role = element("select");
      role.id = "upload-role";
      [
        ["master_spec", t("Current state / baseline", "Текущее состояние / baseline")],
        ["context", t("Context material", "Контекстный материал")],
        ["delta", t("Delta — product change", "Delta — изменение продукта")],
      ].forEach(([value, text]) => {
        role.add(new Option(text, value));
      });
      const label = element("label", t("File purpose", "Назначение файлов"));
      label.htmlFor = role.id;
      const input = element("input");
      input.id = "document-files";
      input.type = "file";
      input.multiple = true;
      input.accept = ".md,.json,.yaml,.yml";
      input.required = true;
      const fileLabel = element("label", t("Choose documents", "Выбрать документы"));
      fileLabel.htmlFor = input.id;
      const submit = element("button", t("Upload documents", "Загрузить документы"));
      submit.type = "submit";
      const results = element("div");
      results.setAttribute("role", "status");
      form.append(
        label,
        role,
        fileLabel,
        input,
        element(
          "p",
          t(
            "Up to 20 files, 1 MiB each; delta: Markdown up to 100 KiB. Product corpus: up to 10 MiB.",
            "До 20 файлов, до 1 MiB каждый; delta — Markdown до 100 KiB. Корпус продукта — до 10 MiB.",
          ),
          "muted",
        ),
        submit,
        results,
      );
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        const selected = Array.from(input.files || []);
        results.replaceChildren();
        if (selected.length > 20) {
          results.append(
            element(
              "p",
              t("Select no more than 20 files.", "Выберите не более 20 файлов."),
              "error",
            ),
          );
          submit.disabled = false;
          return;
        }
        for (const file of selected) {
          try {
            const result = await api(
              `/projects/${pid}/documents?filename=${encodeURIComponent(file.name)}&role=${role.value}`,
              {
                method: "POST",
                headers: { "Content-Type": "application/octet-stream" },
                body: file,
              },
            );
            results.append(
              element(
                "p",
                t(
                  `${file.name}: saved, SHA-256 ${result.sha256}`,
                  `${file.name}: сохранён, SHA-256 ${result.sha256}`,
                ),
                "file-result",
              ),
            );
          } catch (error) {
            results.append(
              element("p", `${file.name}: ${errorMessage(error)}`, "error file-result"),
            );
          }
        }
        submit.disabled = false;
        if (projectId === pid) await renderDocumentList(pid, list);
      });
      const list = element("section");
      content.append(form, list);
      await renderDocumentList(pid, list);
    } else if (view === "analysis") {
      await renderNewRun(pid, content, () => generation === renderGeneration);
    } else if (view === "history") {
      await renderHistory(pid, content, () => generation === renderGeneration);
    } else if (view === "run") {
      await renderRun(pid, selectedRuns.get(pid), content, () => generation === renderGeneration);
    } else if (view === "sources") {
      await renderSources(pid, content, () => generation === renderGeneration);
    } else if (view === "connections") {
      await renderConnections(pid, content, () => generation === renderGeneration);
    } else if (view === "configuration") {
      await renderConfiguration(pid, content, () => generation === renderGeneration);
    } else if (view === "catalogs") {
      content.append(
        element("h2", t("Cybersecurity requirements", "Требования кибербезопасности")),
      );
      await authoringPanel(pid, content, () => generation === renderGeneration);
      if (generation !== renderGeneration) return;
      const builtin = await api("/builtin-catalog");
      const visibility = await api("/builtin/visibility");
      if (generation !== renderGeneration) return;
      const panel = element("section", "", "panel");
      panel.append(
        element("h3", builtin.title),
        element(
          "p",
          t(
            `${builtin.records} records / ${builtin.active_records} active. Version ${builtin.catalog_version}.`,
            `${builtin.records} записей / ${builtin.active_records} активных. Версия ${builtin.catalog_version}.`,
          ),
        ),
        element("p", builtin.review_scope, "notice"),
      );
      panel.append(
        action(t("Provenance and limitations", "Происхождение и ограничения"), () =>
          details(t("Built-in bundle", "Встроенный набор"), builtin),
        ),
        action(
          t("Accept and enable the built-in bundle", "Принять и включить встроенный набор"),
          async () => {
            await post(`/projects/${pid}/builtin-catalog`, {
              confirmed: true,
              expected_sha256: builtin.sha256,
            });
            if (pid === projectId) await render();
          },
        ),
      );
      if (!visibility.hidden) content.append(panel);
      await domainPanel(pid, panel, () => generation === renderGeneration);
      if (generation !== renderGeneration) return;
      const importForm = element("form", "", "stack panel");
      const label = element(
        "label",
        t("Import your JSON/YAML catalog", "Импорт своего каталога JSON/YAML"),
      );
      label.htmlFor = "catalog-file";
      const input = element("input");
      input.type = "file";
      input.id = "catalog-file";
      input.accept = ".json,.yaml,.yml";
      input.required = true;
      const submit = element(
        "button",
        t("Validate and open preview", "Проверить и открыть preview"),
      );
      submit.type = "submit";
      const preview = element("div");
      importForm.append(label, input, submit, preview);
      importForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          const file = input.files?.[0];
          if (!file)
            throw new Error(t("Choose a catalog to preview.", "Выберите каталог для preview."));
          const format = (file.name.split(".").pop() || "").toLowerCase();
          const value = await api(
            `/projects/${pid}/catalog-imports/preview?format=${encodeURIComponent(format)}`,
            { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file },
          );
          preview.replaceChildren(element("pre", JSON.stringify(value, null, 2)));
          if (value.id && value.issues.length === 0) {
            preview.append(
              action(
                t("Accept preview and enable catalog", "Принять preview и включить каталог"),
                async () => {
                  await post(`/projects/${pid}/catalog-imports/${value.id}/commit`, {
                    confirmed: true,
                    expected_sha256: value.sha256,
                  });
                  if (pid === projectId) await render();
                },
              ),
            );
            preview.append(
              action(
                t(
                  "Compare as an update · mappings / conflicts",
                  "Сравнить как обновление · mappings / conflicts",
                ),
                () => updateDialog(pid, { import_id: value.id }),
              ),
            );
          }
        } catch (error) {
          showError(error);
        } finally {
          submit.disabled = false;
        }
      });
      content.append(importForm);
      const catalogs = await api(`/projects/${pid}/catalogs`);
      if (generation !== renderGeneration) return;
      for (const catalog of catalogs) {
        const section = element("section", "", "panel");
        section.append(
          element("h3", `${catalog.catalog_id} · ${catalog.version}`),
          element(
            "p",
            t(
              `${catalog.records} records. ${catalog.enabled ? "Enabled" : "Disabled; snapshot retained"}.`,
              `${catalog.records} записей. ${catalog.enabled ? "Подключён" : "Отключён; снимок сохранён"}.`,
            ),
          ),
        );
        for (const format of ["json", "yaml"]) {
          const link = element(
            "a",
            t(`Export ${format.toUpperCase()}`, `Экспорт ${format.toUpperCase()}`),
            "download-action",
          );
          link.href = `/api/v1/projects/${pid}/catalogs/${catalog.id}/export?format=${format}`;
          link.download = "";
          section.append(link);
        }
        section.append(
          action(t("Browse requirements", "Просмотр требований"), () =>
            requirementPage(pid, catalog.id, 0),
          ),
          action(t("Source coverage", "Покрытие источников"), async () =>
            details(
              t("Catalog coverage", "Покрытие каталога"),
              await api(`/projects/${pid}/catalogs/${catalog.id}/coverage`),
            ),
          ),
          action(
            t(
              "Preview update against the previous version",
              "Preview обновления относительно прежней версии",
            ),
            () => updateDialog(pid, { candidate_catalog_version_id: catalog.id }),
          ),
          action(
            catalog.enabled ? t("Disable", "Отключить") : t("Enable", "Подключить"),
            async () => {
              await post(
                `/projects/${pid}/catalogs/${catalog.id}/binding`,
                { enabled: !catalog.enabled },
                "PATCH",
              );
              if (pid === projectId) await render();
            },
          ),
        );
        section.append(
          action(t("Remove active binding · preview", "Удалить active binding · preview"), () =>
            removeCatalogBinding(pid, catalog.id),
          ),
          action(
            t(
              "Permanently delete this catalog snapshot · preview",
              "Полная очистка этого catalog snapshot · preview",
            ),
            () => purgeDialog(pid, "catalog", catalog.id),
          ),
        );
        content.append(section);
      }
    }
    if (generation === renderGeneration) setStatus(t("Ready", "Готово"));
  } catch (error) {
    if (generation === renderGeneration) showError(error);
  }
}
async function renderDocumentList(pid, target) {
  const documents = await api(`/projects/${pid}/documents`);
  if (projectId !== pid) return;
  target.replaceChildren(element("h3", t("Saved snapshots", "Сохранённые снимки")));
  if (!documents.length)
    target.append(
      element("p", t("No documents uploaded yet.", "Документы пока не загружены."), "empty"),
    );
  for (const doc of documents) {
    const row = element("div", "", "panel");
    row.append(
      element("h3", doc.name),
      element("p", `${doc.role} · ${doc.size} bytes · ${doc.sha256}`),
      action(t("Open snapshot", "Открыть снимок"), async () => {
        const response = await fetch(`/api/v1/projects/${pid}/documents/${doc.id}`);
        if (projectId !== pid) return;
        if (!response.ok) throw new Error(t("Snapshot unavailable.", "Снимок недоступен."));
        const text = await response.text();
        if (projectId === pid) details(doc.name, text);
      }),
      action(
        t("Permanently delete this snapshot · preview", "Полная очистка этого snapshot · preview"),
        () => purgeDialog(pid, "document", doc.id),
      ),
    );
    target.append(row);
  }
}
async function requirementPage(
  pid,
  cid,
  offset,
  filters = { domain: "", profile: "", level: "", source_kind: "" },
  selected = new Set(),
) {
  const query = new URLSearchParams({ ...filters, offset: String(offset), limit: "25" });
  const records = await api(`/projects/${pid}/catalogs/${cid}/requirements?${query}`);
  const coverage = await api(`/projects/${pid}/catalogs/${cid}/coverage`);
  const panel = $("#detail-content");
  panel.replaceChildren(element("h2", t("Requirements", "Требования")));
  for (const [key, title] of [
    ["domain", t("Catalog domain", "Область каталога")],
    ["profile", "Source profile"],
    ["level", t("Catalog level", "Уровень каталога")],
    ["source_kind", t("Source type", "Тип источника")],
  ]) {
    const select = connectionSelect(panel, title, filters[key], [
      ["", t("All", "Все")],
      ...coverage.filter_values[key].map((value) => [value, value]),
    ]);
    select.addEventListener("change", () =>
      requirementPage(pid, cid, 0, { ...filters, [key]: select.value }, selected).catch(showError),
    );
  }
  for (const record of records) {
    const selectionLabel = element("label");
    const selectionCheck = element("input");
    selectionCheck.type = "checkbox";
    selectionCheck.checked = selected.has(record.id);
    selectionLabel.append(
      selectionCheck,
      document.createTextNode(
        t(` Export ${record.id} · ${record.version}`, ` Экспорт ${record.id} · ${record.version}`),
      ),
    );
    selectionCheck.addEventListener("change", () =>
      selectionCheck.checked ? selected.add(record.id) : selected.delete(record.id),
    );
    panel.append(selectionLabel);
    panel.append(
      action(`${record.id} · ${record.title}`, async () => {
        const value = await api(
          `/projects/${pid}/catalogs/${cid}/requirements/${encodeURIComponent(record.id)}`,
        );
        requirementDetails(value);
        $("#detail-content").append(
          action(
            value.origin.kind === "upstream_source"
              ? t("Create user-owned override", "Создать user-owned override")
              : t("Edit as a new version in the form", "Редактировать новой version в форме"),
            async () => {
              const fields = structuredClone(value);
              if (value.origin.kind === "upstream_source") {
                fields.policy_scope = {
                  ...fields.policy_scope,
                  override_of: await api(
                    `/projects/${pid}/catalogs/${cid}/requirements/${encodeURIComponent(value.id)}/reference`,
                  ),
                };
                fields.id = `USER-OVERRIDE-${crypto.randomUUID().replaceAll("-", "").toUpperCase()}`;
                fields.version = "1.0.0";
                fields.origin = { author: "", external_provenance: null, license: null };
                delete fields.source_refs;
                delete fields.source_context;
                delete fields.record_key;
                for (const part of [
                  ...fields.clauses,
                  ...fields.applicability.predicates,
                  ...Object.values(fields.clause_normativity),
                ])
                  part.source_ref_ids = ["user-source"];
              }
              await editRequirement(pid, null, fields);
            },
          ),
        );
      }),
    );
  }
  const actions = element("div", "", "actions");
  if (offset)
    actions.append(
      action(t("Back", "Назад"), () =>
        requirementPage(pid, cid, Math.max(0, offset - 25), filters, selected),
      ),
    );
  if (records.length === 25)
    actions.append(
      action(t("Next", "Далее"), () => requirementPage(pid, cid, offset + 25, filters, selected)),
    );
  for (const format of ["json", "yaml"])
    actions.append(
      action(
        t(
          `Preview selected export · ${format.toUpperCase()}`,
          `Preview экспорта выбранных · ${format.toUpperCase()}`,
        ),
        async () => {
          if (!selected.size)
            throw new Error(
              t("Select requirements to export.", "Отметьте требования для экспорта."),
            );
          const preview = await post(`/projects/${pid}/catalogs/${cid}/selected-export`, {
            requirement_ids: [...selected],
            format,
          });
          const { content, ...metadata } = preview;
          details(
            t("Selected versions and export fields", "Выбранные версии и поля экспорта"),
            metadata,
          );
          $("#detail-content").append(
            element("pre", content),
            action(
              t(
                `Download selected · ${format.toUpperCase()}`,
                `Скачать выбранные · ${format.toUpperCase()}`,
              ),
              () => {
                const url = URL.createObjectURL(
                  new Blob([content], {
                    type: format === "json" ? "application/json" : "application/yaml",
                  }),
                );
                const link = element("a");
                link.href = url;
                link.download = `requirements-selected.${format}`;
                link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
              },
            ),
          );
        },
      ),
    );
  panel.append(actions);
  if (!$("#details").open) $("#details").showModal();
}
$("#new-project").addEventListener("click", () => $("#project-dialog").showModal());
$("#project-cancel").addEventListener("click", () => $("#project-dialog").close());
$("#details-close").addEventListener("click", () => $("#details").close());
$("#details").addEventListener("close", () => {
  if (dialogReturnFocus?.isConnected && !dialogReturnFocus.disabled) dialogReturnFocus.focus();
  else $("#main").focus();
});
$("#project").addEventListener("change", async () => {
  setProject($("#project").value);
  await render();
});
document.querySelectorAll("button").forEach((button) => {
  if (button.dataset.view)
    button.addEventListener("click", () => {
      view = button.dataset.view || "overview";
      render();
    });
});
$("#project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const project = await post("/projects", {
      name: $("#project-name").value,
      description: $("#project-description").value,
    });
    setProject(project.id);
    await refreshProjects();
    $("#project-dialog").close();
    $("#project-form").reset();
    await render();
  } catch (error) {
    $("#project-error").textContent = errorMessage(error);
  }
});
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const response = await post("/session/bootstrap", { token: $("#login-token").value });
    csrf = response.csrf_token;
    $("#login-token").value = "";
    $("#login").close();
    await refreshProjects();
    $("#new-project").disabled = false;
    $("#project").disabled = false;
    await render();
  } catch (error) {
    $("#login-error").textContent = errorMessage(error);
  }
});
(async () => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("bootstrap");
  history.replaceState(null, "", location.pathname);
  try {
    if (token) csrf = (await post("/session/bootstrap", { token })).csrf_token;
    else csrf = (await api("/session")).csrf_token;
    await refreshProjects();
    await render();
    $("#new-project").disabled = false;
    $("#project").disabled = false;
  } catch {
    if (!$("#login").open) $("#login").showModal();
  }
})();

export {
  errorMessage,
  $,
  csrf,
  projectId,
  projects,
  view,
  renderGeneration,
  dialogReturnFocus,
  setProject,
  setView,
  element,
  setStatus,
  api,
  post,
  action,
  showError,
  details,
  refreshProjects,
  render,
  renderDocumentList,
  requirementPage,
};
