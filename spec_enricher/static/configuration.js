import { t } from "./i18n.js";
import { errorMessage } from "./app.js";
import { connectionField, connectionSelect } from "./connections.js";
import { analysisPresets } from "./runs.js";
import { $, projectId, setView, element, api, post, action, render } from "./app.js";

async function renderConfiguration(pid, content, isCurrent) {
  const [initial, prompts, skills, pipelines] = await Promise.all([
    api("/pipeline-defaults"),
    api(`/projects/${pid}/configuration/prompt`),
    api(`/projects/${pid}/configuration/skill`),
    api(`/projects/${pid}/configuration/pipeline`),
  ]);
  if (!isCurrent()) return;
  content.append(
    element(
      "h2",
      t("Pipeline, prompts, and declarative skills", "Pipeline, промпты и декларативные skills"),
    ),
    element(
      "p",
      t(
        "P0 → P1 → P2 → P3 → P4 → P5 → P6 → P7 → P8. Safety checks and normative logic remain mandatory. Before/replace/after hooks run around a specific model step. External SKILL.md content can be manually copied into a text draft; commands are not executed.",
        "P0 → P1 → P2 → P3 → P4 → P5 → P6 → P7 → P8. Safety checks и нормативная логика остаются обязательными. Hooks before/replace/after исполняются вокруг конкретного model-step. Внешние SKILL.md можно вручную перенести в текстовый draft; команды не исполняются.",
      ),
      "notice",
    ),
  );
  content.append(
    action(t("New prompt", "Новый промпт"), () => editPrompt(pid, initial)),
    action(t("New declarative skill", "Новый декларативный skill"), () => editSkill(pid, prompts)),
  );
  content.append(
    element("h3", t("Named pipelines without documents", "Именованные pipelines без документов")),
  );
  for (const record of pipelines) {
    const panel = element("section", "", "panel");
    panel.append(
      element("h3", `${record.content.name} · r${record.revision}`),
      element("pre", JSON.stringify(record.content, null, 2)),
      action(
        t(
          "Use this pipeline revision for a new analysis",
          "Использовать эту pipeline revision для нового анализа",
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
      action(t("Pipeline versions / rollback", "Версии / rollback pipeline"), () =>
        configurationVersions(pid, "pipeline", record),
      ),
    );
    content.append(panel);
  }

  content.append(element("h3", t("Built-in prompts", "Встроенные промпты")));
  for (const [stage, prompt] of Object.entries(initial.prompts)) {
    const panel = element("section", "", "panel");
    panel.append(
      element("h3", `${stage} · ${prompt.version}`),
      element("pre", prompt.template),
      action(t(`Create a custom ${stage} version`, `Создать собственную версию ${stage}`), () =>
        editPrompt(pid, initial, null, stage),
      ),
    );
    content.append(panel);
  }
  for (const [kind, records, title] of [
    ["prompt", prompts, t("Custom prompts", "Собственные промпты")],
    ["skill", skills, "Skills"],
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
  const stage = connectionSelect(form, "Model-step", data.stage || initialStage, [
    ...Object.keys(initial.contracts).map((key) => [key, key]),
    ["skill", t("Additional skill", "Дополнительный skill")],
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
        "Переменные: {{stage}}, {{output_contract}}. Данные документов всегда идут отдельным недоверенным user message. Обязательная policy приложения добавляется сервером.",
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
      ["draft", t("Draft — not used in runs", "Draft — не используется в run")],
      [
        "published",
        t("Published — available for new runs", "Published — можно выбрать для нового run"),
      ],
    ],
  );
  const changelog = connectionField(
    form,
    t("Changelog", "Что изменено"),
    data.changelog || t("Created a custom draft", "Создан собственный draft"),
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
    action(t("Preview with synthetic input", "Preview с синтетическим input"), async () => {
      const result = await post("/prompt-preview", {
        content: value(),
        data: { synthetic: true, text: "Generated fixture only" },
      });
      preview.textContent = JSON.stringify(result, null, 2);
    }),
  );
  const preview = element("pre", t("Preview not prepared yet.", "Preview ещё не подготовлен."));
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
  box.replaceChildren(element("h2", t("Declarative skill", "Декларативный skill")));
  const form = element("form", "", "stack");
  /** @type {Partial<import('./api.generated').APISkillDefinition>} */
  const data = record?.content || {};
  const name = connectionField(
    form,
    t("Skill name", "Название skill"),
    data.name || t("Additional check", "Дополнительная проверка"),
  );
  const version = connectionField(
    form,
    t("SemVer version", "Версия SemVer"),
    data.version || "1.0.0",
  );
  const description = connectionField(
    form,
    t("Skill purpose", "Назначение skill"),
    data.description || "",
    "textarea",
  );
  const stage = connectionSelect(
    form,
    t("Skill stage", "Стадия skill"),
    data.stage || "P5",
    ["P2", "P3", "P5", "P7", "P8"].map((key) => [key, key]),
  );
  const hook = connectionSelect(form, "Hook", data.hook || "after", [
    ["before", t("Before — before the model step", "Before — перед model-step")],
    ["replace", t("Replace — instead of the model step", "Replace — вместо model-step")],
    ["after", t("After — after the model step", "After — после model-step")],
  ]);
  const order = connectionField(
    form,
    t("Order within hook", "Порядок внутри hook"),
    data.order || 0,
    "number",
  );
  const available = prompts.filter((record) => record.content.status === "published");
  const prompt = connectionSelect(
    form,
    t("Pinned published prompt version", "Закреплённая версия published prompt"),
    data.prompt_ref?.id || "",
    [
      [
        "",
        t("Select a prompt with a compatible contract", "Выберите prompt с подходящим contract"),
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
        "Before/after возвращают недоверенные annotations для последующих model-step и отчёта. Replace обязан соблюдать исходный output contract. Outputs не меняют каталог, настройки или review. allowed_tools всегда пуст.",
      ),
      "notice",
    ),
  );
  const inputSchema = connectionField(
    form,
    "Input JSON Schema",
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
    t("Skill timeout, seconds", "Timeout skill, секунд"),
    data.timeout || 30,
    "number",
  );
  const requests = connectionField(
    form,
    t("Maximum skill requests", "Максимум requests skill"),
    data.budget?.max_requests || 20,
    "number",
  );
  const tokens = connectionField(
    form,
    "Max output tokens skill",
    data.budget?.max_output_tokens || 2048,
    "number",
  );
  const affects = connectionField(
    form,
    t(
      "Affects assessment (must stop on failure)",
      "Влияет на assessment (при ошибке stop обязателен)",
    ),
    data.affects_assessment ?? true,
    "checkbox",
  );
  const failure = connectionSelect(
    form,
    t("Failure policy", "Политика ошибки"),
    data.failure_policy || "stop",
    [
      ["stop", t("Stop — stop analysis", "Stop — остановить анализ")],
      [
        "continue_with_warning",
        t(
          "Continue with warning — reporting skills only",
          "Continue with warning — только отчётный skill",
        ),
      ],
    ],
  );
  const enabled = connectionField(
    form,
    t(
      "Enable this skill version for selection in new runs",
      "Включить эту версию skill для выбора в новых run",
    ),
    data.enabled || false,
    "checkbox",
  );
  configSave(form, pid, "skill", record, () => {
    const selected = available.find((item) => item.id === prompt.value);
    if (!selected) throw new Error(t("Select a published prompt.", "Выберите published prompt."));
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
  const preview = element("pre", t("Select a version to compare.", "Выберите версию для diff."));
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
    element("h2", t("Live synthetic fixture test", "Реальный synthetic fixture test")),
    element(
      "p",
      t(
        "The selected endpoint will receive generated test data only. Success means JSON Schema compliance, not semantic quality.",
        "Выбранный endpoint получит только сгенерированные тестовые данные. Успех означает соответствие JSON Schema, а не semantic quality.",
      ),
      "notice",
    ),
  );
  const model = connectionSelect(
    box,
    t("Test request destination", "Направление тестового запроса"),
    "",
    [
      ["", t("Select an endpoint", "Выберите endpoint")],
      ...connections.map((item) => [item.id, `${item.content.endpoint} · ${item.content.model}`]),
    ],
  );
  const output = element("pre", t("Test has not been run.", "Тест не запускался."));
  box.append(
    action(
      t("Send the displayed synthetic request", "Выполнить показанный synthetic запрос"),
      async () => {
        const selected = connections.find((item) => item.id === model.value);
        if (!selected) throw new Error(t("Select an endpoint.", "Выберите endpoint."));
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
