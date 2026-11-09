import { analysisMode, documentReviewRequest, scopePage, nonScopeQuestions } from "./review-mode.js";
import { t } from "./i18n.js";
import { connectionField, connectionSelect } from "./connections.js";
import {
  disclosure,
  $,
  projectId,
  setView,
  element,
  setStatus,
  api,
  post,
  action,
  showError,
  details,
  render,
} from "./app.js";
import { purgeDialog } from "./lifecycle.js";

const selectedRuns = new Map();
/** @type {Map<string, Partial<import('./api.generated').APIRunInput>>} */
const analysisPresets = new Map();
/** @type {EventSource | null} */
let runEventSource = null;
function closeRunEvents() {
  if (runEventSource) runEventSource.close();
  runEventSource = null;
}
const referenceKey = (ref) => (ref ? `${ref.id}@${ref.revision}` : "");
function selectedReference(records, key) {
  const value = records.find((record) => referenceKey(record) === key);
  if (!value)
    throw new Error(
      t(
        "Exact configuration revision unavailable; select it explicitly.",
        "Точная configuration revision недоступна; выберите её явно.",
      ),
    );
  return { id: value.id, revision: value.revision };
}
async function withPinnedReferences(pid, kind, records, refs) {
  const result = [...records];
  for (const ref of refs.filter(Boolean)) {
    if (!result.some((record) => referenceKey(record) === referenceKey(ref)))
      result.push(
        await api(`/projects/${pid}/registry/${kind}/${ref.id}/revisions/${ref.revision}`),
      );
  }
  return result;
}

function checkedList(parent, title, items, selected, label) {
  const fieldset = element("fieldset");
  fieldset.append(element("legend", title));
  const values = [];
  for (const item of items) {
    const input = connectionField(fieldset, label(item), selected.includes(item.id), "checkbox");
    values.push({ id: item.id, input });
  }
  if (!items.length)
    fieldset.append(element("p", t("No matching objects.", "Нет подходящих объектов."), "muted"));
  parent.append(fieldset);
  if (!items.length) fieldset.hidden = true;
  return () => values.filter((value) => value.input.checked).map((value) => value.id);
}

async function renderNewRun(pid, content, isCurrent) {
  const preset = analysisPresets.get(pid) || {};
  const mode = analysisMode(preset);
  const modePanel = disclosure(t("Analysis mode", "Режим проверки"));
  const modeSelect = connectionSelect(modePanel, t("Choose a mode", "Выберите режим"), mode, [
    ["review_documents", t("Check documents against requirements", "Проверить документы по требованиям")],
    ["enrich_changes", t("Advanced: compare and enrich changes", "Дополнительно: сравнить и обогатить изменения")],
  ]);
  modeSelect.addEventListener("change", async () => {
    if (!window.confirm(t("Switch mode? Unsaved choices on this screen will be reset.", "Сменить режим? Несохранённый выбор на этом экране будет сброшен."))) {
      modeSelect.value = mode;
      return;
    }
    analysisPresets.set(pid, { mode: modeSelect.value });
    await render();
  });
  content.append(modePanel);
  if (mode === "enrich_changes") return renderComparisonRun(pid, content, isCurrent);
  return renderDocumentReview(pid, content, isCurrent, preset);
}

async function renderDocumentReview(pid, content, isCurrent, preset) {
  if (preset.pipeline_ref || preset.skill_refs?.length || Object.keys(preset.prompt_refs || {}).length || Object.keys(preset.stage_models || {}).length || preset.source_refs?.length) {
    content.append(element("p", t("This saved configuration contains custom settings unsupported by document review. Switch to advanced comparison, or explicitly start with the standard document-review settings.", "Сохранённая конфигурация содержит настройки, не поддерживаемые проверкой документов. Переключитесь на сравнение или явно начните со стандартных настроек проверки."), "notice"), action(t("Use standard document-review settings", "Использовать стандартные настройки проверки"), async () => { analysisPresets.set(pid, { mode: "review_documents", review_document_ids: preset.review_document_ids, catalog_ids: preset.catalog_ids }); await render(); }));
    return;
  }
  const [documents, catalogs, connections] = await Promise.all([
    api(`/projects/${pid}/documents`), api(`/projects/${pid}/catalogs`), api(`/projects/${pid}/connections`),
  ]);
  if (!isCurrent()) return;
  content.append(element("h2", t("Check your documents", "Проверить документы")), element("p", t(
    "Select all files describing your specification and the requirements to check. No baseline or change document is needed. You get a separate report; original files are not changed.",
    "Выберите все файлы спецификации и требования для проверки. Исходная документация и отдельное описание изменений не нужны. Вы получите отдельный отчёт; исходные файлы не меняются.",
  )), element("p", t("Unknown or incomplete results are not a pass. This checks documentation, not the running product. Review conclusions and quotations manually.", "Неизвестный или неполный результат не означает успешную проверку. Проверяется документация, а не работающий продукт. Выводы и цитаты нужно проверить вручную."), "notice"));
  const form = element("form", "", "stack");
  form.append(action(t("Add specification files", "Добавить файлы спецификации"), () => { setView("documents"); return render(); }));
  const selectedDocuments = checkedList(form, t("1. Documents to check together", "1. Документы для совместной проверки"), documents,
    preset.review_document_ids || documents.filter((doc) => doc.active !== false).map((doc) => doc.id), (doc) => `${doc.name} · ${doc.sha256.slice(0, 12)}`);
  if (!documents.length) form.append(element("p", t("Upload one or more Markdown files first.", "Сначала загрузите один или несколько Markdown-файлов."), "empty"));
  const enabled = catalogs.filter((catalog) => catalog.enabled);
  form.append(action(t("Add requirements", "Добавить требования"), () => { setView("catalogs"); return render(); }));
  const selectedCatalogs = checkedList(form, t("2. Requirements", "2. Требования"), enabled,
    preset.catalog_ids || enabled.map((catalog) => catalog.id), (catalog) => `${catalog.catalog_id} · ${catalog.version} · ${catalog.records}`);
  if (!enabled.length) form.append(element("p", t("Add and enable at least one requirements catalog.", "Добавьте и подключите хотя бы один каталог требований."), "empty"));
  const llms = connections.filter((record) => record.content.kind === "llm" && record.content.enabled !== false);
  const model = connectionSelect(form, t("3. Model connection", "3. Подключение модели"), referenceKey(preset.connection || llms[0]), [
    ["", t("Select a tested connection", "Выберите проверенное подключение")],
    ...llms.map((record) => [referenceKey(record), `${record.content.name} · r${record.revision}`]),
  ]);
  form.append(action(t("Configure model", "Настроить модель"), () => { setView("connections"); return render(); }));
  let factOverrides = (preset.fact_overrides || []).map((item) => ({ ...item }));
  const confirmedFacts = element("section", "", "panel stack");
  const drawConfirmedFacts = () => {
    confirmedFacts.replaceChildren();
    confirmedFacts.hidden = !factOverrides.length;
    if (!factOverrides.length) return;
    confirmedFacts.append(element("h3", t("Your explicit scope answers", "Ваши явные ответы о применимости")), element("p", t("These answers affect which requirements are checked. Recheck them if you change the selected documents or requirements. Preparing this form has not started a model call.", "Эти ответы влияют на выбор проверяемых требований. Перепроверьте их, если меняете документы или требования. Подготовка этой формы не запускает модель."), "notice"));
    for (const item of factOverrides) {
      const card = element("div", "", "stack");
      const label = item.value === null ? t("Unknown", "Неизвестно") : item.value ? t("Yes", "Да") : t("No", "Нет");
      card.append(element("p", `${item.requirement_id} · ${item.predicate_id}: ${label}`), element("p", item.reason), action(t("Remove this answer", "Убрать этот ответ"), () => {
        factOverrides = factOverrides.filter((current) => current !== item);
        invalidate(); drawConfirmedFacts();
      }));
      confirmedFacts.append(card);
    }
    confirmedFacts.append(action(t("Clear all answers", "Очистить все ответы"), () => { factOverrides = []; invalidate(); drawConfirmedFacts(); }));
  };
  drawConfirmedFacts(); form.append(confirmedFacts);
  const advanced = disclosure(t("Optional: limits and step-by-step approval", "Необязательно: лимиты и пошаговое подтверждение"));
  const requests = connectionField(advanced, t("Maximum model requests", "Максимум запросов модели"), preset.limits?.max_requests || 200, "number"); requests.min = "1"; requests.max = "10000";
  const stageRequests = connectionField(advanced, t("Maximum requests per stage", "Максимум запросов на этап"), preset.limits?.stage_max_requests || 100, "number"); stageRequests.min = "1"; stageRequests.max = "10000";
  const tokens = connectionField(advanced, t("Token budget", "Лимит токенов"), preset.limits?.max_tokens || 256000, "number"); tokens.min = "1";
  const seconds = connectionField(advanced, t("Time limit, seconds", "Лимит времени, секунд"), preset.limits?.wall_seconds || 1800, "number"); seconds.min = "1";
  const batchSize = connectionField(advanced, t("Requirements per batch", "Требований в пакете"), preset.limits?.batch_size || 4, "number"); batchSize.min = "1"; batchSize.max = "20";
  const stepMode = connectionField(advanced, t("Approve each external call", "Подтверждать каждый внешний вызов"), preset.step_mode || false, "checkbox");
  advanced.append(element("p", t("Custom prompts, skills, and saved pipelines are not supported in document review yet. Use the advanced comparison mode for those configurations.", "Собственные промпты, навыки и сохранённые сценарии пока не поддерживаются в проверке документов. Для этих настроек используйте дополнительный режим сравнения."), "muted"));
  form.append(advanced);
  const submit = element("button", t("Check readiness and review data access", "Проверить готовность и доступ к данным")); submit.type = "submit";
  const preview = element("section", "", "panel stack"); preview.hidden = true;
  let generation = 0;
  let startButton = null;
  const invalidate = () => { generation += 1; if (startButton) startButton.disabled = true; preview.hidden = true; };
  form.addEventListener("input", invalidate);
  form.addEventListener("change", invalidate);
  form.append(submit); content.append(form, preview);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const current = ++generation;
    submit.disabled = true;
    if (startButton) startButton.disabled = true;
    preview.hidden = true;
    try {
      const request = documentReviewRequest(selectedDocuments(), selectedCatalogs(), llms.find((record) => referenceKey(record) === model.value), {
        max_requests: Number(requests.value), stage_max_requests: Number(stageRequests.value), max_tokens: Number(tokens.value), wall_seconds: Number(seconds.value), batch_size: Number(batchSize.value),
      }, stepMode.checked, factOverrides);
      if (!request) throw new Error(t("Select at least one document, a requirements catalog, and a model connection.", "Выберите хотя бы один документ, каталог требований и подключение модели."));
      const plan = await post(`/projects/${pid}/runs/preflight`, request);
      if (!isCurrent() || current !== generation) return;
      preview.hidden = false;
      preview.replaceChildren(element("h3", t("Review before starting", "Проверьте перед запуском")), element("p", t(
        `${request.review_document_ids.length} documents · ${plan.active_requirements} requirements. The model receives the selected document text and requirements.`,
        `Документов: ${request.review_document_ids.length} · требований: ${plan.active_requirements}. Модель получит выбранные документы и требования.`,
      )), element("pre", JSON.stringify(plan.destinations, null, 2)), element("p", plan.future_payloads, "notice"), element("p", t("Only document assessment runs. No change inference, rewritten specification, or patch acceptance is required to get the report.", "Выполняется только оценка документов. Для отчёта не нужны поиск изменений, переписанная спецификация или принятие правок.")));
      preview.append(disclosure(t("Inspect exact first request", "Посмотреть точный первый запрос"), element("pre", JSON.stringify(plan.first_payload_preview, null, 2))));
      startButton = action(t("Start document review", "Запустить проверку документов"), async () => {
        if (!isCurrent() || current !== generation) return;
        const run = await post(`/projects/${pid}/runs`, { request, expected_plan_sha256: plan.plan_sha256, idempotency_key: crypto.randomUUID() });
        selectedRuns.set(pid, run.id); analysisPresets.delete(pid);
        if (isCurrent()) { setView("run"); await render(); }
      });
      preview.append(startButton);
    } catch (error) { if (isCurrent() && current === generation) showError(error); }
    finally { if (isCurrent()) submit.disabled = false; }
  });
}

