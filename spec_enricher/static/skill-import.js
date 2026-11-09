import { t } from "./i18n.js";

// Deliberately not a YAML loader, Markdown renderer, or package executor.
// Accept only the scalar metadata used by Product-security-skills. Keep the
// entire source intact, and require human adaptation before publication.
export function inspectSkill(text, filename = "SKILL.md") {
  if (filename.toLowerCase() !== "skill.md")
    throw new Error(t("Choose a SKILL.md entry file, not a script or archive.", "Выберите входной SKILL.md, а не скрипт или архив."));
  if (!text.trim() || text.length > 30000 || new TextEncoder().encode(text).length > 100 * 1024 || text.includes("\0"))
    throw new Error(t("SKILL.md must contain text: up to 30,000 characters and 100 KiB.", "SKILL.md должен содержать текст: до 30 000 символов и 100 KiB."));
  const front = text.replace(/^\uFEFF/, "").match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/)?.[1] || "";
  const scalar = (key) => {
    const raw = front.match(new RegExp(`^${key}:\\s*([^\\r\\n]*)$`, "m"))?.[1]?.trim() || "";
    if (/^[>|&*!{[]/.test(raw)) return "";
    return raw.replace(/^(["'])(.*)\1$/, "$2");
  };
  const name = scalar("name");
  const description = scalar("description");
  const links = [...new Set([...text.matchAll(/\]\(([^)\s]+)(?:\s+[^)]*)?\)/g)].map(m => m[1]))];
  const commands = text.split(/\r?\n/).filter(line => /(?:scripts\/|```(?:bash|sh|shell|python|javascript)|allowed-tools:|\b(?:python3?|bash|curl|npm|npx|uv run)\s)/i.test(line)).slice(0, 30);
  return { name: (name || "Imported SKILL.md").slice(0, 200), description, text, links, commands,
    metadataRecognized: !!(name && description),
    variablesUnsupported: text.includes("{{") || text.includes("}}") };
}
