# SDD Security Analyzer

A local app for reviewing software design documents against a pinned security catalog. Upload multiple specification files, select requirements, check them together, and download a separate report. No existing baseline or change document is required. Originals are not overwritten.

**This reviews documentation, not the security of a running system. Model output needs human review.**

## Start

Install and start Docker Desktop with Linux containers. No host Python, Node.js, or test setup is needed. Open a terminal in the downloaded project folder.

macOS (Docker Desktop):

```sh
mkdir -p config exports backups
docker compose up -d --build app
docker compose exec app python -m spec_enricher.cli bootstrap-url
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force config,exports,backups | Out-Null
docker compose up -d --build app
docker compose exec app python -m spec_enricher.cli bootstrap-url
```

Open the printed one-time sign-in link. The UI is at <http://127.0.0.1:8080>. The link expires after five minutes; do not share it. Generate another with the same command if needed. Sessions expire after 12 hours or a restart.

The first build downloads pinned images and hash-locked dependencies. The app does not install packages or download models at startup. Allow 2 CPUs / 2 GiB for the app, plus the resources your model needs.

The main compose.yaml sets the localhost port, persistent storage, and container security settings. It is the application's launcher. The optional compose.test.yaml is only for the checks below.

The UI starts in **English**. Choose English or Русский in the language selector; the browser remembers your choice. Language changes preserve the selected product and page and ask before discarding unsaved fields. The page's file picker follows this choice; the operating system's file dialog follows the browser/system language. Documents, catalog sources, prompts, and saved model outputs are not translated.

## First review

1. Create a product. Under **Documentation → Specification files**, select several Markdown files together, such as proposal, design, specs, and tasks. The default role keeps each file as a separate document. Limits: 20 files per upload, 1 MiB each, 10 MiB total product corpus.
2. Under **Requirements**, add requirements: review and enable the built-in catalog, select a smaller domain scope, import a schema 4.0 catalog, or author custom requirements. Templates in [templates](templates/) are not automatically active policy.
3. Configure one model under **Settings → Model connections**, run its capability test, and approve the exact destination and data scopes for this product.
4. Open **Analysis → New review**. The default **Check documents against requirements** mode lets you select all files, requirements and one model connection. Review readiness and the data-access plan, then start.
5. Open **Analysis → History and results** and read the independent report: requirement assessments, findings, unresolved questions and source evidence. Download its Markdown + JSON ZIP. No patches or rewritten specification need to be accepted.

Optional source imports and snapshots are under **Settings → External sources**, also linked from **Documentation**. They are separate from uploaded files selected for checking.

### Current review-mode limits

- A model-only applicability or conditional-clause exclusion stays unknown until supported by explicit user confirmation. Answering scope questions is optional: you can answer a subset and prepare a new review, while unresolved scope remains unknown. Original documents and raw model output stay unchanged. Preparing answers does not automatically start the review.
- Unknown, not assessed, missing context, and incomplete results are not a pass. They are also not proof of a defect. If retrieval cannot cover all selected document blocks, the affected assessment is conservatively unknown and the run is partial.
- The review checks documentation, not a running product. Model conclusions and quotations require human review; there is no security percentage or certification.
- Findings in document-review mode are currently read-only. Accept/reject decisions and patch editing belong to the advanced comparison workflow.
- Custom requirement catalogs work in review mode. Custom stage prompts, skills and saved pipelines are not yet supported there; the UI makes this restriction explicit instead of silently ignoring them. Use **Analysis mode → Advanced: compare and enrich changes** for these configurations.
- Review mode builds document blocks deterministically at P2 and produces its report after assessment (P6), without model-based change inference or generation/reassessment stages P7/P8. Exports require a ready report and include source references and hashes; treat document quotations as potentially sensitive.

### Advanced comparison

Choose **Analysis mode → Advanced: compare and enrich changes** when you have both existing baseline documentation and a separate Markdown change document. This retains the prior comparison, enrichment, patch review and export workflow, including old history. Multiple change-role Markdown files are combined with filename headings (100 KiB total); ordinary review uploads remain separate.

