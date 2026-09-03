-- Persona usernames were only guarded by check-then-write SELECTs in the jobs
-- and api-core workers, so two concurrent creates could both pass the check
-- and commit the same username. The catalog now enforces global username
-- uniqueness at the authority itself; rows without a username (non-persona
-- apps) are exempt via the partial index.
CREATE UNIQUE INDEX IF NOT EXISTS cf_app_catalog_username_unique_idx
  ON cf_app_catalog(json_extract(data_json, '$.username'))
  WHERE json_extract(data_json, '$.username') IS NOT NULL;