async function renderComparisonRun(pid, content, isCurrent) {
  let [documents, catalogs, connections, prompts, skills, sources, pipelines] = await Promise.all([
    api(`/projects/${pid}/documents`),
    api(`/projects/${pid}/catalogs`),
    api(`/projects/${pid}/connections`),
    api(`/projects/${pid}/configuration/prompt`),
    api(`/projects/${pid}/configuration/skill`),
    api(`/projects/${pid}/sources`),
    api(`/projects/${pid}/configuration/pipeline`),
  ]);
  if (!isCurrent()) return;
  const preset = analysisPresets.get(pid) || {};
  let factOverrides = preset.fact_overrides || [];
  let operationOverrides = preset.operation_overrides || [];
  let adoptionOverrides = preset.adoption_overrides || [];
  [connections, prompts, skills, sources, pipelines] = await Promise.all([
    withPinnedReferences(pid, "connection", connections, [
      preset.connection,
      ...Object.values(preset.stage_models || {}),
    ]),
    withPinnedReferences(pid, "prompt", prompts, Object.values(preset.prompt_refs || {})),
    withPinnedReferences(pid, "skill", skills, preset.skill_refs || []),
    withPinnedReferences(pid, "source", sources, preset.source_refs || []),
    withPinnedReferences(pid, "pipeline", pipelines, [preset.pipeline_ref]),
  ]);
  if (!isCurrent()) return;
  content.append(
    element("h2", t("Compare and enrich changes", "Сравнить и обогатить изменения")),
  );
  content.append(
    element(
      "p",
      t(
        "Models can produce incorrect assessments or source conflicts. Review conclusions and quotations manually; control implementation remains not_verified.",
        "Модель может ошибаться в оценках и конфликтах источников. Проверяйте выводы и цитаты вручную; реализация контролей остаётся not_verified.",
      ),
      "notice",
    ),
  );
  let pinnedPipeline = preset.pipeline_ref || null;
  const pipelineOptions = disclosure(t("Use saved analysis settings", "Использовать сохранённые настройки анализа"));
  content.append(pipelineOptions);
  const pipelinePicker = connectionSelect(
    pipelineOptions,
    t(
      "Named pipeline (bound to an exact revision)",
      "Сохранённые настройки (точная версия)",
    ),
    referenceKey(pinnedPipeline),
    [
      ["", t("No named pipeline", "Без сохранённых настроек")],
      ...pipelines.map((record) => [
        referenceKey(record),
        `${record.content.name} · r${record.revision}`,
      ]),
    ],
  );
  pipelinePicker.addEventListener("change", async () => {
    const selected = pipelines.find((record) => referenceKey(record) === pipelinePicker.value);
    analysisPresets.set(pid, {
      ...preset,
      ...(selected ? selected.content : {}),
      pipeline_ref: selected ? { id: selected.id, revision: selected.revision } : null,
    });
    await render();
  });

  const editor = element("section", "", "panel stack");
  editor.append(element("h3", t("Specification text", "Текст спецификации")));
  const deltaText = connectionField(
    editor,
    t("Change text (up to 100 KiB)", "Текст изменения (до 100 KiB)"),
    "",
    "textarea",
  );
  deltaText.rows = 8;
  const deltaName = connectionField(
    editor,
    t("New immutable snapshot name", "Имя нового снимка"),
    "delta.md",
  );
  const form = element("form", "", "stack");
  const deltas = documents.filter((doc) => doc.role === "delta");
  if (!deltas.length) form.append(action(t("Add specification files", "Добавить файлы спецификации"), () => { setView("documents"); return render(); }));
  const delta = connectionSelect(
    form,
    t("Specification to check", "Спецификация для проверки"),
    preset.delta_document_id || deltas.at(-1)?.id || "",
    [
      ["", t("Select an uploaded specification", "Выберите загруженную спецификацию")],
      ...deltas.map((doc) => [doc.id, `${doc.name} · ${doc.sha256.slice(0, 12)}`]),
    ],
  );
  editor.append(
    action(t("Save text as a separate delta", "Сохранить текст спецификации"), async () => {
      const result = await api(
        `/projects/${pid}/documents?filename=${encodeURIComponent(deltaName.value)}&role=delta`,
        {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: deltaText.value,
        },
      );
      delta.add(new Option(`${deltaName.value} · ${result.sha256.slice(0, 12)}`, result.id));
      delta.value = result.id;
      form.dispatchEvent(new Event("input"));
      setStatus(
        t(
          "Delta snapshot saved; original files were not overwritten.",
          "Снимок спецификации сохранён; исходные файлы не перезаписаны.",
        ),
      );
    }),
  );
  content.append(disclosure(t("Or paste a specification instead of uploading files", "Или вставьте спецификацию вместо загрузки файлов"), editor));
  const baseline = documents.filter((doc) => doc.role === "master_spec");
  const context = documents.filter((doc) => doc.role === "context");
  const selectedBaseline = checkedList(
    form,
    t(
      "Existing documentation (required for comparison)",
      "Исходная документация (обязательна для сравнения)",
    ),
    baseline,
    preset.baseline_document_ids || baseline.filter((doc) => doc.active).map((doc) => doc.id),
    (doc) => `${doc.name} · ${doc.sha256.slice(0, 12)}`,
  );
  const selectedContext = checkedList(
    form,
    t("Additional context", "Дополнительный контекст"),
    context,
    preset.context_document_ids || [],
    (doc) => doc.name,
  );
  const selectedSources = checkedList(
    form,
    t(
      "Sources for this run: pinned snapshots after explicit import",
      "Источники этого run: закреплённые snapshots после явной загрузки",
    ),
    sources
      .filter((record) => record.content.enabled)
      .map((record) => ({ ...record, id: referenceKey(record) })),
    (preset.source_refs || []).map(referenceKey),
    (record) =>
      t(
        () =>
          `${record.content.name} · ${record.content.purpose} · r${record.revision} · ${record.capability_report?.result.status || "not read"}`,
        () =>
          `${record.content.name} · ${record.content.purpose} · r${record.revision} · ${record.capability_report?.result.status || "не прочитан"}`,
      ),
  );
  form.append(
    element(
      "p",
      t(
        "Sources do not refresh automatically. Selected document snapshots have their own hashes and fetched_at. A required source without a successfully read, selected snapshot blocks preflight.",
        "Источник не обновляется автоматически. Выбранные document snapshots имеют собственные hashes и fetched_at. Required source без успешно прочитанного выбранного snapshot блокирует preflight.",
      ),
      "notice",
    ),
  );

  const enabled = catalogs.filter((catalog) => catalog.enabled);
  if (!enabled.length) form.append(action(t("Add requirements before analysis", "Добавить требования перед анализом"), () => { setView("catalogs"); return render(); }));
  const selectedCatalogs = checkedList(
    form,
    t("Requirements to check", "Требования для проверки"),
    enabled,
    preset.catalog_ids || enabled.map((catalog) => catalog.id),
    (catalog) =>
      t(
        () => `${catalog.catalog_id} · ${catalog.version} · ${catalog.records} records`,
        () => `${catalog.catalog_id} · ${catalog.version} · ${catalog.records} записей`,
      ),
  );
  form.append(
    element(
      "p",
      t(
        "Selected catalogs are checked together. Review the scope before starting.",
        "Выбранные каталоги проверяются вместе. Перед запуском проверьте состав требований.",
      ),
      "notice",
    ),
  );
  const llms = connections.filter((record) => record.content.kind === "llm");
  if (!llms.length) form.append(action(t("Set up a model connection", "Настроить подключение модели"), () => { setView("connections"); return render(); }));
  const model = connectionSelect(
    form,
    t("Model connection", "Подключение модели"),
    referenceKey(preset.connection || llms[0]),
    [
      ["", t("Select a tested profile", "Выберите проверенный профиль")],
      ...llms.map((record) => [
        referenceKey(record),
        t(
          () =>
            `${record.content.name} · r${record.revision} · ${record.content.output_mode} · ${record.capability_report?.result.status || "not tested"}`,
          () =>
            `${record.content.name} · r${record.revision} · ${record.content.output_mode} · ${record.capability_report?.result.status || "не проверен"}`,
        ),
      ]),
    ],
  );
  const bindings = element("details");
  bindings.append(
    element(
      "summary",
      t(
        "Models and published prompts for individual model steps",
        "Модели и опубликованные промпты отдельных этапов",
      ),
    ),
  );
  const stageBindings = new Map();
  for (const stage of ["P2", "P3", "P5", "P7", "P8"]) {
    const panel = element("div", "", "stack panel");
    panel.append(element("h3", stage));
    const stageModel = connectionSelect(
      panel,
      t(
        () => `${stage}: model`,
        () => `${stage}: модель`,
      ),
      referenceKey(preset.stage_models?.[stage]),
      [
        ["", t("Primary LLM profile", "Основное подключение модели")],
        ...llms.map((record) => [
          referenceKey(record),
          `${record.content.name} · r${record.revision}`,
        ]),
      ],
    );
    const stagePrompt = connectionSelect(
      panel,
      t(
        () => `${stage}: prompt`,
        () => `${stage}: промпт`,
      ),
      referenceKey(preset.prompt_refs?.[stage]),
      [
        ["", t("Built-in prompt", "Встроенный промпт")],
        ...prompts
          .filter(
            (record) => record.content.stage === stage && record.content.status === "published",
          )
          .map((record) => [referenceKey(record), `${record.content.name} · r${record.revision}`]),
      ],
    );
    stageBindings.set(stage, { model: stageModel, prompt: stagePrompt });
    bindings.append(panel);
  }
  form.append(bindings);
  const selectedSkills = checkedList(
    form,
    t(
      "Skills: before → replace/core → after, order pinned by preflight",
      "Навыки: до → вместо / основной этап → после. Порядок фиксируется перед запуском.",
    ),
    skills
      .filter((record) => record.content.enabled)
      .map((record) => ({ ...record, id: referenceKey(record) })),
    (preset.skill_refs || []).map(referenceKey),
    (record) =>
      `${record.content.stage} ${record.content.hook} ${record.content.order}: ${record.content.name} · r${record.revision}`,
  );
  const requests = connectionField(
    form,
    t(
      "Maximum model requests (including repair/retry)",
      "Максимум запросов модели (включая исправления и повторы)",
    ),
    preset.limits?.max_requests || 200,
    "number",
  );
  requests.min = "1";
  requests.max = "10000";
  const stageRequests = connectionField(
    form,
    t("Maximum requests per stage", "Максимум запросов на один этап"),
    preset.limits?.stage_max_requests || 100,
    "number",
  );
  stageRequests.min = "1";
  stageRequests.max = "10000";
  const tokens = connectionField(
    form,
    t("Conservative token budget", "Лимит токенов"),
    preset.limits?.max_tokens || 256000,
    "number",
  );
  const seconds = connectionField(
    form,
    t("Wall-clock budget, seconds", "Лимит времени, секунд"),
    preset.limits?.wall_seconds || 1800,
    "number",
  );
  const batchSize = connectionField(
    form,
    t("Requirements batch size", "Размер пакета требований"),
    preset.limits?.batch_size || 4,
    "number",
  );
  batchSize.min = "1";
  batchSize.max = "20";
  const stepMode = connectionField(
    form,
    t(
      "Step-by-step mode: stages and exact payload of every external call",
      "Пошаговый режим: этапы и точные данные каждого внешнего вызова",
    ),
    preset.step_mode || false,
    "checkbox",
  );
  form.append(
    element(
      "p",
      t(
        "Before a response, tokens are conservatively reserved using UTF-8 bytes + maximum output. Actual provider usage is stored in artifacts. Budget exhaustion makes the run partial; every ID remains in coverage.",
        "Токены до ответа резервируются консервативно по UTF-8 bytes + max output. Фактический provider usage хранится в artifacts. При исчерпании бюджета run будет partial; каждый ID останется в coverage.",
      ),
      "muted",
    ),
  );
  const overridesInfo = element(
    "p",
    t(
      () =>
        `Explicit answers: ${factOverrides.length}; operations: ${operationOverrides.length}; adoption/waiver: ${adoptionOverrides.length}.`,
      () =>
        `Явные ответы: ${factOverrides.length}; операции: ${operationOverrides.length}; adoption/waiver: ${adoptionOverrides.length}.`,
    ),
    "notice",
  );
  form.append(
    overridesInfo,
    action(
      t(
        "Inspect explicit overrides for this new run",
        "Посмотреть ручные переопределения этого запуска",
      ),
      () =>
        details(t("Overrides do not change frozen history", "Overrides не меняют frozen историю"), {
          fact_overrides: factOverrides,
          operation_overrides: operationOverrides,
          adoption_overrides: adoptionOverrides,
        }),
    ),
    action(
      t("Clear all overrides before analysis", "Очистить ручные переопределения перед анализом"),
      () => {
        factOverrides = [];
        operationOverrides = [];
        adoptionOverrides = [];
        overridesInfo.textContent = t(
          "Overrides cleared; a new preflight is required.",
          "Overrides очищены; требуется новый preflight.",
        );
        if (startButton) startButton.disabled = true;
      },
    ),
  );
  const advanced = disclosure(t("Advanced: sources, skills, budgets, and overrides", "Дополнительно: источники, навыки, лимиты и переопределения"));
  const children = Array.from(form.children);
  const modelIndex = children.indexOf(model);
  for (const child of children.slice(modelIndex + 1)) advanced.append(child);
  // Source selection is optional; keep both its notice and checkboxes together.
  const sourceFieldset = Array.from(form.querySelectorAll("fieldset"))[2];
  if (sourceFieldset) {
    const notice = sourceFieldset.nextElementSibling;
    advanced.prepend(sourceFieldset);
    if (notice?.classList.contains("notice")) sourceFieldset.after(notice);
  }
  form.append(advanced);
  const submit = element(
    "button",
    t("Check readiness and review data access", "Проверить готовность и доступ к данным"),
  );
  submit.type = "submit";
  const preview = element("section", "", "panel");
  form.append(submit);
  content.append(form, preview);
  /** @type {HTMLButtonElement | null} */
  let startButton = null;
  let inputScope = JSON.stringify([
    delta.value,
    selectedBaseline(),
    selectedContext(),
    selectedCatalogs(),
  ]);
  form.addEventListener("input", () => {
    pinnedPipeline = null;
    const nextScope = JSON.stringify([
      delta.value,
      selectedBaseline(),
      selectedContext(),
      selectedCatalogs(),
    ]);
    if (nextScope !== inputScope) {
      factOverrides = [];
      operationOverrides = [];
      adoptionOverrides = [];
      overridesInfo.textContent = t(
        "Documents/catalogs changed: previous answers and overrides were cleared. They can be set for a new run after review.",
        "Набор документов/каталогов изменился: прежние ответы и overrides очищены. Их можно задать для нового run после review.",
      );
      inputScope = nextScope;
    }
    if (startButton) {
      startButton.disabled = true;
      setStatus(
        t(
          "Parameters changed — prepare preflight again.",
          "Параметры изменились. Повторите проверку готовности.",
        ),
      );
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const connection = llms.find((item) => referenceKey(item) === model.value);
      if (!connection || !delta.value || !selectedBaseline().length)
        throw new Error(t("Comparison requires a baseline, a change document, and a model. For standalone specification checks, switch to document review.", "Для сравнения нужны исходная документация, документ изменения и модель. Для самостоятельной проверки спецификации переключитесь в режим проверки документов."));
      /** @type {import('./api.generated').APIRunInput} */
      const request = {
        mode: "enrich_changes",
        delta_document_id: delta.value,
        baseline_document_ids: selectedBaseline(),
        context_document_ids: selectedContext(),
        catalog_ids: selectedCatalogs(),
        connection: { id: connection.id, revision: connection.revision },
        fact_overrides: factOverrides,
        adoption_overrides: adoptionOverrides,
        operation_overrides: operationOverrides,
        pipeline_ref: pinnedPipeline,
        stage_models: Object.fromEntries(
          Array.from(stageBindings)
            .filter(([, binding]) => binding.model.value)
            .map(([stage, binding]) => [stage, selectedReference(llms, binding.model.value)]),
        ),
        prompt_refs: Object.fromEntries(
          Array.from(stageBindings)
            .filter(([, binding]) => binding.prompt.value)
            .map(([stage, binding]) => [stage, selectedReference(prompts, binding.prompt.value)]),
        ),
        skill_refs: selectedSkills().map((key) => selectedReference(skills, key)),
        source_refs: selectedSources().map((key) => selectedReference(sources, key)),
        step_mode: stepMode.checked,
        limits: {
          max_requests: Number(requests.value),
          stage_max_requests: Number(stageRequests.value),
          max_tokens: Number(tokens.value),
          wall_seconds: Number(seconds.value),
          batch_size: Number(batchSize.value),
        },
      };
      const plan = await post(`/projects/${pid}/runs/preflight`, request);
      preview.replaceChildren(
        element("h3", t("Data transfer plan", "План передачи данных")),
        element(
          "p",
          t(
            () => `${plan.active_requirements} active requirements · ${plan.execution_mode}`,
            () => `${plan.active_requirements} активных требований · ${plan.execution_mode}`,
          ),
        ),
        element("pre", JSON.stringify(plan.destinations, null, 2)),
        element("p", plan.future_payloads, "notice"),
      );
      preview.append(
        element(
          "h3",
          t("Pinned model step / hook order", "Закреплённый порядок model-step / hooks"),
        ),
        element("pre", JSON.stringify(plan.hook_order, null, 2)),
        element(
          "p",
          t(
            "P5 assesses each state in one pass. In P8, final document assessment is separate from edit critique: the pinned P5 prompt compares baseline and candidate through the P8 connection, without previous edits or generator conclusions. Every request counts toward limits; step-by-step mode requires separate approval. Rechecking does not guarantee model accuracy.",
            "P5 оценивает каждое состояние одним проходом. В P8 оценка итогового документа отделена от критики правок: закреплённый промпт P5 сравнивает baseline и candidate через подключение P8, без текста прежних правок и выводов генератора. Каждый запрос учитывается в лимитах; в пошаговом режиме требуется отдельное согласие. Перепроверка не гарантирует точность модели.",
          ),
        ),
        element(
          "h3",
          t(
            "Pinned documentary assessment of the P8 candidate",
            "Закреплённая документальная оценка candidate в P8",
          ),
        ),
        element("pre", JSON.stringify(plan.candidate_reassessment_prompt, null, 2)),
        element(
          "h3",
          t(
            "Pinned P3 architectural fact recheck",
            "Закреплённая перепроверка архитектурных фактов P3",
          ),
        ),
        element(
          "p",
          t(
            "Negative facts and conflicting uncertainty in P3 are rechecked separately from control assessment, with the same approvals and limits.",
            "Отрицательные факты и конфликтная неопределённость P3 перепроверяются отдельно от оценки контроля, с теми же согласиями и лимитами.",
          ),
        ),
        element("pre", JSON.stringify(plan.architectural_review_prompt, null, 2)),
      );
      preview.append(
        action(
          t(
            "Save these bindings as a named pipeline",
            "Сохранить эти bindings как именованный pipeline",
          ),
          () => {
            const box = $("#detail-content");
            box.replaceChildren(
              element(
                "h2",
                t(
                  "Save pipeline without documents or evidence",
                  "Сохранить pipeline без документов и evidence",
                ),
              ),
            );
            const name = connectionField(
              box,
              t("Pipeline name", "Название pipeline"),
              t("Custom pipeline", "Собственный pipeline"),
            );
            const version = connectionField(box, t("Pipeline version", "Версия pipeline"), "1.0.0");
            box.append(
              action(
                t("Save new pipeline configuration", "Сохранить новую pipeline configuration"),
                async () => {
                  const content = {
                    name: name.value,
                    version: version.value,
                    connection: request.connection,
                    stage_models: request.stage_models,
                    prompt_refs: request.prompt_refs,
                    skill_refs: request.skill_refs,
                    source_refs: request.source_refs,
                    limits: request.limits,
                    step_mode: request.step_mode,
                  };
                  await post(`/projects/${pid}/configuration/pipeline`, {
                    content,
                    expected_revision: null,
                  });
                  $("#details").close();
                  setStatus(
                    t(
                      "Pipeline saved; current plan unchanged.",
                      "Pipeline сохранён; текущий plan не изменён.",
                    ),
                  );
                },
              ),
            );
            if (!$("#details").open) $("#details").showModal();
          },
        ),
      );

      const payload = element("details");
      payload.append(
        element("summary", t("First actual payload preview", "Первый фактический payload preview")),
        element("pre", JSON.stringify(plan.first_payload_preview, null, 2)),
      );
      preview.append(payload);
      startButton = action(
        t("Start with the displayed plan", "Запустить по показанному плану"),
        async () => {
          const run = await post(`/projects/${pid}/runs`, {
            request,
            expected_plan_sha256: plan.plan_sha256,
            idempotency_key: crypto.randomUUID(),
          });
          selectedRuns.set(pid, run.id);
          analysisPresets.delete(pid);
          if (projectId === pid) {
            setView("run");
            await render();
          }
        },
      );
      preview.append(startButton);
    } catch (error) {
      showError(error);
    } finally {
      submit.disabled = false;
    }
  });
}

