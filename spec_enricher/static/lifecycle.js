import { t } from "./i18n.js";
import { connectionField } from "./connections.js";
import { selectedRuns, analysisPresets } from "./runs.js";
import {
  $,
  projectId,
  setProject,
  element,
  setStatus,
  api,
  post,
  action,
  details,
  refreshProjects,
  render,
} from "./app.js";

async function purgeDialog(pid, kind, objectId) {
  const plan = await post(`/projects/${pid}/purge/preview`, { kind, object_id: objectId });
  details(
    t(
      "Permanently delete selected history · preview",
      "Полное удаление выбранной истории · preview",
    ),
    plan,
  );
  const box = $("#detail-content");
  if (plan.blockers.length) {
    box.append(
      element(
        "p",
        t(
          "Deletion is blocked by references or unfinished runs. Cancel the displayed runs and wait for a terminal state, or separately delete the referencing history first.",
          "Удаление заблокировано ссылками или незавершёнными run. Отмените показанные run и дождитесь terminal state; либо сначала отдельно удалите ссылающуюся историю.",
        ),
        "notice",
      ),
    );
    for (const run of plan.active_runs)
      box.append(
        action(
          t(
            () => `Cancel run ${run.id}`,
            () => `Отменить run ${run.id}`,
          ),
          async () => {
            await post(`/projects/${pid}/runs/${run.id}/cancel`);
            await purgeDialog(pid, kind, objectId);
          },
        ),
      );
    return;
  }
  const phrase = connectionField(
    box,
    t(
      "To permanently delete this history, type DELETE HISTORY",
      "Для необратимого удаления этой истории введите DELETE HISTORY",
    ),
    "",
  );
  const confirm = action(
    t("Delete only the displayed data", "Удалить только показанные данные"),
    async () => {
      if (phrase.value !== "DELETE HISTORY")
        throw new Error(
          t("Enter the exact confirmation phrase.", "Введите точную фразу подтверждения."),
        );
      const result = await post(`/projects/${pid}/purge/commit`, {
        kind,
        object_id: objectId,
        expected_sha256: plan.sha256,
        confirmed: true,
        confirmation: phrase.value,
      });
      $("#details").close();
      if (kind === "project") {
        selectedRuns.delete(pid);
        analysisPresets.delete(pid);
        setProject("");
      }
      await refreshProjects();
      await render();
      if (result.status !== "complete")
        details(
          t(
            "Metadata deleted; retry file cleanup in Settings / Data",
            "Метаданные удалены; очистка файлов требует повторного выполнения в Настройки / Данные",
          ),
          result,
        );
      else
        setStatus(
          t(
            "Selected data deleted; external backups retained.",
            "Выбранные данные удалены; внешние backups сохранены.",
          ),
        );
    },
  );
  confirm.disabled = true;
  phrase.addEventListener("input", () => {
    confirm.disabled = phrase.value !== "DELETE HISTORY";
  });
  box.append(confirm);
}

async function removeCatalogBinding(pid, cid) {
  const plan = await api(`/projects/${pid}/catalogs/${cid}/binding-preview`);
  details(
    t("Remove active binding · history retained", "Удалить active binding · история сохранится"),
    plan,
  );
  $("#detail-content").append(
    action(
      t("Accept preview and remove binding only", "Принять preview и удалить только binding"),
      async () => {
        await post(
          `/projects/${pid}/catalogs/${cid}/binding`,
          { remove: true, expected_preview_sha256: plan.sha256 },
          "PATCH",
        );
        $("#details").close();
        if (projectId === pid) await render();
      },
    ),
  );
}

async function deleteUserCatalogueVersion(owner, revision) {
  const plan = await api(`/user-catalogues/${owner}/versions/${revision}/delete-preview`);
  details(
    t("Remove installed user catalog version", "Удаление установленной user catalogue version"),
    plan,
  );
  if (plan.can_delete)
    $("#detail-content").append(
      action(
        t("Remove this version from instance registry", "Удалить эту version из instance registry"),
        async () => {
          await post(`/user-catalogues/${owner}/versions/${revision}/delete`, {
            confirmed: true,
            expected_sha256: plan.sha256,
          });
          $("#details").close();
          await render();
        },
      ),
    );
}

async function renderPendingPurges(content, isCurrent) {
  const jobs = await api("/purge/jobs");
  if (!isCurrent()) return;
  if (projectId)
    content.append(
      element("h3", t("Permanently delete current product", "Полное удаление текущего продукта")),
      element(
        "p",
        t(
          "A separate operation with counts, reference checks, and confirmation. Canceling a run does not delete its history.",
          "Отдельная операция с counts, проверкой ссылок и подтверждением. Отмена run не удаляет его историю.",
        ),
        "notice",
      ),
      action(t("Preview product deletion", "Предварительный просмотр удаления продукта"), () =>
        purgeDialog(projectId, "project", projectId),
      ),
    );
  if (jobs.length)
    content.append(element("h3", t("Unfinished file cleanup", "Незавершённая очистка файлов")));
  for (const job of jobs)
    content.append(
      element("p", `${job.project_id} · ${job.kind} · ${job.error_code}`),
      action(
        t(
          () => `Retry scoped cleanup ${job.id}`,
          () => `Повторить scoped cleanup ${job.id}`,
        ),
        async () => {
          const result = await post(`/projects/${job.project_id}/purge/jobs/${job.id}/retry`);
          if (result.status !== "complete")
            details(t("Cleanup still requires a retry", "Очистка всё ещё требует retry"), result);
          else await render();
        },
      ),
    );
  const visibility = await api("/builtin/visibility");
  if (!isCurrent()) return;
  content.append(
    element("h3", t("Read-only built-in bundle", "Read-only встроенный bundle")),
    element(
      "p",
      t(
        "Built-in bytes are part of the image and are not physically deleted by this button. Hiding the card does not change bindings or history.",
        "Встроенные bytes находятся в образе и физически не удаляются кнопкой. Скрытие карточки не меняет bindings и историю.",
      ),
    ),
    action(
      visibility.hidden
        ? t("Show built-in bundle", "Показать встроенный bundle")
        : t("Hide built-in bundle card", "Скрыть карточку встроенного bundle"),
      async () => {
        await post("/builtin/visibility", { hidden: !visibility.hidden }, "PUT");
        await render();
      },
    ),
  );
}

export { purgeDialog, removeCatalogBinding, deleteUserCatalogueVersion, renderPendingPurges };
