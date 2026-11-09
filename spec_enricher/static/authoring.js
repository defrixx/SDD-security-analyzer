import { t } from "./i18n.js";
import { connectionField, connectionSelect } from "./connections.js";
import { $, projectId, element, setStatus, api, post, action, render } from "./app.js";
import { deleteUserCatalogueVersion } from "./lifecycle.js";

const lineValues = (value) =>
  value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
const idValues = (value) =>
  value
    .split(",")
    .map((line) => line.trim())
    .filter(Boolean);

function rowEditor(parent, title, initial, factory, blank = () => ({})) {
  const section = element("section", "", "panel stack");
  section.append(element("h3", title));
  const rows = [];
  const list = element("div", "", "stack");
  function add(value) {
    const card = element("fieldset", "", "editor-row");
    card.append(element("legend", `${title} · ${rows.length + 1}`));
    const state = { card, get: factory(card, value) };
    rows.push(state);
    card.append(
      action(t(`Remove row: ${title}`, `Удалить строку: ${title}`), () => {
        card.remove();
        section.dispatchEvent(new Event("input", { bubbles: true }));
      }),
    );
    list.append(card);
    section.dispatchEvent(new Event("input", { bubbles: true }));
  }
  section.append(
    list,
    action(t(`Add row: ${title}`, `Добавить строку: ${title}`), () => add(blank())),
  );
  initial.forEach(add);
  parent.append(section);
  return () => rows.filter((row) => row.card.isConnected).map((row) => row.get());
}

function astEditor(parent, title, initial, kind) {
  const section = element("section", "", "logic-editor");
  section.append(element("h3", title));
  const root = element("div");
  const leaf = kind === "condition" ? "predicate" : "clause";
  let value = initial && Object.keys(initial).length ? structuredClone(initial) : { [leaf]: "" };
  function draw() {
    root.replaceChildren();
    let count = 0;
    function node(box, current, replace, type, depth) {
      if (++count > 512 || depth > 16) {
        box.append(
          element(
            "p",
            t(
              "The large expression has been preserved unchanged. Use the explicit reset to simplify it; validation will report errors.",
              "Большое выражение сохранено без изменений. Для упрощения используйте явный сброс; валидация покажет ошибки.",
            ),
            "notice",
          ),
        );
        return;
      }
      const key = Object.keys(current)[0] || (type === "condition" ? "predicate" : "clause");
      const panel = element("div", "", "logic-node stack");
      const options =
        type === "condition"
          ? [
              ["predicate", t("Condition (ID)", "Условие (ID)")],
              ["all", t("ALL — all", "ALL — все")],
              ["any", t("ANY — any", "ANY — любое")],
              ["not", t("NOT — negation", "NOT — отрицание")],
            ]
          : [
              ["clause", "Clause (ID)"],
              ["all", t("ALL — all clauses", "ALL — все части требования")],
              ["any", t("ANY — alternative", "ANY — альтернатива")],
              ["when", "WHEN / THEN / ELSE"],
            ];
      const select = connectionSelect(
        panel,
        t(
          `${type === "condition" ? "Applicability" : "Clause"} operator`,
          `Оператор ${type === "condition" ? "применимости" : "частей требования"}`,
        ),
        key,
        options,
      );
      const base = () => ({ [type === "condition" ? "predicate" : "clause"]: "" });
      select.addEventListener("change", () => {
        const selected = select.value;
        const next = ["predicate", "clause"].includes(selected)
          ? { [selected]: "" }
          : selected === "when"
            ? {
                when: { predicate: "" },
                // biome-ignore lint/suspicious/noThenProperty: Schema 4.0 WHEN branch is inert data, not a Promise.
                then: { clause: "" },
                else: "not_applicable_branch",
              }
            : selected === "not"
              ? { not: base() }
              : { [selected]: [base()] };
        replace(next);
        draw();
        root.querySelector("select")?.focus();
      });
      if (key === "predicate" || key === "clause") {
        const input = connectionField(
          panel,
          key === "predicate"
            ? t("Architectural condition ID", "ID архитектурного условия")
            : t("ID clause", "ID части требования"),
          current[key] || "",
        );
        input.addEventListener("input", () => {
          current[key] = input.value;
        });
      } else if (key === "all" || key === "any") {
        const children = Array.isArray(current[key]) ? current[key] : [];
        children.forEach((child, index) => {
          const part = element("div");
          node(
            part,
            child,
            (next) => {
              children[index] = next;
            },
            type,
            depth + 1,
          );
          part.append(
            action(t(`Remove branch ${index + 1}`, `Удалить ветвь ${index + 1}`), () => {
              children.splice(index, 1);
              draw();
            }),
          );
          panel.append(part);
        });
        panel.append(
          action(
            t(`Add ${key.toUpperCase()} branch`, `Добавить ветвь ${key.toUpperCase()}`),
            () => {
              children.push(base());
              current[key] = children;
              draw();
            },
          ),
        );
      } else if (key === "not") {
        node(
          panel,
          current.not || { predicate: "" },
          (next) => {
            current.not = next;
          },
          "condition",
          depth + 1,
        );
      } else if (key === "when") {
        panel.append(element("h4", t("WHEN — condition", "ЕСЛИ — условие")));
        node(
          panel,
          current.when,
          (next) => {
            current.when = next;
          },
          "condition",
          depth + 1,
        );
        panel.append(element("h4", t("THEN", "ТО")));
        node(
          panel,
          current.then,
          (next) => {
            // biome-ignore lint/suspicious/noThenProperty: Preserve the schema-defined declarative THEN property.
            current.then = next;
          },
          "clause",
          depth + 1,
        );
        const alternate = connectionSelect(
          panel,
          "ELSE",
          current.else === "not_applicable_branch" ? "skip" : "branch",
          [
            ["skip", t("Inactive branch", "Неактивная ветвь")],
            ["branch", t("Alternative clause logic", "Альтернативная логика частей требования")],
          ],
        );
        alternate.addEventListener("change", () => {
          current.else = alternate.value === "skip" ? "not_applicable_branch" : { clause: "" };
          draw();
        });
        if (current.else !== "not_applicable_branch")
          node(
            panel,
            current.else,
            (next) => {
              current.else = next;
            },
            "clause",
            depth + 1,
          );
      }
      box.append(panel);
    }
    node(
      root,
      value,
      (next) => {
        value = next;
      },
      kind,
      0,
    );
    section.dispatchEvent(new Event("input", { bubbles: true }));
  }
  section.append(
    root,
    action(t(`Reset expression: ${title}`, `Сбросить выражение: ${title}`), () => {
      value = { [leaf]: "" };
      draw();
    }),
  );
  parent.append(section);
  draw();
  return () => structuredClone(value);
}

