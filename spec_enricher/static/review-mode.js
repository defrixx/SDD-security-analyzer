// Only presentation/request composition. Persisted document roles are unchanged.
export function analysisMode(preset = {}) {
  return preset.mode || (preset.delta_document_id ? "enrich_changes" : "review_documents");
}
export function documentReviewRequest(documentIds, catalogIds, connection, limits, stepMode, factOverrides = []) {
  if (!documentIds.length || !catalogIds.length || !connection) return null;
  return {
    mode: "review_documents",
    review_document_ids: [...new Set(documentIds)],
    catalog_ids: [...new Set(catalogIds)],
    connection: { id: connection.id, revision: connection.revision },
    limits,
    step_mode: Boolean(stepMode),
    fact_overrides: factOverrides.map(({ requirement_id, predicate_id, side, value, reason }) => ({ requirement_id, predicate_id, side, value, reason })),
  };
}

// Keeps the original group/control objects, so paging never resets typed answers.
export function scopePage(groups, query = "", page = 0, size = 10) {
  const filtered = groups.filter((group) => group.requirement_id.toLowerCase().includes(query.trim().toLowerCase()));
  const pages = Math.max(1, Math.ceil(filtered.length / size));
  const index = Math.max(0, Math.min(page, pages - 1));
  return { items: filtered.slice(index * size, (index + 1) * size), page: index, pages, total: filtered.length };
}

export function nonScopeQuestions(report) {
  const scopeQuestions = new Set((report.scope_confirmations || []).map((scope) => scope.question).filter(Boolean));
  return (report.questions || []).filter((question) => !scopeQuestions.has(question));
}