Changing selected patches changes the reviewed candidate. Preview/export marks that candidate as requiring a new analysis; it does not silently call a model or overwrite documents.

### Import a Hub skill as a draft

Under **Settings → Skills and advanced analysis settings**, choose **Import SKILL.md as draft** and select
one entry file from `skills/<name>/SKILL.md`. The preview shows the source text,
SHA-256 fingerprint, referenced resources and possible command/tool declarations.
Save creates a new **draft prompt**, never a published prompt or enabled skill.
No existing configuration is overwritten. Only simple `name`/`description` YAML
scalars are recognized; source text is preserved. Limits: UTF-8, 30,000 characters,
100 KiB; template braces conflicting with prompt syntax must be adapted first.

This supports reviewing entry files from Product-security-skills, not installing
its complete Bifrost package. References, assets, scripts, manifest files and tool
permissions are not imported. Dependency/command detection is advisory, not a
complete compatibility or security audit. For the advanced comparison workflow, adapt the instructions and output
schema before publishing a prompt and creating a declarative skill. Custom skills
are not available in document-review mode. Check the source license before
redistributing imported material. Code scanning, code changes, cleanup and fix verification are not supplied
by this importer. Nothing is sent to a model during preview or save.

Built-in prompts work without custom skills. The importer does not install or
execute Hub skill folders or overwrite existing configuration.

## Connect a model

For LM Studio on the Docker Desktop host:

- Load a model and start its server in LM Studio yourself.
- Use `http://host.docker.internal:1234/v1`. Container `localhost` is not the host.
- Use the app's DNS discovery and approve the exact private addresses for this endpoint.
- Select the actual loaded model ID. Use supported `json_schema` output, or explicitly choose degraded `prompt_json`.
- Run the capability test, then approve the project's destination and data scopes.

A successful capability test proves protocol compatibility, not analysis quality or sufficient context for a full run. The app does not load/switch models or fall back to another endpoint. Model-specific options must be set explicitly. Do not expose LM Studio to a LAN or change firewall/CORS settings just to make a test pass.

Other OpenAI-compatible endpoints require explicit external-access policy and project grants. You can enforce an additional startup restriction with `APP_DENY_EXTERNAL_EGRESS=true`; approved private endpoints remain available. Optional MCP sources support typed, explicitly allowlisted read-only calls, not arbitrary tools or shell execution.

Credentials are **session-only by default** and must be entered again after restart. Do not put keys in Git, Compose environment values, documents, or shell history. Advanced file-secret support requires a separate read-only mount under `/run/secrets` and file mode; Docker does not encrypt the host secret file.

### LM Studio native profile

For models whose OpenAI-compatible endpoint returns JSON only in `reasoning_content`,
create a separate **LM Studio native** connection using
`http://host.docker.internal:1234/api/v1`. Existing OpenAI-compatible profiles stay
unchanged. Choose reasoning **none** to send native `reasoning: "off"` per request;
this does not change LM Studio's global settings. The native route disables chat
storage and integrations and accepts only final message output, never reasoning
or tool output as a result.

