-- dedup_mode = "notify" (pack.toml): a pack may record an ok run whose config, code_ref and seed were already
-- recorded, and report the earlier run instead of refusing. The refusal used to be unconditional here (the two
-- unique indexes), so the schema needs a way to tell a declared repeat from an accidental duplicate.
SET search_path = benchbook;

-- A repeat names the run it repeats. Rows without duplicate_of stay under the unique indexes, so there is
-- still exactly one original per (project, dedup_key, seed) and every writer that does not declare a repeat
-- (all refuse-mode packs, and any direct INSERT) is refused exactly as before.
ALTER TABLE experiments ADD COLUMN duplicate_of uuid REFERENCES experiments(id);

DROP INDEX experiments_no_exact_dup;
DROP INDEX experiments_no_exact_dup_noseed;
CREATE UNIQUE INDEX experiments_no_exact_dup ON experiments (project_id, dedup_key, seed)
  WHERE run_status = 'ok' AND seed IS NOT NULL AND duplicate_of IS NULL;
CREATE UNIQUE INDEX experiments_no_exact_dup_noseed ON experiments (project_id, dedup_key)
  WHERE run_status = 'ok' AND seed IS NULL AND duplicate_of IS NULL;

-- duplicate_of cannot be used to slip past the indexes: it must name the original ok run of the same
-- project, key and seed (the one row the indexes allow), and only an ok run can be a repeat.
CREATE FUNCTION check_duplicate_of() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.run_status <> 'ok' OR NOT EXISTS (
       SELECT 1 FROM benchbook.experiments o
       WHERE o.id = NEW.duplicate_of AND o.project_id = NEW.project_id AND o.dedup_key = NEW.dedup_key
         AND o.seed IS NOT DISTINCT FROM NEW.seed AND o.run_status = 'ok' AND o.duplicate_of IS NULL) THEN
    RAISE EXCEPTION 'benchbook: duplicate_of % is not the original ok run with the same project, dedup_key and seed',
      NEW.duplicate_of USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER experiments_duplicate_of BEFORE INSERT ON experiments
  FOR EACH ROW WHEN (NEW.duplicate_of IS NOT NULL) EXECUTE FUNCTION check_duplicate_of();

-- A recorded repeat is logged as 'notified', so 'rejected' (v_dedup_savings, v_agent_stats) keeps meaning
-- "a record was refused".
ALTER TABLE dedup_hits DROP CONSTRAINT dedup_hits_kind_check;
ALTER TABLE dedup_hits ADD CONSTRAINT dedup_hits_kind_check CHECK (kind IN ('rejected','avoided','notified'));
