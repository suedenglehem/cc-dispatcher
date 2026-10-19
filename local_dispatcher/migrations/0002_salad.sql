-- Salad backend support: kind + the fields the daemon needs to run a local
-- Anthropic<->OpenAI proxy (salad_proxy.py) in front of the salad.cloud
-- gateway. All default to the "local" (direct) shape, so pre-existing rows
-- are unchanged.
ALTER TABLE backends ADD COLUMN kind TEXT NOT NULL DEFAULT 'local';
ALTER TABLE backends ADD COLUMN salad_keyfile TEXT;
ALTER TABLE backends ADD COLUMN salad_proxy_port INTEGER;
ALTER TABLE backends ADD COLUMN salad_thinking INTEGER NOT NULL DEFAULT 0;
ALTER TABLE backends ADD COLUMN salad_heartbeat INTEGER NOT NULL DEFAULT 1;