async function renderHistory(pid, content, isCurrent) {
  const runs = await api(`/projects/${pid}/runs`);
  if (!isCurrent()) return;
  content.append(element("h2", t("Run history", "История запусков")));
  if (runs.length > 1) {
    const compare = disclosure(t("Optional: compare previous runs", "Необязательно: сравнить прошлые запуски"));
    compare.append(
      element(
        "h3",
        t("Compare pinned context and results", "Сравнить pinned контекст и результаты"),
      ),
    );
    const options = runs.map((run) => [
      run.id,
      `${run.created_at} · ${run.id.slice(0, 8)} · ${run.status}`,
    ]);
    const left = connectionSelect(
      compare,
      t("First run to compare", "Первый run для сравнения"),
      runs[1].id,
      options,
    );
    const right = connectionSelect(
      compare,
      t("Second run to compare", "Второй run для сравнения"),
      runs[0].id,
      options,
    );
    compare.append(
      action(t("Compare runs", "Сравнить run"), async () =>
        details(
          t(
            "Run comparison · context checked separately",
            "Сравнение запусков · контекст проверяется отдельно",
          ),
          await api(`/projects/${pid}/runs/${left.value}/compare/${right.value}`),
        ),
      ),
    );
    content.append(compare);
  }
  if (!runs.length) content.append(element("p", t("No runs yet.", "Запусков пока нет."), "empty"));
  for (const run of runs) {
    const panel = element("section", "", "panel");
    panel.append(
      element("h3", run.id),
      element(
        "p",
        t(
          () => `${run.created_at} UTC · ${run.status} · ${run.error_code || "no lifecycle error"}`,
          () =>
            `${run.created_at} UTC · ${run.status} · ${run.error_code || "без ошибки lifecycle"}`,
        ),
      ),
      action(t("Open results", "Открыть результаты"), async () => {
        selectedRuns.set(pid, run.id);
        setView("run");
        await render();
      }),
    );
    panel.append(
      action(
        t("Permanently delete this run · preview", "Полное удаление этого run · preview"),
        () => purgeDialog(pid, "run", run.id),
      ),
    );
    content.append(panel);
  }
}