function planEditor(parent, name, original, sourceLevel) {
  const section = element("section", "", "panel stack");
  section.append(element("h3", name));
  const data = original || {};
  const inputs = connectionField(
    section,
    t(`${name}: inputs (one per line)`, `${name}: входы (по одному на строку)`),
    (data.inputs || []).join("\n"),
    "textarea",
  );
  const steps = rowEditor(
    section,
    t(`${name}: steps`, `${name}: шаги`),
    data.steps?.length ? data.steps : [{ id: "s1" }, { id: "s2" }],
    (box, row) => {
      const id = connectionField(box, t("Step ID", "ID шага"), row.id || "");
      const actionText = connectionField(
        box,
        t(
          "Verification action (plan only, not a command)",
          "Действие проверки (только план, не команда)",
        ),
        row.action || "",
        "textarea",
      );
      const ids = connectionField(
        box,
        t("Step clause IDs, comma-separated", "ID частей требования для шага, через запятую"),
        (row.clause_ids || []).join(", "),
      );
      const expected = connectionField(
        box,
        t("Expected step evidence", "Ожидаемое свидетельство шага"),
        row.expected_evidence || "",
        "textarea",
      );
      const guard = connectionField(
        box,
        t("Step applies when", "Шаг действует при условии"),
        Boolean(row.when),
        "checkbox",
      );
      const when = astEditor(
        box,
        t("Step guard", "Условие выполнения шага"),
        row.when || { predicate: "" },
        "condition",
      );
      return () => ({
        id: id.value,
        action: actionText.value,
        clause_ids: idValues(ids.value),
        expected_evidence: expected.value,
        ...(guard.checked ? { when: when() } : {}),
      });
    },
  );
  const conditional = rowEditor(
    section,
    t(`${name}: conditional inputs`, `${name}: условные входы`),
    data.conditional_inputs || [],
    (box, row) => {
      const ids = connectionField(
        box,
        t("Conditional input clause IDs", "ID частей требования для условного входа"),
        (row.clause_ids || []).join(", "),
      );
      const values = connectionField(
        box,
        t("Conditional inputs (one per line)", "Условные входы (по одному на строку)"),
        (row.inputs || []).join("\n"),
        "textarea",
      );
      const when = astEditor(
        box,
        t("Input guard", "Условие использования входных данных"),
        row.when || { predicate: "" },
        "condition",
      );
      return () => ({
        clause_ids: idValues(ids.value),
        inputs: lineValues(values.value),
        when: when(),
      });
    },
  );
  const outcomes = new Map();
  for (const key of sourceLevel
    ? ["pass_when", "fail_when", "unknown_when"]
    : ["satisfied_by_spec", "contradicted_by_spec", "not_addressed", "unknown"])
    outcomes.set(
      key,
      connectionField(
        section,
        `${name}: ${key}`,
        (sourceLevel ? data : data.outcome_conditions || {})[key] || "",
        "textarea",
      ),
    );
  parent.append(section);
  return () => ({
    inputs: lineValues(inputs.value),
    steps: steps(),
    conditional_inputs: conditional(),
    ...(sourceLevel
      ? Object.fromEntries(Array.from(outcomes, ([key, input]) => [key, input.value]))
      : {
          outcome_conditions: Object.fromEntries(
            Array.from(outcomes, ([key, input]) => [key, input.value]),
          ),
        }),
  });
}

