DROP TABLE IF EXISTS edit_source, edit_target, edit_no_key, edit_unique, edit_incompatible,
    edit_rls, edit_identity_source, edit_identity_target CASCADE;
DROP TABLE IF EXISTS edit_rollback_source, edit_rollback_target CASCADE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgbackup_data_rls') THEN
        CREATE ROLE pgbackup_data_rls LOGIN;
    END IF;
END
$$;

CREATE TABLE edit_source (
    id integer PRIMARY KEY,
    name text NOT NULL,
    amount numeric(12,2),
    meta jsonb,
    active boolean DEFAULT true,
    tags text[],
    external_id uuid,
    happened_at timestamptz,
    payload bytea,
    label text GENERATED ALWAYS AS (name || ':' || id) STORED
);

CREATE TABLE edit_target (LIKE edit_source INCLUDING ALL);
CREATE TABLE edit_no_key (name text, value integer);
CREATE TABLE edit_unique (code text NOT NULL UNIQUE, value integer);
CREATE TABLE edit_incompatible (
    id integer PRIMARY KEY,
    name text,
    amount date,
    meta jsonb,
    active boolean
);
CREATE TABLE edit_rls (id integer PRIMARY KEY, value text);
CREATE TABLE edit_identity_source (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    value text
);
CREATE TABLE edit_identity_target (LIKE edit_identity_source INCLUDING ALL);
CREATE TABLE edit_rollback_source (id integer PRIMARY KEY, value integer);
CREATE TABLE edit_rollback_target (
    id integer PRIMARY KEY,
    value integer CHECK (value >= 0)
);

INSERT INTO edit_source(
    id, name, amount, meta, active, tags, external_id, happened_at, payload
) VALUES
    (1, 'same', 10.00, '{"a":1}', true, ARRAY['a','b'],
     '00000000-0000-0000-0000-000000000001', '2026-01-01T00:00:00Z', '\x0102'),
    (2, 'source', 20.00, '{"a":2}', true, ARRAY['source'],
     '00000000-0000-0000-0000-000000000002', '2026-01-02T00:00:00Z', '\x0203'),
    (3, 'new', 30.00, '{"a":3}', false, ARRAY[]::text[],
     '00000000-0000-0000-0000-000000000003', '2026-01-03T00:00:00Z', '\x0304');

INSERT INTO edit_target(
    id, name, amount, meta, active, tags, external_id, happened_at, payload
) VALUES
    (1, 'same', 10.0, '{"a":1}', true, ARRAY['a','b'],
     '00000000-0000-0000-0000-000000000001', '2026-01-01T00:00:00Z', '\x0102'),
    (2, 'target', 22.00, '{"a":9}', false, ARRAY['target'],
     '00000000-0000-0000-0000-000000000002', '2026-02-02T00:00:00Z', '\x0909'),
    (4, 'extra', 40.00, '{"a":4}', true, ARRAY['extra'],
     '00000000-0000-0000-0000-000000000004', '2026-01-04T00:00:00Z', '\x0405');

INSERT INTO edit_no_key VALUES ('x', 1);
INSERT INTO edit_unique VALUES ('unique-key', 1);
INSERT INTO edit_rls VALUES (1, 'visible'), (2, 'hidden');
ALTER TABLE edit_rls ENABLE ROW LEVEL SECURITY;
CREATE POLICY edit_rls_policy ON edit_rls
    USING (id = 1) WITH CHECK (id = 1);
GRANT USAGE ON SCHEMA public TO pgbackup_data_rls;
GRANT SELECT, INSERT, UPDATE, DELETE ON edit_rls TO pgbackup_data_rls;

INSERT INTO edit_identity_source(id, value) OVERRIDING SYSTEM VALUE VALUES
    (1, 'one'), (10, 'ten');
INSERT INTO edit_identity_target(value) VALUES ('one');
INSERT INTO edit_rollback_source VALUES (1, -1), (2, 2);
INSERT INTO edit_rollback_target VALUES (1, 1);