async function renderRun(pid, rid, content, isCurrent) {
  if (runEventSource) {
    runEventSource.close();
    runEventSource = null;
  }
  if (!rid) {
    setView("history");
    return render();
  }
  const prefix = `/projects/${pid}/runs/${rid}`;
  let run = await api(prefix);
  if (!isCurrent()) return;
  content.append(element("h2", t("Run review", "Результаты анализа")), element("p", rid, "muted"));
  const state = element("p", "", "notice");
  state.setAttribute("role", "status");
  const stages = element("div", "", "stage-grid");
  const result = element("section");
  const continuation = element("section");
  let loaded = false;
  let continuationKey = "";
  const cancel = action(t("Cancel further calls", "Отменить дальнейшие calls"), async () => {
    await post(`${prefix}/cancel`);
    setStatus(
      t(
        "Cancellation requested. An in-flight request may still complete.",
        "Запрошена отмена. In-flight request может ещё завершиться.",
      ),
    );
  });
  content.append(
    state,
    cancel,
    continuation,
    stages,
    action(t("Versions and manifest", "Версии и manifest"), async () =>
      details("Pinned manifest", await api(`${prefix}/manifest`)),
    ),
    action(t("Artifacts and actual payloads", "Артефакты и реальные payloads"), async () =>
      artifactInspector(pid, rid),
    ),
    result,
  );
  const update = async (value) => {
    if (!isCurrent()) {
      runEventSource?.close();
      return;
    }
    if (["waiting_approval", "interrupted"].includes(value.status) && !value.checkpoint) {
      value = await api(prefix);
      if (!isCurrent()) return;
    }
    state.textContent = t(
      () => `${value.status} · implementation not verified. No security percentage is calculated.`,
      () => `${value.status} · реализация не проверялась. Процент безопасности не рассчитывается.`,
    );
    cancel.disabled = !["queued", "running", "waiting_approval"].includes(value.status);
    const nextContinuationKey = `${value.status}:${value.checkpoint?.sha256 || ""}`;
    if (nextContinuationKey !== continuationKey) {
      continuationKey = nextContinuationKey;
      continuation.replaceChildren();
      if (
        ["waiting_approval", "interrupted"].includes(value.status) &&
        value.checkpoint.pending_call
      ) {
        const pending = value.checkpoint.pending_call;
        continuation.append(
          element(
            "p",
            t(
              () =>
                `${pending.stage}: awaiting approval to send to ${pending.destination}. The new external call has not been made yet.`,
              () =>
                `${pending.stage}: ожидает подтверждения отправки в ${pending.destination}. Новый внешний вызов ещё не выполнен.`,
            ),
            "notice",
          ),
          action(
            t("Inspect exact payload before sending", "Просмотреть точный payload перед отправкой"),
            async () => {
              const preview = await api(`${prefix}/artifacts/${pending.artifact_id}`);
              if (!isCurrent()) return;
              details(
                t(
                  "Exact next-call payload · credential headers hidden",
                  "Точный payload следующего вызова · credentials headers не отображаются",
                ),
                preview,
              );
              $("#detail-content").append(
                action(
                  t("Approve sending this payload", "Подтвердить отправку этого payload"),
                  async () => {
                    const current = await api(prefix);
                    if (
                      current.checkpoint.pending_call?.artifact_id !== pending.artifact_id ||
                      current.checkpoint.pending_call?.sha256 !== pending.sha256
                    )
                      throw new Error(
                        t(
                          "Payload changed; inspect the new preview.",
                          "Payload изменился; просмотрите новый preview.",
                        ),
                      );
                    await post(`${prefix}/resume`, {
                      expected_checkpoint_sha256: current.checkpoint.sha256,
                      confirmed_call: true,
                      call_preview_id: pending.artifact_id,
                      expected_call_preview_sha256: pending.sha256,
                    });
                    $("#details").close();
                    if (isCurrent()) await render();
                  },
                ),
              );
            },
          ),
        );
      } else if (["waiting_approval", "interrupted"].includes(value.status))
        continuation.append(
          action(
            t("Reviewed — continue from checkpoint", "Просмотрено — продолжить с checkpoint"),
            async () => {
              const current = await api(prefix);
              await post(`${prefix}/resume`, {
                expected_checkpoint_sha256: current.checkpoint.sha256,
              });
              if (isCurrent()) await render();
            },
          ),
        );
    }
    stages.replaceChildren();
    for (const stage of value.stages) {
      const box = element("div", "", "panel");
      box.append(element("strong", `${stage.stage_id} · ${stage.status}`));
      if (stage.usage?.request_count_total !== undefined)
        box.append(
          element(
            "p",
            t(
              () =>
                `Total requests: ${stage.usage.request_count_total}; conservative tokens: ${stage.usage.conservative_token_count}; cost unknown.`,
              () =>
                `Requests всего: ${stage.usage.request_count_total}; conservative tokens: ${stage.usage.conservative_token_count}; стоимость неизвестна.`,
            ),
          ),
        );
      if (stage.errors.length || stage.warnings.length)
        box.append(
          element(
            "pre",
            JSON.stringify({ warnings: stage.warnings, errors: stage.errors }, null, 2),
          ),
        );
      stages.append(box);
    }
    if (cancel.disabled && !loaded) {
      loaded = true;
      run = await api(prefix);
      await runResults(pid, rid, run, result, isCurrent);
    }
  };
  await update(run);
  if (["queued", "running", "waiting_approval"].includes(run.status)) {
    runEventSource = new EventSource(`/api/v1${prefix}/events`);
    runEventSource.addEventListener("progress", (event) => {
      update(JSON.parse(event.data)).catch(showError);
    });
    runEventSource.addEventListener("error", () => {
      if (isCurrent())
        setStatus(
          t(
            "Progress stream interrupted; refresh the page to check the actual status.",
            "Поток progress прерван; обновите страницу, чтобы сверить фактический статус.",
          ),
        );
    });
  }
}

