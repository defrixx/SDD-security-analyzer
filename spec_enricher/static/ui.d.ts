/** Static controls are owned by index.html. $() checks presence before use. */
interface UIElements {
  "#content": HTMLDivElement;
  "#detail-content": HTMLDivElement;
  "#details": HTMLDialogElement;
  "#details-close": HTMLButtonElement;
  "#login": HTMLDialogElement;
  "#login-error": HTMLParagraphElement;
  "#login-form": HTMLFormElement;
  "#login-token": HTMLInputElement;
  "#main": HTMLElement;
  "#new-project": HTMLButtonElement;
  "#page-error": HTMLParagraphElement;
  "#project": HTMLSelectElement;
  "#project-cancel": HTMLButtonElement;
  "#project-description": HTMLTextAreaElement;
  "#project-dialog": HTMLDialogElement;
  "#project-error": HTMLParagraphElement;
  "#project-form": HTMLFormElement;
  "#project-name": HTMLInputElement;
  "#status": HTMLSpanElement;
}

interface RegistryRecord<T> {
  id: string;
  revision: number;
  sha256: string;
  content: T;
  capability_report?: { result: Record<string, unknown> } | null;
  granted_scopes?: string[];
}

type DeepPartial<T> =
  T extends Array<infer Item>
    ? Array<DeepPartial<Item>>
    : T extends object
      ? { [Key in keyof T]?: DeepPartial<T[Key]> }
      : T;

type RequirementFields = Omit<
  DeepPartial<import("./api.generated").APICatalogRequirement>,
  "policy_scope"
> & {
  policy_scope?: {
    authoring_unknown_predicate_ids?: string[];
    override_of?: Record<string, unknown>;
    [key: string]: unknown;
  };
};

interface RequirementDraft {
  draft_id: string;
  revision: number;
  scope: "project" | "user_catalogue";
  project_id: string | null;
  user_catalogue_id: string | null;
  base_requirement_ref: Record<string, unknown> | null;
  fields: RequirementFields;
  unresolved_fields: string[];
  questions: string[];
  created_at: string;
}

interface PipelineDefaults {
  prompts: Record<string, { id: string; version: string; template: string; hash: string }>;
  contracts: Record<string, Record<string, unknown>>;
  allowed_variables: string[];
  data_transport: string;
  skills: string;
}
