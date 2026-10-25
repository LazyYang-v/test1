-- 0003: frozen business dataset tables, GMV v1 semantics, and read-only view
-- (P0 design baseline §§3.5, 3.7, 10; ticket 03).

CREATE TABLE IF NOT EXISTS business.data_snapshots (
    data_snapshot_id CHAR(64) PRIMARY KEY,
    generator_version TEXT NOT NULL,
    seed BIGINT NOT NULL,
    order_count BIGINT NOT NULL CHECK (order_count > 0),
    canonicalization TEXT NOT NULL,
    sort_order TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO business.data_snapshots (
    data_snapshot_id,
    generator_version,
    seed,
    order_count,
    canonicalization,
    sort_order
)
VALUES (
    'e38d3afb175b4d130db4e1600cef8710b448bd67151770f1ca6ad6734e0ab5b8',
    '1.0.0',
    20261026,
    108000,
    'json-sort-keys-compact-utf8',
    'order_no,record_type'
)
ON CONFLICT (data_snapshot_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS business.orders (
    id UUID PRIMARY KEY,
    order_no TEXT NOT NULL UNIQUE,
    data_snapshot_id CHAR(64) NOT NULL
        REFERENCES business.data_snapshots(data_snapshot_id),
    region_code TEXT NOT NULL,
    customer_id UUID NOT NULL,
    ordered_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'refunded', 'cancelled')),
    gross_amount NUMERIC(18, 2) NOT NULL CHECK (gross_amount >= 0),
    discount_amount NUMERIC(18, 2) NOT NULL CHECK (discount_amount >= 0),
    paid_amount NUMERIC(18, 2) NOT NULL CHECK (paid_amount >= 0),
    currency CHAR(3) NOT NULL CHECK (currency = 'CNY'),
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT orders_paid_state_matches_paid_at CHECK (
        (paid_at IS NOT NULL) = (status IN ('paid', 'refunded'))
    ),
    CONSTRAINT orders_amounts_reconcile CHECK (
        gross_amount = discount_amount + paid_amount
    ),
    CONSTRAINT orders_unpaid_amount_is_zero CHECK (
        status IN ('paid', 'refunded') OR paid_amount = 0
    )
);

CREATE TABLE IF NOT EXISTS business.order_items (
    id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES business.orders(id),
    sku_id TEXT NOT NULL,
    category_code TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(18, 2) NOT NULL CHECK (unit_price >= 0),
    discount_amount NUMERIC(18, 2) NOT NULL CHECK (discount_amount >= 0),
    paid_amount NUMERIC(18, 2) NOT NULL CHECK (paid_amount >= 0),
    CONSTRAINT order_items_amounts_reconcile CHECK (
        unit_price * quantity = discount_amount + paid_amount
    ),
    CONSTRAINT order_items_order_sku_unique UNIQUE (order_id, sku_id)
);

CREATE TABLE IF NOT EXISTS business.metric_definitions (
    id UUID PRIMARY KEY,
    metric_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    name TEXT NOT NULL,
    source_view TEXT NOT NULL,
    expression_key TEXT NOT NULL,
    allowed_dimensions TEXT[] NOT NULL,
    allowed_filters TEXT[] NOT NULL,
    aggregation TEXT NOT NULL,
    unit TEXT NOT NULL,
    time_column TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT metric_definitions_key_version_unique UNIQUE (metric_key, version)
);

CREATE INDEX IF NOT EXISTS orders_snapshot_region_paid_at_idx
    ON business.orders (data_snapshot_id, region_code, paid_at)
    WHERE paid_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS order_items_order_id_idx
    ON business.order_items (order_id);

CREATE OR REPLACE VIEW business.gmv_v1 AS
SELECT
    data_snapshot_id,
    order_no,
    region_code,
    status,
    paid_at,
    paid_amount,
    currency
FROM business.orders
WHERE paid_at IS NOT NULL;

INSERT INTO business.metric_definitions (
    id,
    metric_key,
    version,
    name,
    source_view,
    expression_key,
    allowed_dimensions,
    allowed_filters,
    aggregation,
    unit,
    time_column,
    status
)
VALUES (
    '00000000-0000-7000-8000-000000000303',
    'gmv',
    1,
    'Gross merchandise value (paid amount)',
    'business.gmv_v1',
    'paid_amount_sum',
    ARRAY['region_code'],
    ARRAY['region_code', 'paid_at'],
    'SUM',
    'CNY',
    'paid_at',
    'active'
)
ON CONFLICT (metric_key, version) DO NOTHING;

CREATE OR REPLACE FUNCTION business.reject_frozen_dataset_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% rows are frozen after dataset load', TG_TABLE_NAME
        USING ERRCODE = 'P0001';
END;
$$;

DROP TRIGGER IF EXISTS data_snapshots_immutable ON business.data_snapshots;
CREATE TRIGGER data_snapshots_immutable
    BEFORE UPDATE OR DELETE ON business.data_snapshots
    FOR EACH ROW EXECUTE FUNCTION business.reject_frozen_dataset_mutation();

DROP TRIGGER IF EXISTS orders_immutable ON business.orders;
CREATE TRIGGER orders_immutable
    BEFORE UPDATE OR DELETE ON business.orders
    FOR EACH ROW EXECUTE FUNCTION business.reject_frozen_dataset_mutation();

DROP TRIGGER IF EXISTS order_items_immutable ON business.order_items;
CREATE TRIGGER order_items_immutable
    BEFORE UPDATE OR DELETE ON business.order_items
    FOR EACH ROW EXECUTE FUNCTION business.reject_frozen_dataset_mutation();

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA business FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA business FROM agentforge_business_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE agentforge_migrator IN SCHEMA business
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE agentforge_migrator IN SCHEMA business
    REVOKE ALL ON TABLES FROM agentforge_business_ro;
GRANT USAGE ON SCHEMA business TO agentforge_business_ro;
GRANT SELECT ON business.gmv_v1 TO agentforge_business_ro;
