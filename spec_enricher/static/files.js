import { t } from "./i18n.js";

/** Combine uploaded change documents without losing their names or contents.
 * Existing source files are not changed. The server enforces the same byte limit.
 * @param {File[]} files
 * @returns {Promise<File>}
 */
export async function combineSpecification(files) {
  if (!files.length || files.length > 20)
    throw new Error(t("Select between 1 and 20 files.", "Выберите от 1 до 20 файлов."));
  if (files.some((file) => !file.name.toLowerCase().endsWith(".md")))
    throw new Error(t("Choose Markdown (.md) files for a combined specification.", "Для общей спецификации выберите Markdown-файлы (.md)."));
  if (files.reduce((total, file) => total + file.size, 0) > 100 * 1024)
    throw new Error(t("The combined specification must fit within 100 KiB.", "Общая спецификация должна укладываться в 100 KiB."));
  const parts = await Promise.all(files.map(async (file) => `## File: ${file.name}\n\n${await file.text()}`));
  const merged = new File([parts.join("\n\n---\n\n")], "specification-bundle.md", { type: "text/markdown" });
  if (merged.size > 100 * 1024)
    throw new Error(t("The combined specification including file headings exceeds 100 KiB.", "Общая спецификация с заголовками файлов превышает 100 KiB."));
  return merged;
}
