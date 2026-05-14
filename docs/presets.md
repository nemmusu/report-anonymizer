# Server presets

Shipped presets live in
[`config/server_profiles.yml`](https://github.com/nemmusu/report-anonymizer/blob/master/config/server_profiles.yml).
User additions go to `~/.config/document-anonymizer/server.yml`.
Per-project overrides go to `<output_dir>/.anon/server.yml`.
Resolution order: builtin ➜ user ➜ project (last wins).

## Built-in catalog

| Preset | Model | Quality | Ctx | Parallel | Disk | VRAM |
|---|---|---|---|---|---|---|
| `default` ★ | Qwen 3.5 4B Claude-Opus distill Q4_K_M (**CPU**) | 78 | 12 K | 1 | 2.5 GB | ~4.8 GB (**RAM**) |
| `ministral-3-8b-reasoning-bf16` | Ministral 3 8B Reasoning BF16 | **83** | 16 K | 2 | 16.0 GB | ~18.9 GB |
| `rtila-qwen3.5-9b-q4` | rtila Assistant Lite · 9B Q4_K_M | 82 | 12 K | 1 | 5.2 GB | ~7.1 GB |
| `jackrong-qwen3.5-4b-distill-q4` ★ | Qwen 3.5 4B Claude-Opus distill Q4_K_M (GPU) | 78 | 12 K | 1 | 2.5 GB | ~4.8 GB |
| `qwen3.5-9b-bf16` | Qwen 3.5 9B BF16 | 78 | 16 K | 1 | 18.4 GB | ~18.0 GB |
| `ministral-3-8b-reasoning-q5` | Ministral 3 8B Reasoning Q5_K_M | 76 | 16 K | 2 | 5.8 GB | ~9.2 GB |

Quality column = F1 × 100 from the 5-PDF pentest corpus.
`default` and `jackrong-qwen3.5-4b-distill-q4` share the same GGUF
file; the only difference is `n_gpu_layers` (0 vs 99). Same model,
two runtime configs.

## How to choose

| Hardware / goal | Pick |
|---|---|
| 18+ GB VRAM, max quality | `ministral-3-8b-reasoning-bf16` |
| 18+ GB VRAM, fewest false alarms | `qwen3.5-9b-bf16` |
| ~7 GB VRAM, near-leader quality | `rtila-qwen3.5-9b-q4` |
| ~6 GB VRAM, smallest "good" model **★ recommended** | `jackrong-qwen3.5-4b-distill-q4` |
| ~10 GB VRAM, reasoning quality | `ministral-3-8b-reasoning-q5` |
| No GPU | `default` (Jackrong 4B distill Q4 on CPU) |

To pin a default for repeatable runs use **★ Set as default** in the
preset gallery, it persists to
`~/.config/document-anonymizer/preferences.yml`.

## Server lifecycle (sidebar dot + auto-start)

The sidebar **Server** entry shows a small status dot:

- 🔴 **red** = offline
- 🟡 **amber** = starting (worker spawning, health endpoint warming up)
- 🟢 **green** = online

Click the dot to bring the active preset up. A pre-flight check
runs first; on failure you're routed to the Server tab with a
specific error dialog (missing binary, model not on disk, docker
not in PATH, external server unreachable, etc.). When the server
is already up or in flight the click just opens the Server tab.

The Server panel also exposes a persistent **Auto-start server on
launch** toggle. Stored in
`~/.config/document-anonymizer/app_settings.yml`. Pre-flight
applies here too (silent skip when the active preset can't start,
no startup-time error dialog).

## Customising

1. Open the **Server** view → **Customize** on any card.
2. Edit any field (cmd-line preview updates live).
3. **Save** → choose **user** or **project** scope.

Common tweaks:

- **More parallel slots**: `parallel: 4` cuts wall time on long
  scans, at the cost of `ctx_size / parallel` per slot. Use the
  pre-flight token check (Pipeline → Run) to verify the slot still
  fits a ~7 500-token request.
- **Bigger ctx**: bump `ctx_size` to 32 K if you increase the
  detector's `max_chunk_chars` past 5 000 (rare).
- **Different binary**: point `binary` at your own llama.cpp build.

## Quantisation policy

We ship both BF16 and quantised presets when the quant clears the
quality bar. See [BENCHMARKS.md](https://github.com/nemmusu/report-anonymizer/blob/master/BENCHMARKS.md) for same-model
BF16 vs. Q5_K_M / Q4_K_XL / Q8_K_XL deltas. KV cache stays at f16
across the board.
