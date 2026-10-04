-- 生产库 ~/.deskmate/deskmate.db 真实 DDL 快照（阶段 2 部署前）。
-- 用于同步层测试，确保 NOT NULL / DEFAULT 约束与生产一致，避免假绿。
-- 注意：learned_history / loved_entries 部署前尚无 sync_* 列，由 migrate() 经 ALTER 补；
-- profile_items 已含 sync_* 列（阶段 1 已迁移）。

CREATE TABLE profile_items (
                    id            TEXT PRIMARY KEY,
                    layer         TEXT NOT NULL DEFAULT 'cognitive',
                    dimension     TEXT NOT NULL,
                    topic         TEXT DEFAULT '',
                    content       TEXT DEFAULT '',
                    category      TEXT DEFAULT '',
                    status        TEXT DEFAULT 'active',
                    confidence    REAL DEFAULT 0.5,
                    extra_json    TEXT DEFAULT '{}',
                    first_extracted TEXT,
                    last_updated  TEXT
                , valid_from TEXT, valid_until TEXT, sync_uuid TEXT, sync_hlc TEXT, sync_tombstone INTEGER DEFAULT 0);
CREATE INDEX idx_items_layer_dim_status
                    ON profile_items(layer, dimension, status);
CREATE INDEX idx_items_layer_topic
                    ON profile_items(layer, topic);
CREATE UNIQUE INDEX idx_profile_items_sync_uuid ON profile_items(sync_uuid);

CREATE TABLE learned_history (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       TEXT NOT NULL,
                    fact     TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    fp       TEXT NOT NULL,
                    source   TEXT NOT NULL DEFAULT 'live'
                , entity TEXT, polarity TEXT, status TEXT NOT NULL DEFAULT 'active', superseded_by INTEGER, valid_until TEXT, valid_from TEXT);
CREATE UNIQUE INDEX idx_learned_fp ON learned_history(fp);
CREATE INDEX idx_learned_ts ON learned_history(ts DESC, id DESC);
CREATE INDEX idx_learned_cat ON learned_history(category);
CREATE INDEX idx_learned_entity ON learned_history(entity, status, polarity);

CREATE TABLE loved_entries (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    moment_id  TEXT,
                    content    TEXT NOT NULL,
                    day        TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tags       TEXT NOT NULL DEFAULT '#moment #diary'
                );
CREATE INDEX idx_loved_day ON loved_entries(day DESC);
CREATE INDEX idx_loved_content ON loved_entries(content);
