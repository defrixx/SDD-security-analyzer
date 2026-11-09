import { t } from "./i18n.js";
import { $, api, post, action, element, details, render, projectId } from "./app.js";
import { connectionField, connectionSelect } from "./connections.js";

function logicText(node) {
  if (node === "not_applicable_branch") return t("inactive branch", "неактивная ветвь");
  if (node.predicate)
    return t(
      () => `fact ${node.predicate}`,
      () => `факт ${node.predicate}`,
    );
  if (node.clause)
    return t(
      () => `clause ${node.clause}`,
      () => `часть ${node.clause}`,
    );
  if (node.not)
    return t(
      () => `NOT (${logicText(node.not)})`,
      () => `НЕ (${logicText(node.not)})`,
    );
  if (node.when)
    return t(
      () =>
        `IF (${logicText(node.when)}), THEN (${logicText(node.then)}), ELSE (${logicText(node.else)})`,
      () =>
        `ЕСЛИ (${logicText(node.when)}), ТО (${logicText(node.then)}), ИНАЧЕ (${logicText(node.else)})`,
    );
  if (node.all)
    return t(
      () => `ALL (${node.all.map(logicText).join("; ")})`,
      () => `ВСЕ (${node.all.map(logicText).join("; ")})`,
    );
  if (node.any)
    return t(
      () => `ANY (${node.any.map(logicText).join("; ")})`,
      () => `ХОТЯ БЫ ОДНО (${node.any.map(logicText).join("; ")})`,
    );
  return t("Unknown logic; review required", "Неизвестная логика; требуется проверка");
}

export function requirementDetails(value) {
  details(value.id, "");
  const panel = $("#detail-content");
  panel.replaceChildren(
    element("h2", value.title),
    element("p", t(`${value.id} · version ${value.version} · ${value.status}`, `${value.id} · версия ${value.version} · ${value.status}`)),
    element("p", value.statement),
    element(
      "p",
      `${value.domain} / ${value.subdomain} · source_kind ${value.source_kind} · level ${value.level} · severity ${value.severity}`,
    ),
    element("p", t(`Profile: ${value.source_context.profile}`, `Профиль: ${value.source_context.profile}`)),
    element("h3", t("Applicability", "Применимость")),
    element("p", logicText(value.applicability.expression)),
  );
  for (const predicate of value.applicability.predicates)
    panel.append(element("p", `${predicate.id}: ${predicate.text}`));
  panel.append(
    element("p", t(`Unknown: ${value.applicability.unknown_when}`, `Неизвестно: ${value.applicability.unknown_when}`)),
    element("h3", t("Requirement clauses and their force", "Части требования и их сила")),
    element("p", logicText(value.clause_logic)),
  );
  for (const clause of value.clauses) {
    const norm = value.clause_normativity[clause.id];
    const section = element("section", "", "panel");
    section.append(
      element("h4", `${clause.id} · ${norm.force} · ${norm.nature}`),
      element("p", clause.obligation),
      element(
        "p",
        t(
          () => `Basis: ${norm.basis}`,
          () => `Основание: ${norm.basis}`,
        ),
      ),
      element("p", t(`Qualifiers: ${norm.qualifiers.join("; ")}`, `Условия и исключения: ${norm.qualifiers.join("; ")}`)),
    );
    panel.append(section);
  }
  panel.append(
    element(
      "p",
      t(
        "Force is defined per clause. Context is excluded before ALL/ANY; source_kind does not change force. Accepted required defaults apply without a second numeric opt-in. Implementation: not_verified.",
        "Обязательность задана для каждой части. Контекст исключается до проверки «все / любое»; тип источника не меняет обязательность. Принятые обязательные значения по умолчанию действуют без повторного числового подтверждения. Реализация продукта не проверяется.",
      ),
      "notice",
    ),
  );
  for (const [title, content] of [
    [
      t(
        "Documentary assessment plan · not executed",
        "План документальной оценки · не исполняется",
      ),
      value.verification.specification,
    ],
    [
      t("Source-level evidence plan · not executed", "План проверки исходных материалов · не исполняется"),
      value.verification.source_level,
    ],
    [
      t("Full metadata, relations, and provenance", "Полные метаданные, связи и происхождение"),
      value,
    ],
  ]) {
    const disclosure = element("details");
    disclosure.append(element("summary", title), element("pre", JSON.stringify(content, null, 2)));
    panel.append(disclosure);
  }
  panel.append(element("h3", t("Exact quotations", "Точные цитаты")));
  for (const ref of value.source_refs) {
    const citation = element("details");
    citation.append(
      element(
        "summary",
        t(
          () => `${ref.id} · lines ${ref.line_start}–${ref.line_end}`,
          () => `${ref.id} · строки ${ref.line_start}–${ref.line_end}`,
        ),
      ),
      element("pre", ref.quote),
    );
    panel.append(citation);
  }
  panel.append(
    element(
      "p",
      t(
        () => `Provenance: ${JSON.stringify(value.origin)}`,
        () => `Происхождение: ${JSON.stringify(value.origin)}`,
      ),
    ),
  );
}

