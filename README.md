# SDD Security Analyzer

A local app for reviewing software design documents against a pinned security catalog. Compare your current documentation with a proposed change, review the findings and suggested edits, then export a separate result. Originals are not overwritten.

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

The UI starts in **English**. Choose English or Русский in the language selector; the browser remembers your choice. Documents, catalog sources, prompts, and saved model outputs are not translated.

## First review

1. Create a project. Add current documents as the baseline and a Markdown document describing the proposed change as the delta.
2. Review and adopt the built-in catalog, select a smaller domain scope, or import your own schema 4.0 requirements. Blank and example authoring templates are available in the UI and [templates](templates/); examples are not automatically active policy.
3. Configure a model under **Connections**, run its capability test, and approve the exact destination and data scopes for this project.
4. Start the analysis after checking inputs, budgets, and permissions.
5. Review findings, unresolved questions, and proposed edits. Select changes, inspect the diff, and export.

Changing the selected patches changes the candidate that was reviewed. Preview/export marks such a candidate as requiring a new analysis; it does not silently call a model or overwrite your documents.

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

No host Python/Node or additional development dependencies are needed. These checks are not proof of model accuracy, full accessibility, or Windows compatibility.

## Architecture

![Architecture: the local browser uses the API and review pipeline, which reads the pinned catalog, stores project history, and prepares exports for human review. Model and MCP access goes through the permission broker and requires an approved destination and data scope.](architecture.svg)

The local browser talks to the API and review pipeline. The pipeline reads the pinned catalog, keeps project data and history, and prepares exports for human review. Model and MCP requests pass through the permission broker and require an approved destination and data scope.

The diagram is a local SVG image, so it does not require Mermaid support in the Markdown preview. If your preview blocks local images, [open the architecture diagram](architecture.svg) directly.

## Contents and licenses

This checkout contains the runnable app and a focused deterministic test suite, without historical experiments or development tooling. `spec_enricher/` is the backend and local UI; `catalog/` and `sources/` contain the pinned offline corpus; `templates/` supports authoring. The three retained `qa/` files are provenance referenced by the catalog, not a test suite. Sources remain in Russian to preserve their exact hashes and quotations.

Original application code uses the [MIT License](LICENSE). The catalog/source prose retains CC BY 4.0, its [upstream license](sources/LICENSE.upstream), attribution, and per-record provenance. See [NOTICE](NOTICE) for the boundaries and third-party terms. User documents and model outputs are not relicensed by the application.