async function artifactInspector(pid, rid) {
  const items = await api(`/projects/${pid}/runs/${rid}/artifacts`);
  const target = $("#detail-content");
  target.replaceChildren(
    element(
      "h2",
      t("Artifacts (sensitive local data)", "Артефакты (чувствительные локальные данные)"),
    ),
  );
  let offset = 0;
  const list = element("div");
  target.append(list);
  const more = action(t("Next 100", "Следующие 100"), () => {
    for (const item of items.slice(offset, offset + 100))
      list.append(
        action(`${item.kind} · ${item.byte_size} bytes · ${item.sha256.slice(0, 12)}`, async () => {
          const response = await fetch(`/api/v1/projects/${pid}/runs/${rid}/artifacts/${item.id}`);
          if (!response.ok)
            throw new Error(
              t(
                "Artifact unavailable in the selected scope.",
                "Артефакт недоступен в выбранном scope.",
              ),
            );
          const text = await response.text();
          if (projectId === pid) details(item.kind, text);
        }),
      );
    offset += 100;
    more.disabled = offset >= items.length;
  });
  target.append(more);
  more.click();
  if (!$("#details").open) $("#details").showModal();
}

function assessmentLabel(value) {
  const labels = {
    satisfied_by_spec: t("Supported by documents", "Подтверждено документами"),
    contradicted_by_spec: t("Contradicted by documents", "Противоречит документам"),
    not_addressed: t("Not addressed in documents", "Не описано в документах"),
    unknown: t("Unknown: insufficient evidence", "Неизвестно: недостаточно данных"),
    not_assessed: t("Not assessed", "Не оценено"),
    applicable: t("Applicable", "Применимо"),
    not_applicable: t("Not applicable", "Неприменимо"),
  };
  return labels[value] || value || labels.unknown;
}

