"""Project-scoped SQLite persistence with immutable document snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import nullcontext
from pathlib import Path

from .snapshots import sha256

SCHEMA_VERSION = 15


class Store:
    def __init__(self, path: Path):
        if path.is_symlink():
            raise ValueError("database_symlink_rejected")
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        os.chmod(path, 0o600)
        self.connection.execute("PRAGMA foreign_keys=ON")
        # Purged rows should not remain as recoverable SQLite free-list payloads
        # in later logical backups. OS/WAL/SSD secure erase is not promised.
        self.connection.execute("PRAGMA secure_delete=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        fresh_installation = version == 0
        if version > SCHEMA_VERSION:
            self.connection.close()
            raise ValueError("database schema is newer than this application")
        if 0 < version < SCHEMA_VERSION:
            try:
                directory = path.parent / "migration-backups"
                directory.mkdir(mode=0o700, exist_ok=True)
                backup_path = directory / (f"schema-{version}-" + str(uuid.uuid4()) + ".sqlite3")
                temporary = backup_path.with_suffix(".tmp")
                backup = sqlite3.connect(str(temporary))
                try:
                    self.connection.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("pre-migration backup failed integrity check")
                finally:
                    backup.close()
                os.chmod(temporary, 0o600)
                os.replace(temporary, backup_path)
            except (OSError, sqlite3.Error, ValueError):
                self.connection.close()
                raise
        self.connection.execute("PRAGMA journal_mode=WAL")
        if version == 0:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL
                );
                CREATE TABLE documents (
                    project_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('baseline', 'delta')),
                    content BLOB NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(project_id, id),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE INDEX documents_by_project ON documents(project_id, kind);
                PRAGMA user_version=1;
                COMMIT;
            """)
            version = 1
        if version == 1:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE catalog_versions (
                    project_id TEXT NOT NULL, id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL, version TEXT NOT NULL,
                    content BLOB NOT NULL, sha256 TEXT NOT NULL,
                    built_in INTEGER NOT NULL CHECK(built_in IN (0,1)),
                    PRIMARY KEY(project_id, id), UNIQUE(project_id, catalog_id, version),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE catalog_bindings (
                    project_id TEXT NOT NULL, catalog_version_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    PRIMARY KEY(project_id, catalog_version_id),
                    FOREIGN KEY(project_id, catalog_version_id) REFERENCES catalog_versions(project_id, id) ON DELETE CASCADE
                );
                CREATE TABLE requirement_index (
                    project_id TEXT NOT NULL, catalog_version_id TEXT NOT NULL,
                    requirement_id TEXT NOT NULL, version TEXT NOT NULL,
                    title TEXT NOT NULL, domain TEXT NOT NULL, subdomain TEXT NOT NULL,
                    source_kind TEXT NOT NULL, level TEXT NOT NULL, status TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY(project_id, catalog_version_id, requirement_id),
                    FOREIGN KEY(project_id, catalog_version_id) REFERENCES catalog_versions(project_id, id) ON DELETE CASCADE
                );
                CREATE INDEX requirements_page ON requirement_index(project_id, catalog_version_id, domain, requirement_id);
                PRAGMA user_version=2;
                COMMIT;
            """)

            version = 2
        if version == 2:
            self.connection.executescript("""
                BEGIN;
                ALTER TABLE documents ADD COLUMN role TEXT NOT NULL DEFAULT 'master_spec';
                ALTER TABLE documents ADD COLUMN format TEXT NOT NULL DEFAULT 'md';
                ALTER TABLE documents ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1));
                UPDATE documents SET role='delta' WHERE kind='delta';
                CREATE TABLE catalog_imports (
                    project_id TEXT NOT NULL, id TEXT NOT NULL, content BLOB NOT NULL,
                    sha256 TEXT NOT NULL, issues TEXT NOT NULL, committed_id TEXT,
                    PRIMARY KEY(project_id,id), FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE audit_events (
                    project_id TEXT NOT NULL, id TEXT NOT NULL, action TEXT NOT NULL,
                    object_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,id), FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                PRAGMA user_version=3;
                COMMIT;
            """)
            version = 3
        if version == 3:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE registry_versions (
                    project_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    revision INTEGER NOT NULL, content BLOB NOT NULL, sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,kind,id,revision),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE registry_heads (
                    project_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(project_id,kind,id),
                    FOREIGN KEY(project_id,kind,id,revision) REFERENCES registry_versions(project_id,kind,id,revision) ON DELETE CASCADE
                );
                CREATE TABLE capability_reports (
                    project_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    report TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,kind,id,revision),
                    FOREIGN KEY(project_id,kind,id,revision) REFERENCES registry_versions(project_id,kind,id,revision) ON DELETE CASCADE
                );
                CREATE TABLE egress_grants (
                    project_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    config_sha256 TEXT NOT NULL, scopes TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,kind,id,revision),
                    FOREIGN KEY(project_id,kind,id,revision) REFERENCES registry_versions(project_id,kind,id,revision) ON DELETE CASCADE
                );
                CREATE TABLE instance_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO instance_settings VALUES('allow_external_egress','false');
                PRAGMA user_version=4;
                COMMIT;
            """)
            version = 4
        if version == 4:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE runs (
                    project_id TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
                    manifest TEXT NOT NULL, configuration_snapshot_ref TEXT,
                    request_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, review_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT, finished_at TEXT, error_code TEXT,
                    PRIMARY KEY(project_id,id), UNIQUE(project_id,request_key),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX one_active_run ON runs((1)) WHERE status IN ('queued','running','waiting_approval');
                CREATE TABLE run_documents (
                    project_id TEXT NOT NULL, run_id TEXT NOT NULL, document_id TEXT NOT NULL, role TEXT NOT NULL,
                    PRIMARY KEY(project_id,run_id,document_id),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,id)
                );
                CREATE TABLE run_catalogs (
                    project_id TEXT NOT NULL, run_id TEXT NOT NULL, catalog_version_id TEXT NOT NULL,
                    PRIMARY KEY(project_id,run_id,catalog_version_id),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id,catalog_version_id) REFERENCES catalog_versions(project_id,id)
                );
                CREATE TABLE run_configs (
                    project_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL, object_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(project_id,run_id,kind,object_id),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id,kind,object_id,revision) REFERENCES registry_versions(project_id,kind,id,revision)
                );
                CREATE TABLE artifacts (
                    project_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL,
                    metadata TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,id),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE
                );
                CREATE INDEX artifacts_by_run ON artifacts(project_id,run_id,kind);
                CREATE TABLE stages (
                    project_id TEXT NOT NULL, run_id TEXT NOT NULL, stage_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    envelope TEXT NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY(project_id,run_id,stage_id,attempt_id),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE
                );
                CREATE TABLE requirement_results (
                    project_id TEXT NOT NULL, run_id TEXT NOT NULL, requirement_id TEXT NOT NULL,
                    catalog_version_id TEXT NOT NULL, requirement_version TEXT NOT NULL,
                    baseline TEXT NOT NULL, proposed TEXT NOT NULL, change_effect TEXT NOT NULL,
                    PRIMARY KEY(project_id,run_id,requirement_id),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id,catalog_version_id,requirement_id) REFERENCES requirement_index(project_id,catalog_version_id,requirement_id)
                );
                CREATE TABLE findings (
                    project_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL,
                    requirement_id TEXT, type TEXT NOT NULL, severity TEXT NOT NULL, change_effect TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY(project_id,id),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id,run_id,requirement_id) REFERENCES requirement_results(project_id,run_id,requirement_id)
                );
                CREATE INDEX findings_page ON findings(project_id,run_id,type,severity,change_effect);
                CREATE TABLE review_decisions (
                    project_id TEXT NOT NULL, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    target_kind TEXT NOT NULL, target_id TEXT NOT NULL, decision TEXT NOT NULL, comment TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,run_id,revision),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE
                );
                CREATE TABLE model_cache (
                    project_id TEXT NOT NULL, cache_key TEXT NOT NULL, artifact_id TEXT NOT NULL,
                    PRIMARY KEY(project_id,cache_key),
                    FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id) ON DELETE CASCADE
                );
                PRAGMA user_version=5;
                COMMIT;
            """)
            version = 5
        if version == 5:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE exports (
                    project_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL,
                    review_revision INTEGER NOT NULL, request_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                    artifact_id TEXT NOT NULL, bundle_sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,id), UNIQUE(project_id,run_id,request_key),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,id)
                );
                PRAGMA user_version=6;
                COMMIT;
            """)
            version = 6
        if version == 6:
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO instance_settings VALUES('maintenance','false')"
                )
                tables = (
                    "projects",
                    "documents",
                    "catalog_versions",
                    "catalog_bindings",
                    "requirement_index",
                    "catalog_imports",
                    "registry_versions",
                    "registry_heads",
                    "capability_reports",
                    "egress_grants",
                    "runs",
                    "run_documents",
                    "run_catalogs",
                    "run_configs",
                    "artifacts",
                    "stages",
                    "requirement_results",
                    "findings",
                    "review_decisions",
                    "model_cache",
                    "exports",
                )
                for table in tables:
                    for action in ("INSERT", "UPDATE", "DELETE"):
                        self.connection.execute(
                            f"CREATE TRIGGER maintenance_{table}_{action.lower()} BEFORE {action} ON {table} WHEN (SELECT value FROM instance_settings WHERE key='maintenance')='true' BEGIN SELECT RAISE(ABORT,'maintenance_mode'); END"
                        )
                self.connection.execute("PRAGMA user_version=7")
            version = 7
        if version == 7:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE user_catalogues(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE user_catalogue_versions(
                    user_catalogue_id TEXT NOT NULL,revision INTEGER NOT NULL,content BLOB NOT NULL,sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(user_catalogue_id,revision),FOREIGN KEY(user_catalogue_id) REFERENCES user_catalogues(id)
                );
                CREATE TABLE requirement_drafts(
                    id TEXT NOT NULL,revision INTEGER NOT NULL,scope TEXT NOT NULL,project_id TEXT,user_catalogue_id TEXT,
                    content BLOB NOT NULL,sha256 TEXT NOT NULL,committed_catalog_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(id,revision),FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_catalogue_id) REFERENCES user_catalogues(id),
                    CHECK((scope='project' AND project_id IS NOT NULL AND user_catalogue_id IS NULL) OR (scope='user_catalogue' AND project_id IS NULL AND user_catalogue_id IS NOT NULL))
                );
                CREATE TABLE project_requirement_overrides(
                    project_id TEXT NOT NULL,requirement_id TEXT NOT NULL,version TEXT NOT NULL,revision INTEGER NOT NULL,
                    ranking TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,requirement_id,version,revision),FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                PRAGMA user_version=8;
                COMMIT;
            """)
            with self.connection:
                for table in (
                    "user_catalogues",
                    "user_catalogue_versions",
                    "requirement_drafts",
                    "project_requirement_overrides",
                ):
                    for action in ("INSERT", "UPDATE", "DELETE"):
                        self.connection.execute(
                            f"CREATE TRIGGER maintenance_{table}_{action.lower()} BEFORE {action} ON {table} WHEN (SELECT value FROM instance_settings WHERE key='maintenance')='true' BEGIN SELECT RAISE(ABORT,'maintenance_mode'); END"
                        )
            version = 8
        if version == 8:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE source_snapshots(
                    project_id TEXT NOT NULL,id TEXT NOT NULL,source_id TEXT NOT NULL,source_revision INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'source' CHECK(kind='source'),uri TEXT NOT NULL,
                    raw_content BLOB NOT NULL,raw_sha256 TEXT NOT NULL,document_id TEXT,catalog_import_id TEXT,
                    metadata TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,id),
                    FOREIGN KEY(project_id,kind,source_id,source_revision) REFERENCES registry_versions(project_id,kind,id,revision),
                    FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,id),
                    FOREIGN KEY(project_id,catalog_import_id) REFERENCES catalog_imports(project_id,id)
                );
                PRAGMA user_version=9;
                COMMIT;
            """)
            with self.connection:
                for action in ("INSERT", "UPDATE", "DELETE"):
                    self.connection.execute(
                        f"CREATE TRIGGER maintenance_source_snapshots_{action.lower()} BEFORE {action} ON source_snapshots WHEN (SELECT value FROM instance_settings WHERE key='maintenance')='true' BEGIN SELECT RAISE(ABORT,'maintenance_mode'); END"
                    )
            version = 9
        if version == 9:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE settings_imports(bundle_sha256 TEXT PRIMARY KEY,receipt TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                PRAGMA user_version=10;
                COMMIT;
            """)
            with self.connection:
                for action in ("INSERT", "UPDATE", "DELETE"):
                    self.connection.execute(
                        f"CREATE TRIGGER maintenance_settings_imports_{action.lower()} BEFORE {action} ON settings_imports WHEN (SELECT value FROM instance_settings WHERE key='maintenance')='true' BEGIN SELECT RAISE(ABORT,'maintenance_mode'); END"
                    )
            version = 10
        if version == 10:
            self.connection.executescript("""
                BEGIN;
                CREATE TABLE settings_previews(id TEXT PRIMARY KEY,sha256 TEXT NOT NULL UNIQUE,content BLOB NOT NULL,expires_at REAL NOT NULL);
                PRAGMA user_version=11;
                COMMIT;
            """)
            with self.connection:
                for action in ("INSERT", "UPDATE", "DELETE"):
                    self.connection.execute(
                        f"CREATE TRIGGER maintenance_settings_previews_{action.lower()} BEFORE {action} ON settings_previews WHEN (SELECT value FROM instance_settings WHERE key='maintenance')='true' BEGIN SELECT RAISE(ABORT,'maintenance_mode'); END"
                    )

            version = 11
        if version == 11:
            self.connection.executescript("""
                BEGIN;
                ALTER TABLE run_configs RENAME TO run_configs_legacy;
                CREATE TABLE run_configs(
                    project_id TEXT NOT NULL,run_id TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,
                    PRIMARY KEY(project_id,run_id,kind,object_id,revision),
                    FOREIGN KEY(project_id,run_id) REFERENCES runs(project_id,id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id,kind,object_id,revision) REFERENCES registry_versions(project_id,kind,id,revision)
                );
                INSERT INTO run_configs SELECT * FROM run_configs_legacy;
                DROP TABLE run_configs_legacy;
                CREATE INDEX audit_by_created_at ON audit_events(created_at);
                INSERT OR IGNORE INTO instance_settings VALUES('diagnostic_retention_days','14');
                PRAGMA user_version=12;
                COMMIT;
            """)
            with self.connection:
                for action in ("INSERT", "UPDATE", "DELETE"):
                    self.connection.execute(
                        f"CREATE TRIGGER maintenance_run_configs_{action.lower()} BEFORE {action} ON run_configs WHEN (SELECT value FROM instance_settings WHERE key='maintenance')='true' BEGIN SELECT RAISE(ABORT,'maintenance_mode'); END"
                    )

            version = 12
        if version == 12:
            self.connection.executescript("""
                BEGIN;
                ALTER TABLE user_catalogues ADD COLUMN next_revision INTEGER NOT NULL DEFAULT 1;
                UPDATE user_catalogues SET next_revision=coalesce((SELECT max(revision)+1 FROM user_catalogue_versions WHERE user_catalogue_id=user_catalogues.id),1);
                CREATE TABLE purge_jobs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,plan TEXT NOT NULL,status TEXT NOT NULL,error_code TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX purge_by_project ON purge_jobs(project_id,status);
                PRAGMA user_version=13;
                COMMIT;
            """)
            with self.connection:
                for action in ("INSERT", "UPDATE", "DELETE"):
                    self.connection.execute(
                        f"CREATE TRIGGER maintenance_purge_jobs_{action.lower()} BEFORE {action} ON purge_jobs WHEN (SELECT value FROM instance_settings WHERE key='maintenance')='true' BEGIN SELECT RAISE(ABORT,'maintenance_mode'); END"
                    )
            version = 13
        if version == 13:
            self.connection.executescript("""
                BEGIN;
                CREATE INDEX requirement_identity_lookup ON requirement_index(project_id,requirement_id,version);
                CREATE TABLE catalog_updates(
                    project_id TEXT NOT NULL,catalog_version_id TEXT NOT NULL,
                    content TEXT NOT NULL,sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(project_id,catalog_version_id),
                    FOREIGN KEY(project_id,catalog_version_id) REFERENCES catalog_versions(project_id,id) ON DELETE CASCADE
                );
                PRAGMA user_version=14;
                COMMIT;
            """)
            with self.connection:
                for action in ("INSERT", "UPDATE", "DELETE"):
                    self.connection.execute(
                        f"CREATE TRIGGER maintenance_catalog_updates_{action.lower()} BEFORE {action} ON catalog_updates WHEN (SELECT value FROM instance_settings WHERE key='maintenance')='true' BEGIN SELECT RAISE(ABORT,'maintenance_mode'); END"
                    )
            version = 14
        if version == 14:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "INSERT OR IGNORE INTO instance_settings VALUES('bootstrap_configuration',?)",
                    (
                        json.dumps(
                            {"status": "pending" if fresh_installation else "existing_instance"}
                        ),
                    ),
                )
                self.connection.execute("PRAGMA user_version=15")

    def close(self):
        self.connection.close()

    def create_project(self, name: str, description: str = "") -> str:
        if not name.strip() or len(name) > 200 or len(description) > 10_000:
            raise ValueError("invalid project fields")
        project_id = str(uuid.uuid4())
        with self.connection:
            self.connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?)", (project_id, name, description)
            )
        return project_id

    def projects(self):
        return self.connection.execute(
            "SELECT id, name, description FROM projects ORDER BY name, id"
        ).fetchall()

    def add_document(
        self,
        project_id: str,
        name: str,
        kind: str,
        data: bytes,
        *,
        role: str = "master_spec",
        fmt: str = "md",
    ) -> str:
        if not name or len(name) > 255 or kind not in {"baseline", "delta"}:
            raise ValueError("invalid document metadata")
        limit = 100 * 1024 if kind == "delta" else 1024 * 1024
        if len(data) > limit:
            raise ValueError("document size limit exceeded")
        data.decode("utf-8")
        if (
            b"\x00" in data
            or role not in {"master_spec", "context", "delta"}
            or fmt not in {"md", "json", "yaml", "yml"}
        ):
            raise ValueError("invalid document content or role")
        if kind == "delta":
            role = "delta"
            if fmt != "md":
                raise ValueError("delta must be Markdown")
        document_id = str(uuid.uuid4())
        with self.connection if not self.connection.in_transaction else nullcontext():
            total = self.connection.execute(
                "SELECT coalesce(sum(length(content)),0) FROM documents WHERE project_id=? AND active=1 AND kind='baseline'",
                (project_id,),
            ).fetchone()[0]
            if kind == "baseline" and total + len(data) > 10 * 1024 * 1024:
                raise ValueError("project corpus size limit exceeded")
            self.connection.execute(
                "INSERT INTO documents(project_id,id,name,kind,content,sha256,role,format) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, document_id, name, kind, data, sha256(data), role, fmt),
            )
        return document_id

    def documents(self, project_id: str):
        return self.connection.execute(
            "SELECT id, name, kind, sha256 FROM documents WHERE project_id=? ORDER BY rowid",
            (project_id,),
        ).fetchall()

    def document(self, project_id: str, document_id: str):
        return self.connection.execute(
            "SELECT name, kind, content, sha256 FROM documents WHERE project_id=? AND id=?",
            (project_id, document_id),
        ).fetchone()

    def document_metadata(self, project_id: str):
        return self.connection.execute(
            "SELECT id,name,role,format,length(content),sha256,active FROM documents WHERE project_id=? ORDER BY rowid",
            (project_id,),
        ).fetchall()

    def stage_catalog(self, project_id: str, catalog: dict, issues: list) -> str:
        from .catalog import canonical

        content = canonical(catalog)
        import_id = str(uuid.uuid4())
        with self.connection if not self.connection.in_transaction else nullcontext():
            self.connection.execute(
                "INSERT INTO catalog_imports VALUES (?, ?, ?, ?, ?, NULL)",
                (project_id, import_id, content, sha256(content), json.dumps(issues)),
            )
        return import_id

    def staged_catalog(self, project_id: str, import_id: str):
        return self.connection.execute(
            "SELECT content, sha256, issues, committed_id FROM catalog_imports WHERE project_id=? AND id=?",
            (project_id, import_id),
        ).fetchone()

    def audit(self, project_id: str, action: str, object_id: str):
        with self.connection if not self.connection.in_transaction else nullcontext():
            today = self.connection.execute("SELECT date('now')").fetchone()[0]
            last = self.connection.execute(
                "SELECT value FROM instance_settings WHERE key='last_audit_cleanup_date'"
            ).fetchone()
            if last is None or last[0] != today:
                self.connection.execute(
                    "DELETE FROM audit_events WHERE created_at<datetime('now','-14 days')"
                )
                self.connection.execute(
                    "INSERT INTO instance_settings VALUES('last_audit_cleanup_date',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (today,),
                )
            self.connection.execute(
                "INSERT INTO audit_events(project_id,id,action,object_id) VALUES (?, ?, ?, ?)",
                (project_id, str(uuid.uuid4()), action, object_id),
            )

    def catalog_identity_conflicts(self, project_id: str, catalog: dict):
        from .catalog import semantic_hash

        issues = []
        for position, record in enumerate(catalog["requirements"]):
            rows = self.connection.execute(
                "SELECT content FROM requirement_index WHERE project_id=? AND requirement_id=? AND version=?",
                (project_id, record["id"], record["version"]),
            )
            if any(semantic_hash(json.loads(row[0])) != semantic_hash(record) for row in rows):
                issues.append(
                    {
                        "path": f"/requirements/{position}/version",
                        "code": "immutable_requirement_version_conflict",
                        "message": "The project already has different normative content under this requirement ID/version; use a reviewed new requirement version.",
                    }
                )
                if len(issues) >= 200:
                    break
        return issues

    def bind_catalog(
        self, project_id: str, catalog: dict, *, built_in: bool = False, activate: bool = True
    ) -> str:
        outer = self.connection.in_transaction
        with self.connection if not outer else nullcontext():
            if not outer:
                self.connection.execute("BEGIN IMMEDIATE")
            return self._bind_catalog(project_id, catalog, built_in=built_in, activate=activate)

    def _bind_catalog(
        self, project_id: str, catalog: dict, *, built_in: bool, activate: bool
    ) -> str:
        from .catalog import canonical

        payload = canonical(catalog)
        checksum = sha256(payload)
        existing = self.connection.execute(
            "SELECT id, sha256 FROM catalog_versions WHERE project_id=? AND catalog_id=? AND version=?",
            (project_id, catalog["catalog_id"], catalog["catalog_version"]),
        ).fetchone()
        if existing:
            if existing[1] != checksum:
                raise ValueError("catalog ID/version conflict: create a reviewed new version")
            catalog_id = existing[0]
            with self.connection if not self.connection.in_transaction else nullcontext():
                if activate:
                    self._prepare_catalog_activation(project_id, catalog_id)
                self.connection.execute(
                    "INSERT INTO catalog_bindings VALUES (?, ?, ?) ON CONFLICT(project_id, catalog_version_id) DO UPDATE SET enabled=CASE WHEN excluded.enabled=1 THEN 1 ELSE catalog_bindings.enabled END",
                    (project_id, catalog_id, int(activate)),
                )
            return catalog_id
        if self.catalog_identity_conflicts(project_id, catalog):
            raise ValueError("immutable_requirement_version_conflict")
        catalog_id = str(uuid.uuid4())
        with self.connection if not self.connection.in_transaction else nullcontext():
            self.connection.execute(
                "INSERT INTO catalog_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    catalog_id,
                    catalog["catalog_id"],
                    catalog["catalog_version"],
                    payload,
                    checksum,
                    int(built_in),
                ),
            )
            self.connection.execute(
                "INSERT INTO catalog_bindings VALUES (?, ?, 0)", (project_id, catalog_id)
            )
            self.connection.executemany(
                "INSERT INTO requirement_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        project_id,
                        catalog_id,
                        r["id"],
                        r["version"],
                        r["title"],
                        r["domain"],
                        r["subdomain"],
                        r["source_kind"],
                        r["level"],
                        r["status"],
                        json.dumps(r, ensure_ascii=False),
                    )
                    for r in catalog["requirements"]
                ],
            )
            if activate:
                self._prepare_catalog_activation(project_id, catalog_id)
                self.connection.execute(
                    "UPDATE catalog_bindings SET enabled=1 WHERE project_id=? AND catalog_version_id=?",
                    (project_id, catalog_id),
                )
        return catalog_id

    def _prepare_catalog_activation(self, project_id, catalog_id):
        from .catalog import digest

        receipt = self.connection.execute(
            "SELECT content FROM catalog_updates WHERE project_id=? AND catalog_version_id=?",
            (project_id, catalog_id),
        ).fetchone()
        if receipt:
            accepted = json.loads(receipt[0])
            if not accepted.get("can_activate"):
                raise ValueError("catalogue_update_review_required")
            previous_id = accepted.get("previous_catalog_version_id")
            if previous_id and self.catalog(project_id, previous_id) is not None:
                from .catalog_management import update_preview

                current = update_preview(
                    self,
                    project_id,
                    previous_id,
                    self.catalog(project_id, catalog_id),
                    clause_mappings=accepted.get("clause_mappings", []),
                    record_mappings=accepted.get("record_mappings", []),
                    retirements=accepted.get("retirements", {}),
                )
                if current["local_conflicts"]:
                    raise ValueError("catalogue_update_local_conflict_review_required")
            return
        row = self.connection.execute(
            "SELECT rowid,catalog_id,content FROM catalog_versions WHERE project_id=? AND id=?",
            (project_id, catalog_id),
        ).fetchone()
        if row is None:
            raise ValueError("catalogue_not_found_in_project")
        candidate = json.loads(row[2])
        new = {record["id"]: record for record in candidate["requirements"]}
        for previous_id, payload in self.connection.execute(
            "SELECT id,content FROM catalog_versions WHERE project_id=? AND catalog_id=? AND rowid<? ORDER BY rowid DESC",
            (project_id, row[1], row[0]),
        ):
            old = {record["id"]: record for record in json.loads(payload)["requirements"]}
            if any(record["status"] == "active" for rid, record in old.items() if rid not in new):
                raise ValueError("catalogue_update_retirement_review_required")
            for rid in old.keys() & new.keys():
                if {part["id"] for part in old[rid]["clauses"]} - {
                    part["id"] for part in new[rid]["clauses"]
                }:
                    raise ValueError("catalogue_update_clause_mapping_required")
            from .catalog_management import update_preview

            if update_preview(self, project_id, previous_id, candidate)["local_conflicts"]:
                raise ValueError("catalogue_update_local_conflict_review_required")
        value = {
            "kind": "initial_or_identity_preserving_adoption",
            "catalog_sha256": digest(candidate),
            "can_activate": True,
            "history_preserved": True,
        }
        self.connection.execute(
            "INSERT INTO catalog_updates(project_id,catalog_version_id,content,sha256) VALUES(?,?,?,?)",
            (project_id, catalog_id, json.dumps(value), digest(value)),
        )

    def catalogs(self, project_id: str):
        return self.connection.execute(
            """SELECT c.id, c.catalog_id, c.version, c.sha256, c.built_in, b.enabled,
            (SELECT count(*) FROM requirement_index r WHERE r.project_id=c.project_id AND r.catalog_version_id=c.id)
            FROM catalog_versions c LEFT JOIN catalog_bindings b ON b.project_id=c.project_id AND b.catalog_version_id=c.id
            WHERE c.project_id=? ORDER BY c.rowid""",
            (project_id,),
        ).fetchall()

    def catalog(self, project_id: str, catalog_id: str):
        row = self.connection.execute(
            "SELECT content FROM catalog_versions WHERE project_id=? AND id=?",
            (project_id, catalog_id),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_catalog_binding(
        self, project_id: str, catalog_id: str, *, enabled: bool = False, remove: bool = False
    ):
        if remove:
            with self.connection if not self.connection.in_transaction else nullcontext():
                self.connection.execute(
                    "DELETE FROM catalog_bindings WHERE project_id=? AND catalog_version_id=?",
                    (project_id, catalog_id),
                )
        else:
            with self.connection if not self.connection.in_transaction else nullcontext():
                if enabled:
                    self._prepare_catalog_activation(project_id, catalog_id)
                self.connection.execute(
                    "INSERT INTO catalog_bindings(project_id,catalog_version_id,enabled) VALUES(?,?,?) ON CONFLICT(project_id,catalog_version_id) DO UPDATE SET enabled=excluded.enabled",
                    (project_id, catalog_id, int(enabled)),
                )

    def requirements(
        self,
        project_id: str,
        catalog_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        domain: str = "",
        profile: str = "",
        level: str = "",
        source_kind: str = "",
    ):
        if not (0 <= offset and 1 <= limit <= 100):
            raise ValueError("invalid page")
        return self.connection.execute(
            """SELECT requirement_id, version, title, domain, subdomain, level, source_kind, status
            FROM requirement_index WHERE project_id=? AND catalog_version_id=?
            AND (?='' OR domain=?) AND (?='' OR level=?) AND (?='' OR source_kind=?)
            AND (?='' OR json_extract(content, '$.source_context.profile')=?)
            ORDER BY requirement_id LIMIT ? OFFSET ?""",
            (
                project_id,
                catalog_id,
                domain,
                domain,
                level,
                level,
                source_kind,
                source_kind,
                profile,
                profile,
                limit,
                offset,
            ),
        ).fetchall()

    def requirement(self, project_id: str, catalog_id: str, requirement_id: str):
        row = self.connection.execute(
            "SELECT content FROM requirement_index WHERE project_id=? AND catalog_version_id=? AND requirement_id=?",
            (project_id, catalog_id, requirement_id),
        ).fetchone()
        return json.loads(row[0]) if row else None
