-- Scheduling priority: when several eligible backends have free capacity,
-- pick_backend tries them in descending priority. Equal priorities keep the
-- profile's backend_ids order. Default 1 = baseline, so pre-existing rows
-- are unchanged.
ALTER TABLE backends ADD COLUMN priority INTEGER NOT NULL DEFAULT 1;
