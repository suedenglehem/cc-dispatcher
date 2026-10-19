# Editing dispatcher backends

Two layers: the **config file** (persistent, read at daemon start) and the
**live daemon** (`ladd backend …`, runtime-only — a restart reasserts the file).

## Persistent — edit `~/.config/local-dispatcher/config.toml`

Add or change a `[[backends]]` block:

```toml
[[backends]]
id = "i7-local"
endpoint = "http://127.0.0.1:8080"
model_id = "qwen3.8-27b"        # id the server reports
model_label = "qwen3.8-27b"     # passed to `claude --model`
concurrency = 1
resource_group = "local"
# enabled = false               # exclude from scheduling
# is_primary = true             # reserve for the coordinating session
# manually_reserved = true      # exclude from worker jobs
# ctx_override = 110000         # skip the 90%-of-probe derivation
# credential_env = { ANTHROPIC_API_KEY = "..." }   # only if server has --api-key
```

The id must also be listed in the profile that should use it:

```toml
[[profiles]]
id = "cc-worker"
backend_ids = ["i7-local"]
```

Apply and verify:

```sh
systemctl --user restart local-dispatcher   # config is NOT hot-reloaded
ladd workers --refresh
ladd backend check <id>
```

## SaladCloud — the daemon edits config for you

```sh
ladd -a https://<group>.salad.cloud    # add (block + profile entry + local proxy)
    # --id --port --keyfile --model --thinking --concurrency --group --notes
ladd -d https://<group>.salad.cloud    # remove by URL
ladd backend remove-salad <id>         # remove by id
```

Persistent immediately; no restart needed.

## Live toggles — runtime only (restart reasserts the config file)

```sh
ladd backend list
ladd backend enable  <id>
ladd backend disable <id>
ladd backend reserve <id> --note "why"
ladd backend release <id>
ladd backend check   <id>      # force an immediate probe
ladd workers --refresh
ladd status
```

## Hosts

- **tr4**: `~/.config/local-dispatcher/config.toml`; restart with
  `systemctl --user restart local-dispatcher`.
- **i7**: same paths and commands. Current backend `i7-local` →
  `http://127.0.0.1:8080` (llama.cpp, alias `qwen3.8-27b`, running without
  `--api-key`).