This adapter requires explicit **prompt JSON + server validation** mode: the JSON
schema is supplied in the instruction and the application validates the response
locally. It is not server-enforced structured generation. Native requests support
only a system instruction plus one user input; automatic history-based structural
repair is unavailable and rejected output stops the affected stage. A successful capability
test demonstrates compatibility, not analysis quality. Existing data-scope approval,
budget, evidence and schema checks still apply. See the official [native chat API](https://lmstudio.ai/docs/developer/rest/chat)
and [model capability API](https://lmstudio.ai/docs/developer/rest/list).

For the initial built-in P3 stage in native mode, the model's wire contract omits
catalog `version`: this identifier is application-owned metadata. The application
first validates the complete version-free wire response, then attaches the version
from the uniquely pinned requirement/predicate/side mapping and runs the normal
canonical schema, evidence and coverage checks. Unknown, duplicate or ambiguous
identities and model-supplied versions are rejected. Raw responses remain unchanged;
the normalized output records the wire contract and pinned-metadata fingerprint.
Custom prompts, skills and architectural-review calls retain their existing contracts.

Native built-in P5 uses the explicit `builtin.P5.native.optional-design-summary.v1`
wire contract. Its `applicable_design` presentation summary may be omitted; it is
then stored as null without inferred text. A supplied summary must still be a
nonempty string of at most 600 characters. Decisions, evidence, explanations,
question/control-removal fields and pinned identifiers retain their mandatory
validation. Custom prompts/skills, OpenAI-compatible contracts and P8 are unchanged.
The wire version and missing-field handling are recorded on output artifacts and
participate in cache identity. This does not establish model assessment accuracy.

For the native built-in P3 fact-pins wire contract only, one narrowly bounded
syntax normalization may insert a single missing `{` at an array-element start
immediately before a JSON string key and colon. It requires a unique complete
strict parse, at most 64 KiB and 32 candidates. It does not remove tokens, edit
strings/values, accept duplicate keys, combine with Markdown-fence normalization,
or repair truncation/multiple errors. Original raw output is retained and the
normalization plus character offset is recorded. All wire/schema, pinned-ID,
evidence and semantic checks still run. This is syntax-only normalization, not
a guarantee of model intent, assessment quality or constrained generation.

## Data and updates

The named `app_data` Docker volume stores the database and artifacts. The local `config/`, `exports/`, and `backups/` directories are private and excluded from Git. **Never use `docker compose down -v` for an update.**

A fresh installation can optionally import a versioned `config/settings-bundle.json` once. It does not import credentials or grants; imported connections and bindings start disabled. Use the UI for later settings imports.

To stop without deleting data:

```sh
docker compose stop app
```

Before updating, finish or cancel active runs and create a backup. Then build the intended source revision and restart:

```sh
docker compose up -d --build app
docker compose ps
docker compose exec app python -m spec_enricher.cli bootstrap-url
```

Re-enter session credentials. If backend methodology changed, create a new analysis rather than resuming an incompatible run. Existing history remains readable.

## Backup and restore

Backups contain documents, prompts, and model outputs. **They are not encrypted.** Protect them as sensitive files. Session credentials and tokens are excluded.

After finishing or cancelling active runs:

```sh
docker compose exec app python -m spec_enricher.cli backup --output /backups/backup-local.tar.gz
```

The archive appears in `./backups`. Choose a new filename for each backup; existing files are not overwritten. The app coordinates database writes and records per-file hashes. Copying a live SQLite file is not an equivalent backup.

To restore into an empty installation using a compatible app version, place the archive in `backups/`, then:

```sh
docker compose stop app
docker compose run --rm --no-deps app python -m spec_enricher.cli restore --input /backups/backup-local.tar.gz
docker compose up -d app
docker compose exec app python -m spec_enricher.cli bootstrap-url
```

Restore rejects an active service, invalid archives, or a non-empty target. Intentional replacement requires `--replace-existing` and first creates a pre-restore backup. Restore disables connections and grants until reviewed again. Re-enter credentials and verify endpoints, models, and scopes. Older generations and external backups are not deleted by a project purge.

## Security and limitations

- Single-user, localhost-only. Keep publication on `127.0.0.1`; this is not a public web server.
- The container runs non-root with a read-only filesystem, dropped capabilities, and no Docker socket. Persistent data is separate.
- Project data and history are scoped. One-time login, session, Host/Origin/CSRF, bounded parsing, path, schema, and citation checks are implemented.
- Untrusted documents, catalog text, and model/tool outputs cannot authorize network access, change permissions, or execute commands.
- Model output can be wrong despite valid structure and citations. Observed runs have confused missing documentation with an explicit contradiction. No detection-rate or security-certification claim is made.
- Third-party MCP servers control their own implementation. Read-only metadata does not prove the absence of server-side effects.
- Known-secret guards are not universal data-loss prevention. Review documents before sending them to an external service.
- Actual Windows Docker Desktop, auth/custom-port LM Studio, and a complete manual accessibility audit are not fully qualified by the available evidence.
- Host malware/admin access, Docker compromise, and physical secure erasure are outside the app's protection. Keep backups and exported data secure.

## Troubleshooting

- **Sign-in link rejected:** generate a new link; it is single-use and time-limited.
- **Model unreachable:** check the server, loaded model, host address, and exact approved private addresses.
- **Capability passes, analysis fails:** check context size, stage errors, budgets, and output mode. Partial/unknown is not a passing result.
- **Credential missing:** re-enter the session credential rather than adding it to a document or configuration file.
- **Run cannot resume after an update:** preserve history and start a new run with the saved inputs.

## Optional checks

Useful unit, API, security, migration, and localization tests are kept separately from the runtime image. They use synthetic inputs, an isolated temporary filesystem, and no external network or real model:

```sh
docker compose -f compose.test.yaml run --rm --build tests
```

The backend checks need no host Python/Node or additional development dependencies. Separate focused UI checks require an existing Node.js installation:

```sh
node tests/ui-files.test.mjs
node tests/ui-review-mode.test.mjs
node tests/ui-skill-import.test.mjs
```

Set `SDD_TEST_LANG=ru` to repeat the file-upload checks in Russian. The optional `tests/ui-navigation.test.cjs` also needs an installed Playwright package and Chromium/Chrome; `PLAYWRIGHT_MODULE` and `CHROME_BINARY` can point to existing installations. It launches a fresh isolated browser with a local mock API, not a live model or product. Screenshots go to a temporary directory, or to `UI_SCREENSHOT_DIR` when supplied.

These checks are not proof of model accuracy, full accessibility, or Windows compatibility.

## Architecture

![Architecture: the local browser uses the API and review pipeline, which reads the pinned catalog, stores project history, and prepares exports for human review. Model and MCP access goes through the permission broker and requires an approved destination and data scope.](architecture.svg)

The local browser talks to the API and review pipeline. The pipeline reads the pinned catalog, keeps project data and history, and prepares exports for human review. Model and MCP requests pass through the permission broker and require an approved destination and data scope.

The diagram is a local SVG image, so it does not require Mermaid support in the Markdown preview. If your preview blocks local images, [open the architecture diagram](architecture.svg) directly.

### Document-review sequence

1. The browser submits selected document, catalog and connection IDs to preflight, which validates the configuration and exposes the data-access plan.
2. Starting the review pins its inputs and configuration. P1 snapshots the selected documents; P2 builds the checked block set without a model call.
3. P3 extracts applicability facts. P4 applies deterministic catalog conditions and holds model-only exclusions for user confirmation.
4. P5 assesses applicable obligations against checked-document evidence. Schema, pinned-ID, citation and coverage checks can reject or mark incomplete results.
5. P6 consolidates assessments, findings and unresolved questions. The report endpoint and ZIP export expose the independent report, with no P7/P8 rewrite stages.
6. Optional scope answers prepare a new review. The user reviews its inputs and starts it explicitly; earlier reports and original documents are preserved.

## Contents and licenses

This checkout contains the runnable app and a focused deterministic test suite, without historical experiments or development tooling. `spec_enricher/` is the backend and local UI; `catalog/` and `sources/` contain the pinned offline corpus; `templates/` supports authoring. The three retained `qa/` files are provenance referenced by the catalog, not a test suite. Sources remain in Russian to preserve their exact hashes and quotations.

Original application code uses the [MIT License](LICENSE). The catalog/source prose retains CC BY 4.0, its [upstream license](sources/LICENSE.upstream), attribution, and per-record provenance. See [NOTICE](NOTICE) for the boundaries and third-party terms. User documents and model outputs are not relicensed by the application.
