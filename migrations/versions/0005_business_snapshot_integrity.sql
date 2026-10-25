-- 0005: serialize initial snapshot loading and freeze every loaded row.

CREATE TABLE IF NOT EXISTS business.dataset_loads (
    data_snapshot_id CHAR(64) PRIMARY KEY
        REFERENCES business.data_snapshots(data_snapshot_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'loading', 'complete')),
    completed_at TIMESTAMPTZ
);

DROP TRIGGER IF EXISTS dataset_loads_insert_immutable ON business.dataset_loads;

INSERT INTO business.dataset_loads (data_snapshot_id, status, completed_at)
SELECT snapshot.data_snapshot_id,
       CASE WHEN EXISTS (
           SELECT FROM business.orders AS orders
           WHERE orders.data_snapshot_id = snapshot.data_snapshot_id
       ) THEN 'complete' ELSE 'pending' END,
       CASE WHEN EXISTS (
           SELECT FROM business.orders AS orders
           WHERE orders.data_snapshot_id = snapshot.data_snapshot_id
       ) THEN now() ELSE NULL END
FROM business.data_snapshots AS snapshot
ON CONFLICT (data_snapshot_id) DO NOTHING;

CREATE OR REPLACE FUNCTION business.enforce_dataset_load_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status = 'complete'
       OR NOT (
           (OLD.status = 'pending' AND NEW.status = 'loading')
           OR (OLD.status = 'loading' AND NEW.status = 'complete')
       ) THEN
        RAISE EXCEPTION 'dataset load state cannot be reset or removed'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION business.reject_dataset_load_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'dataset load state is provisioned by migration only'
        USING ERRCODE = 'P0001';
END;
$$;

DROP TRIGGER IF EXISTS dataset_loads_insert_immutable ON business.dataset_loads;
CREATE TRIGGER dataset_loads_insert_immutable
    BEFORE INSERT ON business.dataset_loads
    FOR EACH ROW EXECUTE FUNCTION business.reject_dataset_load_insert();

CREATE OR REPLACE FUNCTION business.require_completed_dataset_load()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    final_status TEXT;
BEGIN
    SELECT status INTO final_status
    FROM business.dataset_loads
    WHERE data_snapshot_id = NEW.data_snapshot_id;
    IF final_status = 'loading' THEN
        RAISE EXCEPTION 'dataset load cannot commit while loading'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS dataset_loads_transition ON business.dataset_loads;
CREATE TRIGGER dataset_loads_transition
    BEFORE UPDATE OR DELETE ON business.dataset_loads
    FOR EACH ROW EXECUTE FUNCTION business.enforce_dataset_load_transition();

DROP TRIGGER IF EXISTS dataset_loads_must_complete ON business.dataset_loads;
CREATE CONSTRAINT TRIGGER dataset_loads_must_complete
    AFTER INSERT OR UPDATE ON business.dataset_loads
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION business.require_completed_dataset_load();

CREATE OR REPLACE FUNCTION business.enforce_snapshot_load_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    affected_snapshot_ids CHAR(64)[];
    affected_snapshot_id CHAR(64);
    load_status TEXT;
BEGIN
    IF TG_TABLE_NAME = 'orders' THEN
        IF TG_OP = 'INSERT' THEN
            affected_snapshot_ids := ARRAY[NEW.data_snapshot_id];
        ELSIF TG_OP = 'DELETE' THEN
            affected_snapshot_ids := ARRAY[OLD.data_snapshot_id];
        ELSE
            affected_snapshot_ids := ARRAY[OLD.data_snapshot_id, NEW.data_snapshot_id];
        END IF;
    ELSE
        IF TG_OP = 'INSERT' THEN
            SELECT data_snapshot_id INTO affected_snapshot_id
            FROM business.orders WHERE id = NEW.order_id;
            affected_snapshot_ids := ARRAY[affected_snapshot_id];
        ELSIF TG_OP = 'DELETE' THEN
            SELECT data_snapshot_id INTO affected_snapshot_id
            FROM business.orders WHERE id = OLD.order_id;
            affected_snapshot_ids := ARRAY[affected_snapshot_id];
        ELSE
            SELECT data_snapshot_id INTO affected_snapshot_id
            FROM business.orders WHERE id = OLD.order_id;
            affected_snapshot_ids := ARRAY[affected_snapshot_id];
            SELECT data_snapshot_id INTO affected_snapshot_id
            FROM business.orders WHERE id = NEW.order_id;
            affected_snapshot_ids := array_append(affected_snapshot_ids, affected_snapshot_id);
        END IF;
    END IF;

    FOREACH affected_snapshot_id IN ARRAY affected_snapshot_ids LOOP
        SELECT status INTO load_status
        FROM business.dataset_loads
        WHERE data_snapshot_id = affected_snapshot_id;
        IF NOT FOUND OR load_status IS DISTINCT FROM 'loading' THEN
            RAISE EXCEPTION '% rows are immutable outside initial dataset loading', TG_TABLE_NAME
                USING ERRCODE = 'P0001';
        END IF;
    END LOOP;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS orders_immutable ON business.orders;
DROP TRIGGER IF EXISTS order_items_immutable ON business.order_items;
DROP TRIGGER IF EXISTS orders_load_state ON business.orders;
DROP TRIGGER IF EXISTS order_items_load_state ON business.order_items;

CREATE TRIGGER orders_load_state
    BEFORE INSERT OR UPDATE OR DELETE ON business.orders
    FOR EACH ROW EXECUTE FUNCTION business.enforce_snapshot_load_state();

CREATE TRIGGER order_items_load_state
    BEFORE INSERT OR UPDATE OR DELETE ON business.order_items
    FOR EACH ROW EXECUTE FUNCTION business.enforce_snapshot_load_state();