async function documentReviewResults(pid, rid, target, isCurrent) {
  const prefix = `/projects/${pid}/runs/${rid}`;
  let report;
  try { report = await api(`${prefix}/report`); }
  catch (error) {
    if (!isCurrent()) return;
    target.replaceChildren(element("h3", t("Document review report", "Отчёт о проверке документов")), element("p", t("A report is not available yet. A stopped or incomplete run is not a successful check. Inspect the stage messages above before continuing.", "Отчёт пока недоступен. Остановленный или неполный запуск не означает успешную проверку. Перед продолжением изучите сообщения этапов выше."), "notice"), element("p", error.message || String(error)));
    return;
  }
  if (!isCurrent()) return;
  target.replaceChildren(element("h3", t("Document review report", "Отчёт о проверке документов")), element("p", t("Documentation only; product implementation is not verified. Model conclusions and quotations require human review. Unknown does not mean passed or failed.", "Проверена только документация; реализация продукта не проверялась. Выводы модели и цитаты требуют проверки человеком. Неизвестный результат не означает ни успех, ни провал."), "notice"));
  target.append(element("p", report.incomplete
    ? t("Incomplete coverage: some assessments could not be completed. Do not treat this report as a pass.", "Проверка неполная: часть оценок не завершена. Этот отчёт нельзя считать успешной проверкой.")
    : t("Report generated. This is not a security certification or a blanket pass.", "Отчёт сформирован. Это не сертификат безопасности и не общий вердикт об успехе.")),
    element("p", t(`${report.matrix.length} requirements · ${report.unknown_requirements} unknown or not assessed · ${report.findings.length} findings`, `Требований: ${report.matrix.length} · неизвестно или не оценено: ${report.unknown_requirements} · замечаний: ${report.findings.length}`)));
  const exportKey = crypto.randomUUID();
  target.append(action(t("Download independent report (Markdown + JSON)", "Скачать отдельный отчёт (Markdown + JSON)"), async () => {
    const result = await post(`${prefix}/report/exports`, { expected_review_revision: report.review_revision, idempotency_key: exportKey });
    if (!isCurrent()) return;
    const link = element("a", t("Download report ZIP", "Скачать ZIP с отчётом"), "download-action");
    link.href = `/api/v1${prefix}/exports/${result.id}`; link.download = result.filename;
    target.append(link); link.focus();
  }), element("p", t("The report includes document quotations and source references. Review it before sharing. Original documents are unchanged; no patches need to be accepted.", "Отчёт содержит цитаты документов и ссылки на источники. Проверьте его перед передачей другим. Исходные документы не меняются; принимать правки не требуется."), "muted"));
  const filter = connectionSelect(target, t("Show requirements", "Показать требования"), "all", [
    ["all", t("All assessments", "Все оценки")], ["unknown", t("Unknown or not assessed", "Неизвестно или не оценено")], ["not_addressed", t("Not addressed", "Не описано")], ["contradicted_by_spec", t("Contradictions", "Противоречия")], ["satisfied_by_spec", t("Supported by documents", "Подтверждено документами")],
  ]);
  const matrix = element("section", "", "stack"); target.append(matrix);
  const draw = () => {
    matrix.replaceChildren();
    for (const row of report.matrix.filter((row) => filter.value === "all" || (filter.value === "unknown" ? ["unknown", "not_assessed"].includes(row.assessment?.assessment || "unknown") : row.assessment?.assessment === filter.value))) {
      const value = row.assessment || {};
      matrix.append(disclosure(`${row.requirement_id} · ${assessmentLabel(value.assessment)}`, element("p", t(`Applicability: ${assessmentLabel(value.applicability)}`, `Применимость: ${assessmentLabel(value.applicability)}`)), element("p", value.reason || ""), ...(Object.values(value.clause_assessments || {}).flatMap((clause) => [element("p", clause.explanation || clause.reason || ""), ...(clause.evidence_refs || []).filter((ref) => ref.quote).map((ref) => element("p", ref.quote))])), element("pre", JSON.stringify(value, null, 2))));
    }
    if (!matrix.children.length) matrix.append(element("p", t("No matching requirements.", "Нет подходящих требований.")));
  };
  filter.addEventListener("change", draw); draw();
  target.append(element("h3", t("Findings", "Замечания")));
  for (const finding of report.findings) {
    const card = element("section", "", "panel stack");
    card.append(element("h4", finding.requirement_ref?.id || finding.requirement_id || finding.id), element("p", assessmentLabel(finding.assessment)), element("p", finding.explanation || finding.reason || ""));
    if (finding.suggested_resolution) card.append(element("p", finding.suggested_resolution));
    for (const ref of finding.evidence_refs || []) {
      if (ref.quote) card.append(element("p", ref.quote));
      if (ref.document_id) card.append(element("p", t(`Source: ${ref.document_id}`, `Источник: ${ref.document_id}`), "muted"));
    }
    card.append(disclosure(t("Evidence and details", "Доказательства и подробности"), element("pre", JSON.stringify(finding, null, 2)))); target.append(card);
  }
  if (!report.findings.length) target.append(element("p", t("No findings listed. Check unknown assessments and incomplete coverage before drawing conclusions.", "Замечаний в списке нет. Перед выводами проверьте неизвестные оценки и полноту проверки.")));
  const otherQuestions = nonScopeQuestions(report);
  if (otherQuestions.length) target.append(disclosure(t(`Other questions to resolve (${otherQuestions.length})`, `Другие вопросы для уточнения (${otherQuestions.length})`), ...otherQuestions.map((question) => element("p", question))));
  if (report.scope_confirmations?.length) {
    const panel = element("section", "", "panel stack");
    panel.append(element("h3", t("Confirm applicability before excluding requirements", "Уточните применимость перед исключением требований")), element("p", t("The model proposed excluding requirements or individual clauses. This is not confirmed and is not a pass. Answer only what you know; provide the basis for each answer. Your answers prepare a new review, without changing this report or starting the model.", "Модель предложила исключить требования или отдельные пункты. Это не подтверждено и не означает успешную проверку. Отвечайте только на известные вам вопросы и укажите основания. Ответы подготовят новую проверку, не изменяя этот отчёт и не запуская модель."), "notice"));
    panel.append(element("p", t(`${report.scope_confirmations.length} requirements need clarification. You can answer only some; the rest remain unverified. Downloading the report does not require answers.`, `Требуют уточнения: ${report.scope_confirmations.length}. Можно ответить на часть вопросов; остальные останутся непроверенными. Для скачивания отчёта ответы не нужны.`)));
    const search = connectionField(panel, t("Filter by requirement ID", "Фильтр по коду требования"), "", "search");
    const groups = [];
    const answers = [];
    for (const scope of report.scope_confirmations) {
      const section = disclosure(`${scope.requirement_id} · ${t("Questions", "Вопросов")}: ${scope.facts?.length || 0}`);
      section.append(element("p", scope.reason === "review_scope_exclusion_requires_confirmation" ? t("Confirm the scope before excluding this requirement or its clauses. The model’s assumption alone is not sufficient.", "Подтвердите область применимости перед исключением требования или его пунктов. Одного предположения модели недостаточно.") : scope.question || ""));
      if (scope.excluded_clause_ids?.length) section.append(element("p", t(`Clauses proposed for exclusion: ${scope.excluded_clause_ids.join(", ")}`, `Предложены к исключению пункты: ${scope.excluded_clause_ids.join(", ")}`), "muted"));
      for (const fact of scope.facts || []) {
        const card = element("div", "", "panel stack");
        card.append(element("p", fact.predicate_text || fact.predicate_id), element("p", fact.question || fact.explanation || ""));
        for (const evidence of fact.evidence || []) if (evidence.quote) card.append(element("p", evidence.quote));
        const answer = connectionSelect(card, t("Your answer (not the model's assumption)", "Ваш ответ (не предположение модели)"), "", [["", t("Choose an answer", "Выберите ответ")], ["true", t("Yes, the condition exists", "Да, условие выполняется")], ["false", t("No, the condition is absent", "Нет, условие отсутствует")], ["unknown", t("I do not know", "Мне неизвестно")]]);
        const reason = connectionField(card, t("Basis for your answer or uncertainty", "Основание ответа или причина неизвестности"), "", "textarea");
        answers.push({ requirement_id: scope.requirement_id, fact, answer, reason }); section.append(card);
      }
      groups.push({ requirement_id: scope.requirement_id, node: section });
    }
    const visibleGroups = element("div", "", "stack");
    const paging = element("div", "", "toolbar");
    const count = element("p"); count.setAttribute("aria-live", "polite");
    let page = 0;
    const drawPage = () => {
      const result = scopePage(groups, search.value, page); page = result.page;
      visibleGroups.replaceChildren(...result.items.map((group) => group.node));
      count.textContent = t(`Page ${page + 1} of ${result.pages} · ${result.total} requirements`, `Страница ${page + 1} из ${result.pages} · требований: ${result.total}`);
      previous.disabled = page === 0; next.disabled = page + 1 >= result.pages;
    };
    const previous = action(t("Previous", "Назад"), () => { page -= 1; drawPage(); }, { disabledWhen: () => page === 0 });
    const next = action(t("Next", "Далее"), () => { page += 1; drawPage(); }, { disabledWhen: () => page + 1 >= scopePage(groups, search.value, page).pages });
    search.addEventListener("input", () => { page = 0; drawPage(); });
    paging.append(previous, count, next); panel.append(visibleGroups, paging); drawPage();
    if (answers.length) panel.append(action(t("Prepare a new review with my answers", "Подготовить новую проверку с моими ответами"), async () => {
      const chosen = answers.filter((item) => item.answer.value || item.reason.value.trim());
      if (!chosen.length || chosen.some((item) => !item.answer.value || !item.reason.value.trim())) throw new Error(t("Choose an answer and provide a basis for each question you fill in.", "Для каждого заполненного вопроса выберите ответ и укажите основание."));
      const overrides = chosen.map((item) => ({ requirement_id: item.requirement_id, predicate_id: item.fact.predicate_id, side: "proposed", value: item.answer.value === "unknown" ? null : item.answer.value === "true", reason: item.reason.value.trim() }));
      const manifest = await api(`${prefix}/manifest`);
      if (!isCurrent()) return;
      const keys = new Set(overrides.map((item) => `${item.requirement_id}|${item.side}|${item.predicate_id}`));
      const retained = (manifest.request.fact_overrides || []).filter((item) => !keys.has(`${item.requirement_id}|${item.side}|${item.predicate_id}`));
      analysisPresets.set(pid, { ...manifest.request, mode: "review_documents", fact_overrides: [...retained, ...overrides] });
      setView("analysis"); await render();
    }));
    target.append(panel);
  }
  target.append(disclosure(t("Pinned documents and full report", "Зафиксированные документы и полный отчёт"), element("pre", JSON.stringify(report, null, 2))));
}