export async function domainPanel(pid, panel, isCurrent) {
  const coverage = await api("/builtin-catalog/coverage");
  if (!isCurrent()) return;
  panel.append(
    action(t("Coverage of 26 sources and exclusions", "Покрытие 26 источников и исключения"), () =>
      details(t("Source coverage", "Покрытие источников"), coverage),
    ),
  );
  const group = element("fieldset", "", "stack");
  group.append(
    element("legend", t("Select built-in bundle domains", "Выбрать области встроенного набора")),
  );
  const choices = [];
  for (const domain of coverage.domains) {
    const label = element("label");
    const check = element("input");
    check.type = "checkbox";
    check.value = domain.id;
    label.append(
      check,
      document.createTextNode(
        t(
          () => ` ${domain.id} · ${domain.active_records} active records`,
          () => ` ${domain.id} · ${domain.active_records} активных записей`,
        ),
      ),
    );
    group.append(label);
    choices.push(check);
  }
  group.append(
    action(t("Preview selected domains", "Предпросмотр выбранных областей"), async () => {
      const selected = choices.filter((item) => item.checked).map((item) => item.value);
      if (!selected.length)
        throw new Error(
          t(
            "Select at least one domain or use the entire bundle.",
            "Выберите хотя бы одну область или используйте весь набор.",
          ),
        );
      const preview = await post(`/projects/${pid}/builtin-catalog/preview`, { domains: selected });
      details(
        t(
          "Scope and dependency closure of selected domains",
          "Состав выбранных областей с учётом зависимостей",
        ),
        preview,
      );
      if (!preview.issues.length)
        $("#detail-content").append(
          action(t("Accept this scope and enable", "Принять этот состав и подключить"), async () => {
            await post(`/projects/${pid}/catalog-imports/${preview.id}/commit`, {
              confirmed: true,
              expected_sha256: preview.sha256,
            });
            $("#details").close();
            if (pid === projectId) await render();
          }),
        );
    }),
  );
  panel.append(group);
}

export async function updateDialog(pid, candidate) {
  const catalogs = await api(`/projects/${pid}/catalogs`);
  details(t("Catalog update preview", "Предпросмотр обновления каталога"), "");
  const panel = $("#detail-content");
  panel.append(
    element(
      "p",
      t(
        "Select a previous version from the same series. Preview shows changes to guards, force/nature, clause IDs, and local conflicts. History remains unchanged.",
        "Выберите прежнюю версию той же серии. Предпросмотр показывает изменения условий, обязательности, типов и ID частей требования, а также локальные конфликты. История остаётся неизменной.",
      ),
      "notice",
    ),
  );
  const previous = connectionSelect(panel, t("Previous snapshot", "Предыдущий снимок"), "", [
    ["", t("Select a version", "Выберите версию")],
    ...catalogs
      .filter((item) => item.id !== candidate.candidate_catalog_version_id)
      .map((item) => [
        item.id,
        `${item.catalog_id} · ${item.version} · ${item.enabled ? "active" : "inactive"}`,
      ]),
  ]);
  const mappings = connectionField(
    panel,
    t("Clause mappings JSON · old requirement/clause → targets", "Сопоставление частей JSON: прежнее требование/часть → новые части"),
    "[]",
    "textarea",
  );
  const records = connectionField(
    panel,
    t("Record mappings JSON · old requirement → target requirement IDs", "Сопоставление записей JSON: прежнее требование → ID новых требований"),
    "[]",
    "textarea",
  );
  const retirements = connectionField(
    panel,
    t("Retirements JSON · removed requirement ID → reason", "Удалённые требования JSON: ID → причина"),
    "{}",
    "textarea",
  );
  panel.append(
    element(
      "p",
      t(
        'Clause: [{"old_requirement_id":"ID","old_clause_id":"C1","targets":[{"requirement_id":"ID","clause_id":"C2"}]}]. Record: [{"old_requirement_id":"OLD","target_requirement_ids":["NEW"]}]. Mapping confirms lineage; the author performs semantic review.',
        'Clause: [{"old_requirement_id":"ID","old_clause_id":"C1","targets":[{"requirement_id":"ID","clause_id":"C2"}]}]. Record: [{"old_requirement_id":"OLD","target_requirement_ids":["NEW"]}]. Mapping подтверждает lineage; semantic review выполняет автор.',
      ),
    ),
  );
  const result = element("section", "", "stack");
  panel.append(
    action(t("Check changes and mappings", "Проверить изменения и сопоставления"), async () => {
      if (!previous.value)
        throw new Error(t("Select a previous version.", "Выберите прежнюю версию."));
      const selection = {
        ...candidate,
        previous_catalog_version_id: previous.value,
        clause_mappings: JSON.parse(mappings.value),
        record_mappings: JSON.parse(records.value),
        retirements: JSON.parse(retirements.value),
      };
      const preview = await post(`/projects/${pid}/catalog-updates/preview`, selection);
      result.replaceChildren(element("pre", JSON.stringify(preview, null, 2)));
      const accept = async (activate) => {
        await post(`/projects/${pid}/catalog-updates/commit`, {
          ...selection,
          confirmed: true,
          activate,
          expected_sha256: preview.sha256,
        });
        $("#details").close();
        if (pid === projectId) await render();
      };
      if (!preview.requirement_version_reuse_conflicts.length) {
        result.append(
          action(
            t(
              "Accept preview · install inactive version",
              "Принять предпросмотр и сохранить неактивную версию",
            ),
            () => accept(false),
          ),
        );
        if (preview.can_activate)
          result.append(
            action(
              t(
                "Accept preview · activate and disable previous binding",
                "Принять предпросмотр, подключить новую версию и отключить прежнюю",
              ),
              () => accept(true),
            ),
          );
      }
    }),
    result,
  );
}
