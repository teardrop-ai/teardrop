-- 072: Reserve the platform namespace for first-party marketplace tools.
-- Domain: marketplace / tenancy
-- Invariant: Community organisations cannot claim the platform/{tool} namespace.

-- NOT VALID preserves deployability if a legacy row already owns the slug while
-- enforcing the reservation for every new or updated organisation. Catalog
-- queries exclude such legacy rows until they are remediated.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_orgs_slug_reserved_platform'
          AND conrelid = 'orgs'::regclass
    ) THEN
        ALTER TABLE orgs
            ADD CONSTRAINT chk_orgs_slug_reserved_platform
            CHECK (slug <> 'platform') NOT VALID;
    END IF;
END;
$$;