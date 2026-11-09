import { t } from "./i18n.js";
import { errorMessage, filePicker } from "./app.js";
import { inspectSkill } from "./skill-import.js";
import { connectionField, connectionSelect } from "./connections.js";
import { analysisPresets } from "./runs.js";
import { $, projectId, setView, disclosure, element, api, post, action, render } from "./app.js";

async function renderConfiguration(pid, content, isCurrent) {
  const [initial, prompts, skills, pipelines] = await Promise.all([
    api("/pipeline-defaults"),
    api(`/projects/${pid}/configuration/prompt`),
    api(`/projects/${pid}/configuration/skill`),
    api(`/projects/${pid}/configuration/pipeline`),
  ]);
  if (!isCurrent()) return;
  content.append(element("h2", t("Skills and analysis settings", "Навыки и настройки анализа")));
  content.append(element("p", t("Skills are optional. Start with built-in prompts, or import SKILL.md as an inactive text draft.", "Навыки необязательны. Начните со встроенных промптов или импортируйте SKILL.md как неактивный текстовый черновик.")));
  content.append(disclosure(t("How imported skills work", "Как работают импортированные навыки"), element("p", t("Import keeps the entry text, not its dependencies or tools. Adapt the instructions and output schema, publish the prompt, then create a declarative skill referencing it and select it in Advanced analysis. Before/replace/after hooks apply to a model step. Mandatory application safety and normative checks remain enforced. Arbitrary scripts and Hub packages are not executed.", "Импорт сохраняет входной текст, но не зависимости и инструменты. Адаптируйте инструкции и формат ответа, опубликуйте промпт, затем создайте декларативный навык с этим промптом и выберите его в дополнительных настройках анализа. Навык работает до, вместо или после этапа модели. Обязательные проверки безопасности и нормативной логики сохраняются. Произвольные скрипты и пакеты хаба не исполняются."))));
  content.append(
    action(t("Import SKILL.md as draft", "Импорт SKILL.md в черновик"), () => importSkill(pid)),
    action(t("New prompt", "Новый промпт"), () => editPrompt(pid, initial)),
    action(t("New declarative skill", "Новый декларативный навык"), () => editSkill(pid, prompts)),
  );
  content.append(
    element("h3", t("Named pipelines without documents", "Сохранённые настройки анализа")),
  );
  for (const record of pipelines) {
    const panel = element("section", "", "panel");
    panel.append(
      element("h3", `${record.content.name} · r${record.revision}`),
      element("pre", JSON.stringify(record.content, null, 2)),
      action(
        t(
          "Use this pipeline revision for a new analysis",
          "Использовать эту версию настроек для нового анализа",
        ),
        async () => {
          analysisPresets.set(pid, {
            ...record.content,
            pipeline_ref: { id: record.id, revision: record.revision },
          });
          setView("analysis");
          await render();
        },
      ),
      action(t("Pipeline versions / rollback", "Версии настроек и возврат к предыдущей"), () =>
        configurationVersions(pid, "pipeline", record),
      ),
    );
    content.append(panel);
  }

  const builtins = disclosure(t("Inspect built-in prompts", "Посмотреть встроенные промпты"));
  content.append(builtins);
  for (const [stage, prompt] of Object.entries(initial.prompts)) {
    const panel = element("section", "", "panel");
    panel.append(
      element("h3", `${stage} · ${prompt.version}`),
      element("pre", prompt.template),
      action(t(`Create a custom ${stage} version`, `Создать собственную версию ${stage}`), () =>
        editPrompt(pid, initial, null, stage),
      ),
    );
    builtins.append(panel);
  }
  for (const [kind, records, title] of [
    ["prompt", prompts, t("Custom prompts", "Собственные промпты")],
    ["skill", skills, t("Skills", "Навыки")],
  ]) {
    content.append(element("h3", title));
    if (!records.length)
      content.append(
        element("p", t("No saved versions yet.", "Пока нет сохранённых версий."), "empty"),
      );
    for (const record of records) {
      const item = record.content;
      const panel = element("section", "", "panel");
      panel.append(
        element("h3", `${item.name} · ${item.version} · r${record.revision}`),
        element(
          "p",
          t(
            `${item.stage} · ${item.status || `${item.hook} / order ${item.order}`} · ${item.enabled === false ? "disabled" : ""}`,
            `${item.stage} · ${item.status || `${item.hook} / order ${item.order}`} · ${item.enabled === false ? "отключён" : ""}`,
          ),
        ),
        element("p", record.sha256),
        action(t(`Edit ${item.name}`, `Редактировать ${item.name}`), () =>
          kind === "prompt" ? editPrompt(pid, initial, record) : editSkill(pid, prompts, record),
        ),
        action(
          t(`Versions / diff / rollback: ${item.name}`, `Версии / diff / rollback: ${item.name}`),
          () => configurationVersions(pid, kind, record),
        ),
        action(
          t(`Synthetic fixture test: ${item.name}`, `Тест на synthetic fixture: ${item.name}`),
          () => configurationTest(pid, kind, record),
        ),
      );
      content.append(panel);
    }
  }
}

