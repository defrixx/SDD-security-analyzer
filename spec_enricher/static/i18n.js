// Only application-authored messages belong here or in t(en, ru) calls.
// Never translate document, catalog, prompt, model, or user-supplied content.
const LANGUAGE_KEY = "sdd-ui-language";

/** @returns {"en" | "ru"} */
function readLanguage() {
  try {
    return localStorage.getItem(LANGUAGE_KEY) === "ru" ? "ru" : "en";
  } catch {
    return "en";
  }
}
const language = readLanguage();

/** @returns {"en" | "ru"} */
export function getLanguage() {
  return language;
}

/** Template callbacks avoid evaluating the unused translation or its expressions.
 * @param {string | (() => string)} en
 * @param {string | (() => string)} ru
 * @returns {string}
 */
export function t(en, ru) {
  const message = language === "ru" ? ru : en;
  return typeof message === "function" ? message() : message;
}

/** Initialize only explicitly marked, application-owned shell messages. */
export function initializeLanguage() {
  document.documentElement.lang = language;
  for (const node of document.querySelectorAll("[data-i18n-ru]")) {
    if (node instanceof HTMLElement && language === "ru") {
      node.textContent = node.dataset.i18nRu || "";
    }
  }
  for (const node of document.querySelectorAll("[data-i18n-aria-ru]")) {
    if (node instanceof HTMLElement && language === "ru") {
      node.setAttribute("aria-label", node.dataset.i18nAriaRu || "");
    }
  }

  // Do not silently discard edits when reloading into another language.
  // Values remain only in memory, never in browser storage (including tokens).
  /** @type {Map<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, string>} */
  const edited = new Map();
  /** @param {HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement} input */
  const fieldValue = (input) =>
    input instanceof HTMLInputElement && ["checkbox", "radio"].includes(input.type)
      ? String(input.checked)
      : input.value;
  const isField = (node) =>
    node instanceof HTMLInputElement ||
    node instanceof HTMLTextAreaElement ||
    node instanceof HTMLSelectElement;
  document.addEventListener("focusin", (event) => {
    const node = event.target;
    if (isField(node) && !node.matches("[data-language], #project") && !edited.has(node)) {
      edited.set(node, fieldValue(node));
    }
  });
  // File chooser and assistive input can produce change without prior focus.
  document.addEventListener("input", (event) => {
    const node = event.target;
    if (isField(node) && !node.matches("[data-language], #project") && !edited.has(node)) {
      edited.set(node, "");
    }
  });
  for (const select of document.querySelectorAll("select[data-language]")) {
    if (!(select instanceof HTMLSelectElement)) continue;
    select.value = language;
    select.addEventListener("change", () => {
      const next = select.value;
      if (next === language || !["en", "ru"].includes(next)) return;
      const dirty = [...edited].some(
        ([node, initial]) => node.isConnected && fieldValue(node) !== initial,
      );
      if (
        dirty &&
        !window.confirm(
          t(
            "Changing language reloads the page and discards unsaved edits. Continue?",
            "Смена языка перезагрузит страницу и удалит несохранённые изменения. Продолжить?",
          ),
        )
      ) {
        select.value = language;
        return;
      }
      try {
        localStorage.setItem(LANGUAGE_KEY, next);
      } catch {
        select.value = language;
        window.alert(
          t(
            "Browser storage is unavailable. Allow local storage to save your language preference.",
            "Хранилище браузера недоступно. Разрешите локальное хранилище для сохранения языка.",
          ),
        );
        return;
      }
      // A reload also refreshes open dialogs and future asynchronous messages.
      // It performs no project writes and does not cancel backend runs.
      location.reload();
    });
  }
}