/** @param {string} pid
 * @param {RequirementDraft | null} [existing]
 * @param {RequirementFields | null} [initialFields]
 */
async function editRequirement(pid, existing = null, initialFields = null) {
  const [initial, catalogues, rankings] = await Promise.all([
    api("/requirement-form/defaults"),
    api("/user-catalogues"),
    api(`/projects/${pid}/requirement-rankings`),
  ]);
  /** @type {RequirementFields} */
  const fields = structuredClone(existing?.fields || initialFields || initial.fields);
  let draft = existing;
  const box = $("#detail-content");
  box.replaceChildren(
    element(
      "h2",
      t(
        "Custom requirement · draft → preview → new version",
        "Собственное требование: черновик → предпросмотр → новая версия",
      ),
    ),
  );
  const form = element("form", "", "stack");
  form.addEventListener("submit", (event) => event.preventDefault());
  const scope = connectionSelect(
    form,
    t("Save destination", "Куда сохранить"),
    existing?.scope === "user_catalogue" ? existing.user_catalogue_id : "project",
    [
      ["project", t("Selected product only", "Только выбранный продукт")],
      ...catalogues.map((item) => [
        item.id,
        t(`User catalog: ${item.name}`, `Пользовательский каталог: ${item.name}`),
      ]),
    ],
  );
  scope.disabled = Boolean(existing);
  form.append(
    element(
      "p",
      t(
        "The shared user catalog contains authored policy text only. Product documentation, evidence, secrets, and grants are not copied. Binding to another project is a separate action.",
        "Общий пользовательский каталог содержит только авторский текст требований. Документация, доказательства, секреты и разрешения продукта не копируются. Подключение к другому продукту выполняется отдельно.",
      ),
      "notice",
    ),
  );
  const id = connectionField(
    form,
    t("Stable requirement ID", "Стабильный ID требования"),
    fields.id,
  );
  id.readOnly = Boolean(existing);
  const version = connectionField(
    form,
    t("New requirement version", "Новая версия требования"),
    fields.version,
  );
  const title = connectionField(form, t("Requirement title", "Название требования"), fields.title);
  const statement = connectionField(
    form,
    t("Requirement statement", "Формулировка требования"),
    fields.statement,
    "textarea",
  );
  statement.rows = 5;
  const domain = connectionField(form, t("Domain", "Домен"), fields.domain);
  const subdomain = connectionField(form, t("Subdomain", "Поддомен"), fields.subdomain);
  const tags = connectionField(
    form,
    t("Tags, comma-separated", "Метки через запятую"),
    (fields.tags || []).join(", "),
  );
  const author = connectionField(
    form,
    t("Policy author", "Автор собственной политики"),
    fields.origin && "author" in fields.origin ? fields.origin.author : "",
  );
  const license = connectionField(
    form,
    t(
      "Custom license, if any (blank: no license assigned)",
      "Собственная лицензия, если задана (пусто: не назначать)",
    ),
    fields.origin?.license || "",
  );
  const kind = connectionSelect(
    form,
    t("Descriptive source_kind", "Тип источника (описательный)"),
    fields.source_kind || "mandatory",
    ["mandatory", "recommended", "review_criterion", "local_assumption"].map((key) => [key, key]),
  );
  const level = connectionSelect(
    form,
    t("Descriptive record evidence level", "Уровень подтверждения записи (описательный)"),
    fields.level || "specification",
    ["specification", "implementation", "process"].map((key) => [key, key]),
  );
  const severity = connectionSelect(
    form,
    t("Source policy severity", "Критичность по исходной политике"),
    fields.severity || "unrated",
    ["unrated", "critical", "high", "medium", "low"].map((key) => [key, key]),
  );
  const currentRanking = rankings.find(
    (row) => row.requirement_id === fields.id && row.version === fields.version,
  );
  const ranking = connectionSelect(
    form,
    t(
      "Project importance (separate from upstream severity)",
      "Важность для продукта (отдельно от критичности исходной политики)",
    ),
    currentRanking?.ranking || "unranked",
    ["unranked", "critical", "high", "normal", "low"].map((key) => [key, key]),
  );
  const rankingReason = connectionField(
    form,
    t("Reason for project importance", "Причина проектной важности"),
    currentRanking?.reason || "",
    "textarea",
  );
  const rankingScope = () => {
    ranking.disabled = scope.value !== "project";
    rankingReason.disabled = scope.value !== "project";
  };
  scope.addEventListener("change", rankingScope);
  rankingScope();

  form.append(
    element(
      "p",
      t(
        "Force and nature are set per clause. Source kind and record level do not assign normative force. Runtime implementation is always not_verified.",
        "Обязательность и тип задаются для каждой части требования отдельно. Тип источника и уровень записи не определяют обязательность. Реализация продукта не проверяется.",
      ),
      "notice",
    ),
  );
  const unknown = connectionField(
    form,
    t(
      "I have not yet defined the architectural applicability condition",
      "Я ещё не определил архитектурное условие применимости",
    ),
    Boolean(fields.policy_scope?.authoring_unknown_predicate_ids?.length),
    "checkbox",
  );
  const predicates = rowEditor(
    form,
    t("Architectural conditions", "Архитектурные условия"),
    fields.applicability?.predicates || [],
    (card, item) => {
      const key = connectionField(card, t("Condition ID", "ID условия"), item.id || "");
      const text = connectionField(
        card,
        t("Architectural fact description", "Описание архитектурного факта"),
        item.text || "",
        "textarea",
      );
      const evidence = connectionField(
        card,
        t(
          "How to establish this fact (one per line)",
          "Как установить этот факт (по одному на строку)",
        ),
        (item.evidence || []).join("\n"),
        "textarea",
      );
      return () => ({
        ...item,
        id: key.value,
        text: text.value,
        evidence: lineValues(evidence.value),
      });
    },
    () => ({ id: `p${crypto.randomUUID().slice(0, 8)}` }),
  );
  const applicability = astEditor(
    form,
    t("Applicability logic — separate AST", "Логика применимости — отдельное дерево условий"),
    fields.applicability?.expression,
    "condition",
  );
  const unknownWhen = connectionField(
    form,
    t(
      "When the fact is unknown / question for the user",
      "Когда факт неизвестен / вопрос пользователю",
    ),
    fields.applicability?.unknown_when || "",
    "textarea",
  );
  const clauses = rowEditor(
    form,
    t("Clauses and normative force", "Части требования и их обязательность"),
    fields.clauses?.length ? fields.clauses : [{ id: "c1" }],
    (card, item) => {
      const norm = fields.clause_normativity?.[item.id] || {};
      const key = connectionField(card, t("ID clause", "ID части требования"), item.id || "");
      const text = connectionField(
        card,
        t("Clause obligation", "Обязанность части требования"),
        item.obligation || "",
        "textarea",
      );
      const force = connectionSelect(
        card,
        t("Force clause", "Обязательность части"),
        norm.force || "undetermined",
        ["required", "recommended", "not_a_requirement", "undetermined"].map((key) => [key, key]),
      );
      const nature = connectionSelect(card, t("Nature clause", "Тип части требования"), norm.nature || "", [
        ["", t("Select nature", "Выберите тип")],
        ...[
          "technical_control",
          "review_obligation",
          "process_obligation",
          "local_policy_parameter",
          "context",
        ].map((key) => [key, key]),
      ]);
      const basis = connectionField(
        card,
        t("Basis for clause force", "Основание обязательности"),
        norm.basis || "",
        "textarea",
      );
      const qualifiers = connectionField(
        card,
        t(
          "Clause conditions and exceptions (one per line)",
          "Условия и исключения части требования (по одному на строку)",
        ),
        (norm.qualifiers || []).join("\n"),
        "textarea",
      );
      const evidenceLevel = connectionSelect(
        card,
        t("Evidence level clause", "Уровень подтверждения"),
        norm.evidence_level || "specification",
        ["specification", "implementation", "process"].map((key) => [key, key]),
      );
      return () => ({
        clause: { ...item, id: key.value, obligation: text.value },
        norm: {
          ...norm,
          force: force.value,
          nature: nature.value,
          basis: basis.value,
          qualifiers: lineValues(qualifiers.value),
          evidence_level: evidenceLevel.value,
        },
      });
    },
    () => ({ id: `c${crypto.randomUUID().slice(0, 8)}` }),
  );
  const logic = astEditor(
    form,
    t("Clause logic — ALL / ANY / WHEN / ELSE", "Логика частей требования: все / любое / если / иначе"),
    fields.clause_logic || { clause: "c1" },
    "clause",
  );
  const spec = planEditor(
    form,
    t("Specification assessment", "Проверка по спецификации"),
    fields.verification?.specification,
    false,
  );
  const implementation = planEditor(
    form,
    t("Implementation / process evidence plan", "План подтверждения реализации или процесса"),
    fields.verification?.source_level,
    true,
  );
  const unresolved = connectionField(
    form,
    t(
      "Incomplete structural fields (one per line)",
      "Незаполненные структурные поля (по одному на строку)",
    ),
    (existing?.unresolved_fields || []).join("\n"),
    "textarea",
  );
  const questions = connectionField(
    form,
    t("Open semantic questions (one per line)", "Открытые вопросы по смыслу (по одному на строку)"),
    (existing?.questions || []).join("\n"),
    "textarea",
  );
  const provenance = element("details");
  provenance.append(
    element(
      "summary",
      t("Preserved provenance and references", "Сохраняемое происхождение и ссылки"),
    ),
    element(
      "pre",
      JSON.stringify(
        {
          origin: fields.origin,
          source_refs: fields.source_refs,
          base_requirement_ref: existing?.base_requirement_ref || null,
        },
        null,
        2,
      ),
    ),
  );
  form.append(provenance);
  function value() {
    const parts = clauses();
    const policy = { ...(fields.policy_scope || {}) };
    let facts = predicates();
    let expression = applicability();
    let question = unknownWhen.value;
    if (unknown.checked) {
      facts = [
        {
          id: "authoring-unknown",
          text: t(
            "The architectural condition has not yet been defined by the user",
            "Архитектурное условие пользователем ещё не определено",
          ),
          evidence: [
            t(
              "The policy author must explicitly define the condition",
              "Требуется явное определение условия автором политики",
            ),
          ],
        },
      ];
      expression = { predicate: "authoring-unknown" };
      policy.authoring_unknown_predicate_ids = ["authoring-unknown"];
      question ||= t(
        "Which architectural condition determines whether this requirement applies?",
        "Какое архитектурное условие определяет применимость этого требования?",
      );
    } else delete policy.authoring_unknown_predicate_ids;
    return {
      ...fields,
      id: id.value,
      version: version.value,
      title: title.value,
      statement: statement.value,
      domain: domain.value,
      subdomain: subdomain.value,
      tags: idValues(tags.value),
      source_kind: kind.value,
      level: level.value,
      severity: severity.value,
      origin: { ...fields.origin, author: author.value, license: license.value || null },
      applicability: { predicates: facts, expression, unknown_when: question },
      clauses: parts.map((part) => part.clause),
      clause_normativity: Object.fromEntries(parts.map((part) => [part.clause.id, part.norm])),
      clause_logic: logic(),
      verification: {
        ...fields.verification,
        specification: spec(),
        source_level: implementation(),
      },
      policy_scope: policy,
    };
  }
  const preview = element("section", "", "panel stack");
  /** @type {HTMLButtonElement | null} */
  let commitButton = null;
  async function saveDraft() {
    const selectedScope = scope.value === "project" ? "project" : "user_catalogue";
    const owner = selectedScope === "project" ? pid : scope.value;
    const path = `/requirement-drafts/${selectedScope}/${owner}${draft ? `/${draft.draft_id}` : ""}`;
    /** @type {RequirementDraft} */
    const saved = await post(
      path,
      {
        fields: value(),
        base_requirement_ref:
          existing?.base_requirement_ref || fields.policy_scope?.override_of || null,
        expected_revision: draft?.revision || null,
        unresolved_fields: lineValues(unresolved.value),
        questions: lineValues(questions.value),
      },
      draft ? "PUT" : "POST",
    );
    draft = saved;
    id.readOnly = true;
    scope.disabled = true;
    setStatus(
      t(`Draft saved · revision ${draft.revision}`, `Черновик сохранён · версия ${draft.revision}`),
    );
    return draft;
  }
  async function showPreview() {
    const reviewedDraft = await saveDraft();
    const owner = reviewedDraft.project_id || reviewedDraft.user_catalogue_id;
    const result = await api(
      `/requirement-drafts/${reviewedDraft.scope}/${owner}/${reviewedDraft.draft_id}/preview`,
    );
    preview.replaceChildren(
      element("h3", t("Preview / structural validation", "Предпросмотр и проверка структуры")),
      element(
        "p",
        t(`Scope: ${reviewedDraft.scope}. Semantic review: not_performed; implementation: not_verified.`, `Область действия: ${reviewedDraft.scope}. Смысловая проверка не проведена; реализация не проверена.`),
      ),
      element("pre", JSON.stringify(result, null, 2)),
    );
    const confirmed = connectionField(
      preview,
      t(
        "I accept the displayed scope, authorship, conditions, and normative force",
        "Принимаю показанную область действия, авторство, условия и обязательность",
      ),
      false,
      "checkbox",
    );
    const activate = connectionField(
      preview,
      t(
        "Bind this new version to the selected product only",
        "Подключить новую версию только к выбранному продукту",
      ),
      false,
      "checkbox",
    );
    activate.disabled = reviewedDraft.scope !== "project";
    const replace = connectionField(
      preview,
      t(
        "Explicitly disable the previous binding of this custom requirement when enabling the new one",
        "Отключить предыдущую версию этого требования при включении новой",
      ),
      false,
      "checkbox",
    );
    replace.disabled = reviewedDraft.scope !== "project";
    commitButton = action(
      t(
        "Save a new requirement version from preview",
        "Сохранить новую версию требования из предпросмотра",
      ),
      async () => {
        if (!confirmed.checked)
          throw new Error(
            t("Accept the displayed scope and content.", "Примите показанную область действия и содержание."),
          );
        if (
          reviewedDraft.scope === "project" &&
          ranking.value !== "unranked" &&
          !rankingReason.value.trim()
        )
          throw new Error(
            t("Provide a reason for project importance.", "Укажите причину проектной важности."),
          );
        await post(
          `/requirement-drafts/${reviewedDraft.scope}/${owner}/${reviewedDraft.draft_id}/commit`,
          {
            expected_revision: reviewedDraft.revision,
            expected_sha256: result.sha256,
            confirmed: true,
            activate: activate.checked,
            replace_previous_bindings: replace.checked,
          },
        );
        if (reviewedDraft.scope === "project" && ranking.value !== "unranked") {
          const previous = (await api(`/projects/${pid}/requirement-rankings`)).find(
            (row) => row.requirement_id === id.value && row.version === version.value,
          );
          await post(
            `/projects/${pid}/requirement-ranking`,
            {
              requirement_id: id.value,
              version: version.value,
              expected_revision: previous?.revision || 0,
              ranking: ranking.value,
              reason: rankingReason.value,
            },
            "PUT",
          );
        }

        $("#details").close();
        if (projectId === pid) await render();
      },
    );
    commitButton.disabled = Boolean(
      result.issues.length || !result.sha256 || reviewedDraft.unresolved_fields.length,
    );
    preview.append(commitButton);
    preview.scrollIntoView({ block: "start" });
  }
  form.addEventListener("input", () => {
    if (commitButton) commitButton.disabled = true;
  });
  form.append(
    action(t("Save draft", "Сохранить черновик"), saveDraft),
    action(t("Preview", "Предпросмотр"), showPreview),
    action(t("Validate structure", "Проверить структуру"), showPreview),
  );
  box.append(form, preview);
  if (!$("#details").open) $("#details").showModal();
}