function importSkill(pid) {
  const box = $("#detail-content");
  box.replaceChildren(element("h2", t("Import SKILL.md as draft", "Импорт SKILL.md в черновик")));
  box.append(element("p", t("Choose one entry file from skills/<name>/SKILL.md. Preview stays in this browser until you save. References, assets, scripts, Bifrost manifests and tool permissions are not imported or executed. This is a text draft, not a working installed skill.", "Выберите входной файл skills/<name>/SKILL.md. Предпросмотр остаётся в браузере до сохранения. References, assets, scripts, манифесты Bifrost и разрешения инструментов не импортируются и не исполняются. Это текстовый черновик, а не установленный рабочий навык."), "notice"));
  const input = element("input");
  input.type = "file"; input.accept = ".md"; input.id = "skill-import-file";
  const preview = element("section", "", "stack");
  const status = element("p", "", "error"); status.setAttribute("role", "alert");
  box.append(filePicker(input), status, preview);
  let generation = 0;
  input.addEventListener("change", async () => {
    const current = ++generation;
    preview.replaceChildren(); status.textContent = "";
    try {
      const file = input.files?.[0]; if (!file) return;
      if (file.size > 100 * 1024) throw new Error(t("Maximum file size: 100 KiB.", "Максимальный размер файла: 100 KiB."));
      const bytes = await file.arrayBuffer();
      const source = inspectSkill(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes), file.name);
      if (current !== generation) return;
      const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(x => x.toString(16).padStart(2, "0")).join("");
      if (current !== generation) return;
      preview.append(element("h3", source.name), element("p", source.description), element("p", `SHA-256: ${hash}`));
      preview.append(element("p", t("Compatibility: manual adaptation required. No repository access, code edits, scanners, commands or external tools are provided by this importer. A specification review cannot verify code implementation.", "Совместимость: нужна ручная адаптация. Импорт не даёт доступа к репозиторию, редактированию кода, сканерам, командам или внешним инструментам. Проверка спецификации не подтверждает реализацию в коде."), "notice"));
      if (!source.metadataRecognized) preview.append(element("p", t("Metadata was not fully recognized. Only simple name/description fields are read; original text is preserved.", "Метаданные распознаны не полностью. Читаются только простые поля name/description; исходный текст сохранён.")));
      preview.append(element("h4", t("Referenced resources (not imported or opened)", "Ссылки и ресурсы (не импортируются и не открываются)")), element("pre", source.links.join("\n") || t("No Markdown links detected. This is not a dependency audit.", "Markdown-ссылки не обнаружены. Это не полный аудит зависимостей.")));
      preview.append(element("h4", t("Possible commands/tool declarations (not executed)", "Возможные команды и инструменты (не исполняются)")), element("pre", source.commands.join("\n") || t("No command patterns detected. Manual review is still required.", "Шаблоны команд не обнаружены. Ручная проверка всё равно нужна.")));
      preview.append(disclosure(t("Original text (plain text, not rendered)", "Исходный текст (без исполнения разметки)"), element("pre", source.text)));
      if (source.variablesUnsupported) {
        preview.append(element("p", t("Cannot save: template braces {{ or }} conflict with the prompt format. Adapt a separate copy first; the original file is unchanged.", "Сохранение недоступно: скобки {{ или }} конфликтуют с форматом промпта. Сначала адаптируйте отдельную копию; исходный файл не изменён."), "error"));
        return;
      }
      const save = action(t("Save inactive draft", "Сохранить неактивный черновик"), async () => {
        save.disabled = true; input.disabled = true;
        try {
          await post(`/projects/${pid}/configuration/prompt`, { content: {
            name: source.name, version: "1.0.0", stage: "skill", template: source.text,
            allowed_variables: [], output_schema: { type: "object" }, status: "draft",
            changelog: `Imported SKILL.md text only. SHA-256 ${hash}. Dependencies not imported; manual adaptation and output contract review required.`,
          }, expected_revision: null });
          $("#details").close(); if (projectId === pid) await render();
        } catch (error) { status.textContent = errorMessage(error); save.disabled = false; input.disabled = false; }
      });
      preview.append(element("p", t("Saved drafts are not selected or executed in analysis. Edit the draft to adapt instructions and output schema; publication is a separate action.", "Черновики не выбираются и не выполняются в анализе. Отредактируйте инструкции и формат ответа; публикация выполняется отдельно.")), save);
    } catch (error) { if (current === generation) status.textContent = errorMessage(error); }
  });
  if (!$("#details").open) $("#details").showModal();
}