async function runResults(pid, rid, run, target, isCurrent) {
  const prefix = `/projects/${pid}/runs/${rid}`;
  if (!isCurrent()) return;
  const manifestForMode = await api(`${prefix}/manifest`);
  if (!isCurrent()) return;
  if (manifestForMode.request?.mode === "review_documents") return documentReviewResults(pid, rid, target, isCurrent);
  target.replaceChildren(
    element("h3", t("Requirements matrix", "Матрица требований")),
    element(
      "p",
      t(
        "Applicability, documentary assessment, enforcement, and review decision are independent fields.",
        "Применимость, documentary assessment, enforcement и review decision — независимые поля.",
      ),
    ),
  );
  if (run.documentary_summary) {
    const summary = run.documentary_summary;
    target.append(
      element(
        "p",
        t(
          () =>
            `${summary.rows} matrix rows. Unknown and incomplete assessments are tracked separately; implementation: not_verified.`,
          () =>
            `${summary.rows} строк матрицы. Неопределённые и незавершённые оценки учитываются отдельно; implementation: not_verified.`,
        ),
        "notice",
      ),
    );
    const proposed = summary.sides.proposed;
    target.append(element("pre", JSON.stringify(proposed, null, 2)));
    if ((proposed.applicability.applicable || 0) === 0)
      target.append(
        element(
          "p",
          t(
            "No requirements have established applicability. Check unknowns, selected scope, and author questions; this does not confirm security.",
            "Требований с установленной применимостью нет. Проверьте unknown, выбранный scope и вопросы автору; это не подтверждение безопасности.",
          ),
          "notice",
        ),
      );
  }
  const questions = await api(`${prefix}/questions`);
  if (!isCurrent()) return;
  if (
    questions.facts.length ||
    questions.operation_questions.length ||
    questions.questions.length
  ) {
    const panel = element("section", "", "panel stack");
    panel.append(element("h3", t("Questions for the author", "Уточнения к автору")));
    if (questions.questions.length) panel.append(element("pre", questions.questions.join("\n")));
    if (questions.operation_questions.length)
      panel.append(
        element(
          "p",
          t(
            "Some add/replace/delete operations are ambiguous. Clarify which behavior is added, replaced, or removed; the original snapshot is retained.",
            "Есть неоднозначные add/replace/delete. Уточните, какое поведение добавляется, заменяется или удаляется; исходный snapshot сохранён.",
          ),
          "notice",
        ),
        action(t("Clarify original delta", "Уточнить исходную delta"), async () => {
          const manifest = await api(`${prefix}/manifest`);
          const response = await fetch(
            `/api/v1/projects/${pid}/documents/${manifest.request.delta_document_id}`,
          );
          if (!response.ok)
            throw new Error(t("Original delta unavailable.", "Исходная delta недоступна."));
          const text = await response.text();
          if (projectId !== pid) return;
          const body = $("#detail-content");
          body.replaceChildren(element("h2", t("Clarify change", "Уточнение изменения")));
          const edited = connectionField(
            body,
            t("New Markdown delta version", "Новая версия delta в Markdown"),
            text,
            "textarea",
          );
          edited.rows = 12;
          body.append(
            action(
              t(
                "Save new delta and prepare analysis",
                "Сохранить новую delta и подготовить анализ",
              ),
              async () => {
                const saved = await api(
                  `/projects/${pid}/documents?filename=clarified.delta.md&role=delta`,
                  {
                    method: "POST",
                    headers: { "Content-Type": "application/octet-stream" },
                    body: edited.value,
                  },
                );
                analysisPresets.set(pid, {
                  ...manifest.request,
                  pipeline_ref: null,
                  delta_document_id: saved.id,
                  fact_overrides: [],
                  operation_overrides: [],
                });
                $("#details").close();
                setView("analysis");
                if (projectId === pid) await render();
              },
            ),
          );
          if (!$("#details").open) $("#details").showModal();
        }),
      );
    const answers = [];
    for (const fact of questions.facts) {
      const card = element("div", "", "stack panel");
      card.append(
        element("p", `${fact.requirement_id} · ${fact.side}: ${fact.predicate_text}`),
        element("p", fact.question || fact.explanation),
      );
      const answer = connectionSelect(card, t("Your answer", "Ваш ответ"), "unknown", [
        ["unknown", t("Unknown for now", "Пока неизвестно")],
        ["true", t("Yes — condition established", "Да — условие установлено")],
        [
          "false",
          t("No — absence of condition established", "Нет — отсутствие условия установлено"),
        ],
      ]);
      const reason = connectionField(
        card,
        t("Answer basis / reason for uncertainty", "Основание ответа / причина неизвестности"),
        "",
        "textarea",
      );
      answers.push({ fact, answer, reason });
      panel.append(card);
    }
    if (answers.length)
      panel.append(
        action(
          t(
            "Restart with explicit answers (new run)",
            "Перезапустить с явными ответами (новый run)",
          ),
          async () => {
            const overrides = answers
              .filter((item) => item.reason.value.trim())
              .map((item) => ({
                requirement_id: item.fact.requirement_id,
                predicate_id: item.fact.predicate_id,
                side: item.fact.side,
                value: item.answer.value === "unknown" ? null : item.answer.value === "true",
                reason: item.reason.value,
              }));
            if (!overrides.length)
              throw new Error(
                t(
                  "Provide a basis for at least one answer.",
                  "Укажите основание хотя бы одного ответа.",
                ),
              );
            const manifest = await api(`${prefix}/manifest`);
            const keys = new Set(
              overrides.map((item) => `${item.requirement_id}|${item.side}|${item.predicate_id}`),
            );
            const retained = (manifest.request.fact_overrides || []).filter(
              (item) => !keys.has(`${item.requirement_id}|${item.side}|${item.predicate_id}`),
            );
            analysisPresets.set(pid, {
              ...manifest.request,
              fact_overrides: [...retained, ...overrides],
            });
            setView("analysis");
            if (projectId === pid) await render();
          },
        ),
      );
    target.append(panel);
  }
  const matrixControls = element("div", "", "toolbar");
  const applicability = connectionSelect(matrixControls, t("Proposed applicability", "Применимость после изменений"), "", [
    ["", t("All", "Все")],
    ["applicable", t("Applicable", "Применимые")],
    ["not_applicable", t("Not applicable", "Неприменимые")],
    ["unknown", t("Unknown", "Неопределённые")],
  ]);
  const assessment = connectionSelect(matrixControls, t("Proposed assessment", "Оценка после изменений"), "", [
    ["", t("All", "Все")],
    ...[
      "satisfied_by_spec",
      "contradicted_by_spec",
      "not_addressed",
      "unknown",
      "not_assessed",
    ].map((key) => [key, key]),
  ]);
  const matrixList = element("div");
  target.append(matrixControls, matrixList);
  let matrixGeneration = 0;
  async function loadMatrix(offset) {
    const generation = ++matrixGeneration;
    const matrix = await api(
      prefix +
        `/assessments?offset=${offset}&limit=50&applicability=${encodeURIComponent(applicability.value)}&assessment=${encodeURIComponent(assessment.value)}`,
    );
    if (!isCurrent() || generation !== matrixGeneration) return;
    const wrap = element("div", "", "table-wrap");
    const table = element("table");
    const head = element("tr");
    ["ID", t("Baseline", "До изменений"), t("Proposed", "После изменений"), t("Effect", "Эффект"), t("Implementation", "Реализация")].forEach((label) => {
      const th = element("th", label);
      th.scope = "col";
      head.append(th);
    });
    table.append(head);
    for (const row of matrix) {
      const line = element("tr");
      [
        row.requirement_id,
        `${row.baseline.applicability} / ${row.baseline.assessment}`,
        `${row.proposed.applicability} / ${row.proposed.assessment}`,
        row.change_effect,
        "not_verified",
      ].forEach((value) => {
        line.append(element("td", value));
      });
      table.append(line);
    }
    wrap.append(table);
    matrixList.replaceChildren(wrap);
    const pager = element("div", "", "actions");
    if (offset)
      pager.append(
        action(t("Matrix: back", "Матрица: назад"), () => loadMatrix(Math.max(0, offset - 50))),
      );
    if (matrix.length === 50)
      pager.append(action(t("Matrix: next", "Матрица: далее"), () => loadMatrix(offset + 50)));
    pager.append(
      action(t("Current page details", "Подробности текущей страницы"), () =>
        details(t("Assessment matrix", "Матрица оценки"), matrix),
      ),
    );
    matrixList.append(pager);
  }
  applicability.addEventListener("change", () => loadMatrix(0).catch(showError));
  assessment.addEventListener("change", () => loadMatrix(0).catch(showError));
  await loadMatrix(0);
  let reviewRevision = run.review_revision;
  const filters = element("div", "", "toolbar");
  const findingType = connectionSelect(filters, t("Finding type", "Тип замечания"), "", [
    ["", t("All", "Все")],
    ...[
      "explicit_contradiction",
      "missing_security_design",
      "uncertain_applicability",
      "source_conflict",
      "delta_ambiguity",
      "existing_issue",
      "unverifiable_claim",
    ].map((key) => [key, key]),
  ]);
  const severity = connectionSelect(filters, t("Severity", "Критичность"), "", [
    ["", t("All", "Все")],
    ...["unrated", "critical", "high", "medium", "low"].map((key) => [key, key]),
  ]);
  const effect = connectionSelect(filters, t("Change effect", "Эффект изменения"), "", [
    ["", t("All", "Все")],
    ...["introduced", "regressed", "improved", "unchanged", "unresolved"].map((key) => [key, key]),
  ]);
  const review = connectionSelect(filters, t("Review state", "Статус проверки"), "", [
    ["", t("All", "Все")],
    ...["unreviewed", "accepted", "rejected", "needs_clarification", "deferred"].map((key) => [
      key,
      key,
    ]),
  ]);
  const findingList = element("section");
  target.append(element("h3", t("Findings", "Замечания")), filters, findingList);
  let findingGeneration = 0;
  async function loadFindings(offset) {
    const generation = ++findingGeneration;
    const findings = await api(
      prefix +
        `/findings?offset=${offset}&limit=25&type=${encodeURIComponent(findingType.value)}&severity=${encodeURIComponent(severity.value)}&effect=${encodeURIComponent(effect.value)}&review_state=${encodeURIComponent(review.value)}`,
    );
    if (!isCurrent() || generation !== findingGeneration) return;
    findingList.replaceChildren();
    if (!findings.length)
      findingList.append(
        element(
          "p",
          t("No findings match the selected filters.", "По выбранным фильтрам findings нет."),
          "empty",
        ),
      );
    for (const finding of findings) {
      const card = element("article", "", "panel");
      card.append(
        element("h3", `${finding.requirement_ref?.id || "Delta"} · ${finding.type}`),
        element(
          "p",
          `${finding.change_effect} · ${finding.severity} · review: ${finding.review_state}`,
        ),
        element("p", finding.explanation),
      );
      const refs = element("details");
      refs.append(
        element(
          "summary",
          t("Exact evidence snapshots / quotes", "Точные evidence snapshots / quotes"),
        ),
        element("pre", JSON.stringify(finding.evidence_refs, null, 2)),
      );
      card.append(refs);
      const comment = connectionField(
        card,
        t("Reason for review decision", "Причина решения review"),
        finding.review_comment || "",
        "textarea",
      );
      for (const [state, label] of [
        ["accepted", t("Accept finding", "Принять finding")],
        ["rejected", t("Reject finding", "Отклонить finding")],
        ["needs_clarification", t("Needs clarification", "Нужно уточнение")],
        ["deferred", t("Defer", "Отложить")],
      ])
        card.append(
          action(label, async () => {
            const value = await post(`${prefix}/decisions`, {
              expected_revision: reviewRevision,
              target_kind: "finding",
              target_id: finding.id,
              decision: state,
              comment: comment.value,
            });
            reviewRevision = value.review_revision;
            setStatus(
              t(
                () => `Review: ${state}. Assessment preserved.`,
                () => `Review: ${state}. Assessment сохранён.`,
              ),
            );
            await loadFindings(offset);
          }),
        );
      findingList.append(card);
      if (finding.requirement_ref) {
        const adoption = element("details");
        adoption.append(
          element(
            "summary",
            t(
              "Adoption / waiver for a new run, with reason",
              "Adoption / waiver для нового run с причиной",
            ),
          ),
        );
        adoption.append(
          element(
            "p",
            t(
              "This is a policy adoption decision. Original force/nature and documentary assessments remain in history.",
              "Это решение об использовании policy. Исходные force/nature и documentary assessment в истории сохраняются.",
            ),
          ),
        );
        const mode = connectionSelect(adoption, t("Adoption mode", "Режим принятия"), "required", [
          [
            "required",
            t(
              "Required defaults of the accepted source profile",
              "Required defaults принятого source profile",
            ),
          ],
          ["advisory", "Advisory adoption"],
          ["waived", t("Waived with an explicit reason", "Waived с явной причиной")],
          ["undecided", t("Undecided", "Решение не принято")],
        ]);
        const reason = connectionField(
          adoption,
          t("Adoption / waiver reason", "Причина adoption / waiver"),
          "",
          "textarea",
        );
        adoption.append(
          action(
            t(
              "Prepare a new analysis with this adoption",
              "Подготовить новый анализ с этим adoption",
            ),
            async () => {
              if (!reason.value.trim())
                throw new Error(
                  t("Provide an adoption / waiver reason.", "Укажите причину adoption / waiver."),
                );
              const manifest = await api(`${prefix}/manifest`);
              const retained = (manifest.request.adoption_overrides || []).filter(
                (item) => item.requirement_id !== finding.requirement_ref.id,
              );
              analysisPresets.set(pid, {
                ...manifest.request,
                adoption_overrides: [
                  ...retained,
                  {
                    requirement_id: finding.requirement_ref.id,
                    mode: mode.value,
                    reason: reason.value,
                  },
                ],
              });
              setView("analysis");
              if (projectId === pid) await render();
            },
          ),
        );
        card.append(adoption);
      }
    }
    const pager = element("div", "", "actions");
    if (offset)
      pager.append(
        action(t("Findings: back", "Findings: назад"), () =>
          loadFindings(Math.max(0, offset - 25)),
        ),
      );
    if (findings.length === 25)
      pager.append(action(t("Findings: next", "Findings: далее"), () => loadFindings(offset + 25)));
    findingList.append(pager);
  }
  [findingType, severity, effect, review].forEach((input) => {
    input.addEventListener("change", () => loadFindings(0).catch(showError));
  });
  await loadFindings(0);
  let proposal;
  try {
    proposal = await api(`${prefix}/proposal`);
  } catch {
    target.append(
      element(
        "p",
        t(
          "Suggested edits are not available yet; check stage errors.",
          "Предложения правок ещё недоступны; смотрите ошибки стадий.",
        ),
        "notice",
      ),
    );
    return;
  }
  target.append(
    action(
      t(
        "Independent P8 check: proposal critique and reassessment",
        "Независимый контроль P8: critique и reassessment предложения",
      ),
      () => showCritiquePage(pid, rid, 0),
    ),
  );
  if (proposal.incomplete || proposal.questions.length)
    target.append(
      element(
        "p",
        t(
          "The proposal is incomplete or contains questions. Unresolved findings are retained; inspect the critique and merge conflict artifacts.",
          "Предложение неполное или содержит вопросы. Неразрешённые findings сохраняются; ознакомьтесь с critique и артефактами merge conflicts.",
        ),
        "notice",
      ),
    );
  const patches = element("section");
  target.append(element("h3", t("Selective patches", "Выборочные исправления")), patches);
  const selection = new Map();
  const editors = new Map();
  for (const patch of proposal.patches) {
    const card = element("article", "", "panel stack");
    card.append(
      element("h3", patch.id),
      element("p", patch.explanation),
      element(
        "p",
        patch.changes_business_scope
          ? t("Warning: changes business scope.", "Внимание: меняет business scope.")
          : t("Proposal requires review.", "Предложение требует review."),
      ),
    );
    const selected = connectionField(
      card,
      t(
        "Include in preview / export (export requires an accepted decision)",
        "Включить в preview / экспорт (экспорт требует принятого решения)",
      ),
      patch.review_state === "accepted",
      "checkbox",
    );
    selection.set(patch.id, selected);
    card.append(element("pre", patch.original || t("Insertion", "Вставка")));
    const replacement = connectionField(
      card,
      t(
        "Suggested Markdown (manual edits require a new analysis)",
        "Предлагаемый Markdown (ручная правка требует нового анализа)",
      ),
      patch.replacement,
      "textarea",
    );
    replacement.rows = 6;
    editors.set(patch.id, replacement);
    const comment = connectionField(
      card,
      t("Patch decision comment", "Комментарий к patch decision"),
      patch.review_comment || "",
    );
    for (const [state, label] of [
      ["accepted", t("Accept patch", "Принять правку")],
      ["rejected", t("Reject patch", "Отклонить правку")],
      ["deferred", t("Defer patch", "Отложить правку")],
    ])
      card.append(
        action(label, async () => {
          if (replacement.value !== patch.replacement)
            throw new Error(
              t(
                "Save the manual version as a new delta and analyze it first.",
                "Ручной вариант сначала сохраните новой delta и проанализируйте.",
              ),
            );
          const value = await post(`${prefix}/decisions`, {
            expected_revision: reviewRevision,
            target_kind: "patch",
            target_id: patch.id,
            decision: state,
            comment: comment.value,
          });
          reviewRevision = value.review_revision;
          selected.checked = state === "accepted";
          setStatus(`Patch: ${state}`);
        }),
      );
    patches.append(card);
  }
  const selectedIds = () =>
    Array.from(selection)
      .filter(([, input]) => input.checked)
      .map(([id]) => id);
  const previewBody = () => ({
    expected_review_revision: reviewRevision,
    expected_original_sha256: proposal.original_sha256,
    selected_patch_ids: selectedIds(),
    edited_replacements: Object.fromEntries(
      proposal.patches
        .filter(
          (patch) =>
            selection.get(patch.id).checked && editors.get(patch.id).value !== patch.replacement,
        )
        .map((patch) => [patch.id, editors.get(patch.id).value]),
    ),
  });
  target.append(
    action(t("Inspect selected diff", "Посмотреть выбранные изменения"), async () => {
      const value = await post(`${prefix}/proposal/preview`, previewBody());
      const body = $("#detail-content");
      body.replaceChildren(element("h2", t("Selected changes", "Выбранные изменения")));
      const columns = element("div", "", "diff-columns");
      columns.append(element("pre", value.original), element("pre", value.proposed));
      body.append(columns, element("pre", value.diff));
      if (value.requires_new_analysis)
        body.append(
          element(
            "p",
            t(
              "The selected version requires a new analysis. P8 assessments apply to the full candidate and do not validate this document.",
              "Выбранная версия требует нового анализа. Оценки P8 относятся к полному candidate и не подтверждают этот документ.",
            ),
            "notice",
          ),
        );
      if (!$("#details").open) $("#details").showModal();
    }),
    action(
      t(
        "Save manual version as a new delta and analyze",
        "Сохранить ручной вариант новой delta и проверить",
      ),
      async () => {
        const value = await post(`${prefix}/proposal/preview`, previewBody());
        const saved = await api(
          `/projects/${pid}/documents?filename=review-edited.delta.md&role=delta`,
          {
            method: "POST",
            headers: { "Content-Type": "application/octet-stream" },
            body: value.proposed,
          },
        );
        const manifest = await api(`${prefix}/manifest`);
        analysisPresets.set(pid, {
          ...manifest.request,
          pipeline_ref: null,
          delta_document_id: saved.id,
          fact_overrides: [],
          operation_overrides: [],
        });
        setView("analysis");
        if (projectId === pid) await render();
      },
    ),
    element(
      "p",
      t(
        "Export includes the selected specification, JSON/Markdown review, diff, and manifest. When only some edits are selected, the P8 assessment stays bound to the full candidate; the selected version requires a new analysis. Document quotations may be sensitive; credentials and raw model payloads are excluded.",
        "Экспорт содержит выбранную спецификацию, JSON/Markdown review, diff и manifest. При выборе части правок оценка P8 сохраняет привязку к полному candidate; выбранная версия требует нового анализа. Цитаты документов могут быть чувствительными; credentials и raw model payloads не включаются.",
      ),
      "notice",
    ),
    action(
      t("Export accepted selected edits", "Экспортировать принятые выбранные правки"),
      async () => {
        if (Object.keys(previewBody().edited_replacements).length)
          throw new Error(
            t(
              "Manual edits require a new analysis before export.",
              "Ручные изменения требуют нового анализа перед экспортом.",
            ),
          );
        const result = await post(`${prefix}/exports`, {
          expected_review_revision: reviewRevision,
          expected_original_sha256: proposal.original_sha256,
          accepted_patch_ids: selectedIds(),
          idempotency_key: crypto.randomUUID(),
        });
        const link = element("a", t("Download new result ZIP", "Скачать новый ZIP результата"));
        link.href = `/api/v1${prefix}/exports/${result.id}`;
        link.download = result.filename;
        target.append(link);
        if (result.requires_new_analysis)
          target.append(
            element(
              "p",
              t(
                "Exported version was not assessed by P8; a new analysis is required.",
                "Экспортированная версия не оценена P8; требуется новый анализ.",
              ),
              "notice",
            ),
          );
        link.focus();
      },
    ),
  );
}

async function showCritiquePage(pid, rid, offset) {
  const result = await api(`/projects/${pid}/runs/${rid}/critique?offset=${offset}&limit=25`);
  details(
    t(
      "Independent proposal check · implementation not verified",
      "Независимый контроль предложения · реализация не проверена",
    ),
    result,
  );
  const box = $("#detail-content");
  if (offset)
    box.append(
      action(t("P8: back", "P8: назад"), () =>
        showCritiquePage(pid, rid, Math.max(0, offset - 25)),
      ),
    );
  if (
    offset + 25 <
    Math.max(
      result.critique_count,
      result.reassessment_count,
      result.candidate_assessment_count || 0,
    )
  )
    box.append(action(t("P8: next", "P8: далее"), () => showCritiquePage(pid, rid, offset + 25)));
}

export {
  selectedRuns,
  analysisPresets,
  runEventSource,
  closeRunEvents,
  referenceKey,
  selectedReference,
  withPinnedReferences,
  checkedList,
  renderNewRun,
  renderHistory,
  renderRun,
  artifactInspector,
  runResults,
  showCritiquePage,
};