async function authoringPanel(pid, content, isCurrent = () => projectId === pid) {
  const [drafts, catalogues] = await Promise.all([
    api(`/requirement-drafts/project/${pid}`),
    api("/user-catalogues"),
  ]);
  if (!isCurrent()) return;
  const panel = element("section", "", "panel stack");
  panel.append(
    element("h3", t("Custom requirements", "Собственные требования")),
    action(t("Add requirement", "Добавить требование"), () => editRequirement(pid)),
  );
  const links = element("div", "", "actions");
  for (const kind of ["blank", "example"])
    for (const format of ["json", "yaml"]) {
      const link = element(
        "a",
        t(
          `${kind === "blank" ? "Draft template" : "Synthetic example"} ${format.toUpperCase()}`,
          `${kind === "blank" ? "Шаблон черновика" : "Синтетический пример"} ${format.toUpperCase()}`,
        ),
      );
      link.href = `/api/v1/requirement-templates/${kind}/${format}`;
      link.download = "";
      links.append(link);
    }
  panel.append(
    element(
      "p",
      t(
        "The template is incomplete; the example is synthetic. Downloading does not enable the policy.",
        "Шаблон неполный; пример синтетический. Скачивание не подключает требования.",
      ),
      "notice",
    ),
    links,
  );
  for (const draft of drafts)
    panel.append(
      action(t(`Draft: ${draft.fields.title || draft.fields.id} · r${draft.revision}`, `Черновик: ${draft.fields.title || draft.fields.id} · r${draft.revision}`), () =>
        editRequirement(pid, draft),
      ),
    );
  panel.append(
    action(t("Create user catalog", "Создать пользовательский каталог"), () => {
      const box = $("#detail-content");
      box.replaceChildren(
        element("h2", t("New shared user catalog", "Новый общий пользовательский каталог")),
      );
      const name = connectionField(
        box,
        t("User catalog name", "Название пользовательского каталога"),
        "",
      );
      box.append(
        action(t("Save user catalog", "Сохранить пользовательский каталог"), async () => {
          await post("/user-catalogues", { name: name.value });
          $("#details").close();
          if (projectId === pid) await render();
        }),
      );
      if (!$("#details").open) $("#details").showModal();
    }),
  );
  for (const item of catalogues)
    panel.append(
      action(
        t(`User catalog: ${item.name}`, `Пользовательский каталог: ${item.name}`),
        async () => {
          const [versions, userDrafts] = await Promise.all([
            api(`/user-catalogues/${item.id}/versions`),
            api(`/requirement-drafts/user_catalogue/${item.id}`),
          ]);
          const box = $("#detail-content");
          box.replaceChildren(
            element("h2", item.name),
            element(
              "p",
              t(
                "Bindings pin a version; new versions do not automatically update other projects.",
                "Подключения закреплены за версией; новые версии не обновляют другие продукты автоматически.",
              ),
              "notice",
            ),
          );
          for (const draft of userDrafts)
            box.append(
              action(
                t(
                  `Edit draft ${draft.fields.title || draft.fields.id}`,
                  `Редактировать черновик ${draft.fields.title || draft.fields.id}`,
                ),
                () => editRequirement(pid, draft),
              ),
            );
          for (const version of versions)
            box.append(
              element("h3", t(`Version ${version.revision}`, `Версия ${version.revision}`)),
              element("pre", JSON.stringify(version, null, 2)),
              action(
                t(
                  `Explicitly bind displayed version ${version.revision} to the current product`,
                  `Подключить показанную версию ${version.revision} к текущему продукту`,
                ),
                async () => {
                  await post(`/projects/${pid}/user-catalogue-bindings`, {
                    user_catalogue_id: item.id,
                    revision: version.revision,
                    expected_sha256: version.sha256,
                    confirmed: true,
                  });
                  $("#details").close();
                  if (projectId === pid) await render();
                },
              ),
              action(
                t(
                  `Remove version ${version.revision} from instance registry · preview`,
                  `Удалить версию ${version.revision} из реестра установки · предпросмотр`,
                ),
                () => deleteUserCatalogueVersion(item.id, version.revision),
              ),
            );
          if (!$("#details").open) $("#details").showModal();
        },
      ),
    );
  content.append(panel);
}

export { lineValues, idValues, rowEditor, astEditor, planEditor, editRequirement, authoringPanel };