function configSave(form, pid, kind, record, value) {
  const submit = element("button", t("Save immutable version", "Сохранить неизменяемую версию"));
  submit.type = "submit";
  const message = element("p", "", "error");
  message.setAttribute("role", "alert");
  form.append(submit, message);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const content = value();
      await post(
        `/projects/${pid}/configuration/${kind}${record ? `/${record.id}` : ""}`,
        { content, expected_revision: record?.revision || null },
        record ? "PUT" : "POST",
      );
      $("#details").close();
      if (projectId === pid) await render();
    } catch (error) {
      message.textContent = errorMessage(error);
    } finally {
      submit.disabled = false;
    }
  });
}

/** @param {string} pid
 * @param {PipelineDefaults} initial
 * @param {RegistryRecord<import('./api.generated').APIPromptDefinition> | null} [record]
 * @param {string} [initialStage]
 */
function editPrompt(pid, initial, record = null, initialStage = "P3") {
  const box = $("#detail-content");
  box.replaceChildren(element("h2", t("Prompt version", "Версия промпта")));
  const form = element("form", "", "stack");
  /** @type {Partial<import('./api.generated').APIPromptDefinition>} */
  const data = record?.content || {};
  const name = connectionField(
    form,
    t("Prompt name", "Название промпта"),
    data.name || t("Custom prompt", "Собственный промпт"),
  );
  const version = connectionField(
    form,
    t("SemVer version", "Версия SemVer"),
    data.version || "1.0.0",
  );
  const stage = connectionSelect(form, t("Model step", "Этап модели"), data.stage || initialStage, [
    ...Object.keys(initial.contracts).map((key) => [key, key]),
    ["skill", t("Additional skill", "Дополнительный навык")],
  ]);
  const template = connectionField(
    form,
    t("Instruction (trusted configuration)", "Инструкция (доверенная конфигурация)"),
    data.template || initial.prompts[initialStage].template,
    "textarea",
  );
  template.rows = 12;
  form.append(
    element(
      "p",
      t(
        "Variables: {{stage}}, {{output_contract}}. Document data is always sent as a separate untrusted user message. The server adds the mandatory application policy.",
        "Переменные: {{stage}}, {{output_contract}}. Данные документов передаются отдельным сообщением как недоверенные данные. Обязательные правила приложения добавляются сервером.",
      ),
      "notice",
    ),
  );
  const schema = connectionField(
    form,
    "Output JSON Schema",
    JSON.stringify(data.output_schema || initial.contracts[stage.value], null, 2),
    "textarea",
  );
  schema.rows = 8;
  const state = connectionSelect(
    form,
    t("Prompt state", "Состояние промпта"),
    data.status || "draft",
    [
      ["draft", t("Draft — not used in runs", "Черновик — не используется в анализе")],
      [
        "published",
        t("Published — available for new runs", "Опубликован — доступен для нового анализа"),
      ],
    ],
  );
  const changelog = connectionField(
    form,
    t("Changelog", "Что изменено"),
    data.changelog || t("Created a custom draft", "Создан собственный черновик"),
    "textarea",
  );
  const value = () => ({
    name: name.value,
    version: version.value,
    stage: stage.value,
    template: template.value,
    allowed_variables: ["stage", "output_contract"],
    output_schema: JSON.parse(schema.value),
    status: state.value,
    changelog: changelog.value,
  });
  const sync = () => {
    schema.readOnly = stage.value !== "skill";
    if (stage.value !== "skill")
      schema.value = JSON.stringify(initial.contracts[stage.value], null, 2);
  };
  stage.addEventListener("change", () => {
    if (!record && stage.value !== "skill") template.value = initial.prompts[stage.value].template;
    sync();
  });
  sync();
  form.append(
    action(t("Preview with synthetic input", "Предпросмотр с синтетическими входными данными"), async () => {
      const result = await post("/prompt-preview", {
        content: value(),
        data: { synthetic: true, text: "Generated fixture only" },
      });
      preview.textContent = JSON.stringify(result, null, 2);
    }),
  );
  const preview = element("pre", t("Preview not prepared yet.", "Предпросмотр ещё не подготовлен."));
  form.append(preview);
  configSave(form, pid, "prompt", record, value);
  box.append(form);
  if (!$("#details").open) $("#details").showModal();
}

