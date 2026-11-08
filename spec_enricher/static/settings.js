import { t, getLanguage } from "./i18n.js";
import { connectionField } from "./connections.js";
import { checkedList } from "./runs.js";
import {
  csrf,
  projectId,
  projects,
  setProject,
  element,
  setStatus,
  api,
  post,
  action,
  showError,
  details,
  refreshProjects,
  render,
} from "./app.js";
import { renderPendingPurges } from "./lifecycle.js";

async function renderSettings(content, isCurrent) {
  const receipts = await api("/settings/imports");
  if (!isCurrent()) return;
  content.append(
    element("h2", t("Settings transfer and data", "Перенос настроек и данные")),
    element(
      "p",
      t(
        "Settings are transferred separately from documents and run history. Use CLI backup/restore to transfer full history. JSON and backups are not encrypted.",
        "Настройки переносятся отдельно от документов и run history. Полный перенос истории выполняется CLI backup/restore. JSON и backup не зашифрованы.",
      ),
      "notice",
    ),
  );
  const exportForm = element("form", "", "stack panel");
  exportForm.append(
    element("h3", t("Export settings-bundle.json", "Экспорт settings-bundle.json")),
  );
  const selected = checkedList(
    exportForm,
    t(
      "Explicitly selected projects for configuration export",
      "Явно выбранные проекты для экспорта конфигурации",
    ),
    projects,
    projectId ? [projectId] : [],
    (project) => project.name,
  );
  const catalogues = connectionField(
    exportForm,
    t(
      "Include selected catalog versions with authored/source texts (without project evidence)",
      "Включить выбранные версии каталогов с authored/source текстами (без project evidence)",
    ),
    false,
    "checkbox",
  );
  const sanitized = connectionField(
    exportForm,
    t(
      "Sanitized sharing template: hide addresses/paths and reset prompt/skill texts",
      "Sanitized sharing template: скрыть адреса/пути и сбросить prompt/skill тексты",
    ),
    false,
    "checkbox",
  );
  exportForm.append(
    element(
      "p",
      t(
        "Regular export preserves prompts and exact version references; endpoints, paths, and texts may reveal internal information. The sanitized option resets texts to defaults and requires a new review. Secret values, documents, and grants are excluded.",
        "Обычный экспорт сохраняет промпты и точные version refs; endpoints, пути и тексты могут раскрывать внутреннюю информацию. Sanitized-вариант сбрасывает тексты к defaults и требует нового review. Secret values, документы и grants не включаются.",
      ),
      "notice",
    ),
  );
  const preview = element("section", "", "panel stack");
  const submit = element(
    "button",
    t("Prepare settings export preview", "Подготовить preview экспорта настроек"),
  );
  submit.type = "submit";
  exportForm.append(submit);
  /** @type {HTMLButtonElement | null} */
  let downloadButton = null;
  exportForm.addEventListener("input", () => {
    if (downloadButton) downloadButton.disabled = true;
  });
  exportForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const selection = {
        project_ids: selected(),
        include_catalogues: catalogues.checked,
        sanitized: sanitized.checked,
      };
      const result = await post("/settings/export/preview", selection);
      if (!isCurrent()) return;
      preview.replaceChildren(
        element(
          "h3",
          t("Exact contents of the transferable bundle", "Точный состав переносимого bundle"),
        ),
        element("p", result.notice, "notice"),
        element("pre", JSON.stringify(result.preview, null, 2)),
      );
      const inspect = element("details");
      inspect.append(
        element("summary", t("Inspect actual settings JSON", "Просмотреть actual settings JSON")),
      );
      inspect.addEventListener("toggle", () => {
        if (inspect.open && inspect.children.length === 1)
          inspect.append(element("pre", JSON.stringify(result.bundle, null, 2)));
      });
      preview.append(inspect);
      downloadButton = action(
        t(
          "Accept displayed contents and download settings-bundle.json",
          "Принять показанный состав и скачать settings-bundle.json",
        ),
        async () => {
          const response = await fetch("/api/v1/settings/export", {
            method: "POST",
            headers: {
              "X-UI-Language": getLanguage(),
              "Content-Type": "application/json",
              "X-CSRF-Token": csrf,
            },
            body: JSON.stringify({
              ...selection,
              expected_sha256: result.preview.sha256,
              confirmed: true,
            }),
          });
          if (!response.ok)
            throw new Error(
              (await response.json()).message || t("Export blocked.", "Экспорт заблокирован."),
            );
          const url = URL.createObjectURL(await response.blob());
          const link = element(
            "a",
            t("Download settings-bundle.json", "Скачать settings-bundle.json"),
          );
          link.href = url;
          link.download = "settings-bundle.json";
          document.body.append(link);
          link.click();
          link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          setStatus(
            t(
              "Settings exported without keys or documents.",
              "Настройки экспортированы без ключей и документов.",
            ),
          );
        },
      );
      preview.append(downloadButton);
    } catch (error) {
      showError(error);
    } finally {
      submit.disabled = false;
    }
  });
  content.append(exportForm, preview);
  const importForm = element("form", "", "stack panel");
  importForm.append(
    element("h3", t("Import into new isolated projects", "Импорт в новые изолированные проекты")),
  );
  const id = "settings-file";
  const label = element("label", t("Choose settings-bundle.json", "Выбрать settings-bundle.json"));
  label.htmlFor = id;
  const input = element("input");
  input.id = id;
  input.type = "file";
  input.accept = ".json";
  input.required = true;
  const button = element(
    "button",
    t("Validate settings bundle and remapping", "Проверить settings bundle и remapping"),
  );
  button.type = "submit";
  const importPreview = element("section", "", "stack");
  importForm.append(label, input, button, importPreview);
  importForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    try {
      const file = input.files?.[0];
      if (!file) throw new Error(t("Choose a settings bundle.", "Выберите settings bundle."));
      if (file.size > 32 * 1024 * 1024)
        throw new Error(t("Settings bundle exceeds 32 MiB.", "Settings bundle превышает 32 MiB."));
      const result = await api("/settings/import/preview", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      if (!isCurrent()) return;
      importPreview.replaceChildren(
        element("pre", JSON.stringify(result, null, 2)),
        element(
          "p",
          t(
            "New project scopes will be created. Connections, sources, skills, and catalog bindings will be disabled; keys and paths must be configured again. Import makes no network requests.",
            "Будут созданы новые project scopes. Connections, sources, skills и catalog bindings отключены; ключи и пути нужно настроить заново. Сетевых запросов при импорте нет.",
          ),
          "notice",
        ),
        action(
          t(
            "Accept this import preview and create new scopes",
            "Принять этот import preview и создать новые scopes",
          ),
          async () => {
            const receipt = await post("/settings/import/commit", {
              preview_id: result.preview_id,
              expected_sha256: result.sha256,
              confirmed: true,
            });
            if (!isCurrent()) {
              await refreshProjects();
              return;
            }
            if (receipt.projects.length) setProject(receipt.projects[0].project_id);
            await refreshProjects();
            await render();
            details(
              t(
                "Transfer result · unresolved aliases need configuration",
                "Результат переноса · unresolved aliases требуют настройки",
              ),
              receipt,
            );
          },
        ),
      );
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
    }
  });
  content.append(
    importForm,
    action(
      t("Clear temporary settings previews", "Очистить временные settings previews"),
      async () => {
        await api("/settings/import/previews", { method: "DELETE" });
        setStatus(
          t(
            "Temporary previews deleted; imported projects retained.",
            "Временные previews удалены; импортированные проекты сохранены.",
          ),
        );
      },
    ),
  );
  content.append(
    element(
      "h3",
      t("Import results and unresolved mappings", "Результаты импорта и unresolved mappings"),
    ),
  );
  for (const item of receipts)
    content.append(
      action(`${item.created_at} · ${item.sha256.slice(0, 12)}`, () =>
        details(
          t("Keys/paths/catalogs to reconfigure", "Ключи/пути/каталоги для повторной настройки"),
          item.receipt,
        ),
      ),
    );
  content.append(
    element("h3", t("Storage and recovery", "Хранение и восстановление")),
    element(
      "p",
      t(
        "Snapshots and results are retained until explicitly deleted by the user. Diagnostic/audit metadata: 14 days. Deletion is logical; secure SSD erasure and deletion of external backups are not guaranteed.",
        "Snapshots и результаты хранятся до явного удаления пользователем. Diagnostic/audit metadata — 14 дней. Удаление логическое; secure erase SSD и удаление внешних backups не обещаются.",
      ),
    ),
    element(
      "p",
      "Backup: docker compose exec app python -m spec_enricher.cli backup --output /backups/backup-local.tar.gz",
    ),
    element(
      "p",
      t(
        "Restore with the service stopped: docker compose run --rm --no-deps app python -m spec_enricher.cli restore --input /backups/backup-local.tar.gz. See README.md (Backup and restore).",
        "Restore выполняется при остановленном сервисе: docker compose run --rm --no-deps app python -m spec_enricher.cli restore --input /backups/backup-local.tar.gz. Подробности — README.md, раздел Backup and restore.",
      ),
    ),
  );
  await renderPendingPurges(content, isCurrent);
}

export { renderSettings };