/** @param {string} pid
 * @param {Array<RegistryRecord<import('./api.generated').APIPromptDefinition>>} prompts
 * @param {RegistryRecord<import('./api.generated').APISkillDefinition> | null} [record]
 */
function editSkill(pid, prompts, record = null) {
  const box = $("#detail-content");
  box.replaceChildren(element("h2", t("Declarative skill", "Декларативный навык")));
  const form = element("form", "", "stack");
  /** @type {Partial<import('./api.generated').APISkillDefinition>} */
  const data = record?.content || {};
  const name = connectionField(
    form,
    t("Skill name", "Название навыка"),
    data.name || t("Additional check", "Дополнительная проверка"),
  );
  const version = connectionField(
    form,
    t("SemVer version", "Версия SemVer"),
    data.version || "1.0.0",
  );
  const description = connectionField(
    form,
    t("Skill purpose", "Назначение навыка"),
    data.description || "",
    "textarea",
  );
  const stage = connectionSelect(
    form,
    t("Skill stage", "Этап выполнения навыка"),
    data.stage || "P5",
    ["P2", "P3", "P5", "P7", "P8"].map((key) => [key, key]),
  );
  const hook = connectionSelect(form, t("Hook", "Место выполнения"), data.hook || "after", [
    ["before", t("Before — before the model step", "До этапа модели")],
    ["replace", t("Replace — instead of the model step", "Вместо этапа модели")],
    ["after", t("After — after the model step", "После этапа модели")],
  ]);
  const order = connectionField(
    form,
    t("Order within hook", "Порядок выполнения в выбранном месте"),
    data.order || 0,
    "number",
  );
  const available = prompts.filter((record) => record.content.status === "published");
  const prompt = connectionSelect(
    form,
    t("Pinned published prompt version", "Закреплённая версия опубликованного промпта"),
    data.prompt_ref?.id || "",
    [
      [
        "",
        t("Select a prompt with a compatible contract", "Выберите промпт с подходящим форматом ответа"),
      ],
      ...available.map((record) => [
        record.id,
        `${record.content.name} · ${record.content.stage} · r${record.revision}`,
      ]),
    ],
  );
  form.append(
    element(
      "p",
      t(
        "Before/after return untrusted annotations for subsequent model steps and the report. Replace must follow the original output contract. Outputs do not change the catalog, settings, or review. allowed_tools is always empty.",
        "Навыки до и после этапа возвращают недоверенные аннотации для следующих этапов и отчёта. Замещающий навык должен соблюдать исходный формат ответа. Результаты не меняют каталог, настройки или проверку. Список allowed_tools всегда пуст.",
      ),
      "notice",
    ),
  );
  const inputSchema = connectionField(
    form,
    t("Input JSON Schema", "Схема входных данных JSON"),
    JSON.stringify(
      data.input_schema || {
        type: "object",
        properties: { stage_input: { type: "object" }, prior_skill_outputs: { type: "array" } },
        required: ["stage_input", "prior_skill_outputs"],
        additionalProperties: false,
      },
      null,
      2,
    ),
    "textarea",
  );
  const timeout = connectionField(
    form,
    t("Skill timeout, seconds", "Срок ожидания навыка, секунд"),
    data.timeout || 30,
    "number",
  );
  const requests = connectionField(
    form,
    t("Maximum skill requests", "Максимум запросов навыка"),
    data.budget?.max_requests || 20,
    "number",
  );
  const tokens = connectionField(
    form,
    t("Maximum skill output tokens", "Максимум токенов ответа навыка"),
    data.budget?.max_output_tokens || 2048,
    "number",
  );
  const affects = connectionField(
    form,
    t(
      "Affects assessment (must stop on failure)",
      "Влияет на оценку (при ошибке анализ должен остановиться)",
    ),
    data.affects_assessment ?? true,
    "checkbox",
  );
  const failure = connectionSelect(
    form,
    t("Failure policy", "Действие при ошибке"),
    data.failure_policy || "stop",
    [
      ["stop", t("Stop — stop analysis", "Остановить анализ")],
      [
        "continue_with_warning",
        t(
          "Continue with warning — reporting skills only",
          "Продолжить с предупреждением — только для отчётного навыка",
        ),
      ],
    ],
  );
  const enabled = connectionField(
    form,
    t(
      "Enable this skill version for selection in new runs",
      "Разрешить выбор этой версии навыка в новых анализах",
    ),
    data.enabled || false,
    "checkbox",
  );
  configSave(form, pid, "skill", record, () => {
    const selected = available.find((item) => item.id === prompt.value);
    if (!selected) throw new Error(t("Select a published prompt.", "Выберите опубликованный промпт."));
    return {
      name: name.value,
      version: version.value,
      description: description.value,
      kind: "prompt",
      stage: stage.value,
      hook: hook.value,
      order: Number(order.value),
      input_schema: JSON.parse(inputSchema.value),
      output_schema: selected.content.output_schema,
      prompt_ref: { id: selected.id, revision: selected.revision },
      allowed_tools: [],
      data_scopes: ["delta", "baseline_fragments", "catalog", "skill_outputs"],
      timeout: Number(timeout.value),
      budget: { max_requests: Number(requests.value), max_output_tokens: Number(tokens.value) },
      affects_assessment: affects.checked,
      failure_policy: failure.value,
      enabled: enabled.checked,
    };
  });
  box.append(form);
  if (!$("#details").open) $("#details").showModal();
}

async function configurationVersions(pid, kind, record) {
  const versions = await api(`/projects/${pid}/configuration/${kind}/${record.id}/versions`);
  const box = $("#detail-content");
  box.replaceChildren(element("h2", t("Immutable versions", "Неизменяемые версии")));
  const preview = element("pre", t("Select a version to compare.", "Выберите версию для сравнения."));
  for (const version of versions)
    box.append(
      element("p", `r${version.revision} · ${version.created_at} UTC · ${version.sha256}`),
      action(
        t(`Diff r${version.revision} → current`, `Diff r${version.revision} → текущая`),
        async () => {
          preview.textContent =
            (
              await api(
                `/projects/${pid}/configuration/${kind}/${record.id}/diff?before=${version.revision}&after=${record.revision}`,
              )
            ).diff || t("No changes", "Без изменений");
        },
      ),
      action(
        t(
          `Roll back r${version.revision} as a new revision`,
          `Rollback r${version.revision} новой revision`,
        ),
        async () => {
          await post(`/projects/${pid}/configuration/${kind}/${record.id}/rollback`, {
            expected_revision: record.revision,
            source_revision: version.revision,
          });
          $("#details").close();
          if (projectId === pid) await render();
        },
      ),
    );
  box.append(preview);
  if (!$("#details").open) $("#details").showModal();
}

async function configurationTest(pid, kind, record) {
  const connections = (await api(`/projects/${pid}/connections`)).filter(
    (record) => record.content.kind === "llm",
  );
  const box = $("#detail-content");
  box.replaceChildren(
    element("h2", t("Live synthetic fixture test", "Проверка на синтетических данных")),
    element(
      "p",
      t(
        "The selected endpoint will receive generated test data only. Success means JSON Schema compliance, not semantic quality.",
        "Выбранный сервер получит только сгенерированные тестовые данные. Успешная проверка подтверждает соответствие JSON Schema, но не качество смысла ответа.",
      ),
      "notice",
    ),
  );
  const model = connectionSelect(
    box,
    t("Test request destination", "Направление тестового запроса"),
    "",
    [
      ["", t("Select an endpoint", "Выберите сервер")],
      ...connections.map((item) => [item.id, `${item.content.endpoint} · ${item.content.model}`]),
    ],
  );
  const output = element("pre", t("Test has not been run.", "Тест не запускался."));
  box.append(
    action(
      t("Send the displayed synthetic request", "Отправить показанный синтетический запрос"),
      async () => {
        const selected = connections.find((item) => item.id === model.value);
        if (!selected) throw new Error(t("Select an endpoint.", "Выберите сервер."));
        output.textContent = JSON.stringify(
          await post(`/projects/${pid}/configuration/${kind}/${record.id}/test`, {
            connection: { id: selected.id, revision: selected.revision },
            revision: record.revision,
          }),
          null,
          2,
        );
      },
    ),
    output,
  );
  if (!$("#details").open) $("#details").showModal();
}

export {
  renderConfiguration,
  configSave,
  editPrompt,
  editSkill,
  configurationVersions,
  configurationTest,
};
