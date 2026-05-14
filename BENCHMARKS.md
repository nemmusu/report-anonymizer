# Model benchmarks

Head-to-head LLM comparison on a 5-PDF pentest corpus with 44
manually curated ground-truth values. Numbers drive the curated
preset list in [config/server_profiles.yml](https://github.com/nemmusu/report-anonymizer/blob/master/config/server_profiles.yml)
and the recommended-file ordering in
[anonymize/hf_models.py](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/hf_models.py).

## Score

**Quality score** = `F1 × 100`, rounded. F1 is the harmonic mean of
precision and recall: one number from 0 to 100, lower is worse.
Bands:

| Score | Meaning |
|---|---|
| 80 to 100 | Excellent. Catches almost every leak with few false alarms. |
| 65 to 79  | Good. Usable in production with light Review. |
| 50 to 64  | Usable. Expect real time in Review. |
| 40 to 49  | Poor. Misses too many leaks or floods Review. |
| 0 to 39   | Not recommended. No better than the regex baseline. |

Definitions: `precision = TP / (TP + FP)` (of flagged values, the
share that were real leaks). `recall = TP / (TP + FN)` (of real
leaks, the share the model caught). Accuracy is not reported because
the denominator (around 50 000 character offsets per PDF, mostly
non-leaks) dominates and produces deceptively high numbers (above
99 %).

## Methodology

- **Corpus.** 5 PDFs from a real pentest report. The benchmark
  harness reads the corpus location from the `BENCH_CORPUS_ROOT`
  env var so the repo never embeds a filesystem path. The corpus
  itself is not redistributed.
- **Ground truth.** 44 distinct customer-identifying values were
  manually curated and cross-checked three times. The list is kept
  alongside the corpus (under `$BENCH_CORPUS_ROOT/groundtruth.yml`),
  not in this repository, for the same privacy reasons as the
  corpus itself.
- **Pipeline.** Every profile is started fresh, then each PDF is
  processed via `anonymize-dossier all --force-rescan`.
- **Chunk strategy.** `structured`, the Markdown-aware splitter
  that never breaks tables, code fences or headings.
- **Metric.** The set of distinct `from` values applied
  (case-insensitive) is compared against the ground-truth set per
  PDF. The aggregate Quality score (and the underlying precision,
  recall and F1 it rolls up) is reported below.
- **Hardware.** Same machine for all runs (NVIDIA RTX 5090, 32 GB
  VRAM, Linux 6.17).
- **Memory.** Peak VRAM sampled from
  `nvidia-smi --query-gpu=memory.used` after each PDF. Captures the
  steady-state KV-cache plus weights footprint while serving
  requests.
- **Runner:** [`bench/run_precision_benchmark.py`](https://github.com/nemmusu/report-anonymizer/blob/master/bench/run_precision_benchmark.py).
- **Catalog patcher:** [`bench/apply_bench_to_catalog.py`](https://github.com/nemmusu/report-anonymizer/blob/master/bench/apply_bench_to_catalog.py).
  Reads the JSON sidecar produced by the runner and rewrites
  `CuratedRepo.benchmark_*` fields and preset-description VRAM
  numbers in place.

## Aggregate leaderboard (93 models)

| Column | What it means |
|---|---|
| **Quality** | F1 × 100 rounded. Sort key. |
| **TP** | Real leaks correctly anonymised. |
| **FP** | Wrongly flagged strings (over-detection). |
| **FN** | Real leaks missed. |
| **Precision** | `TP / (TP + FP)`. Higher = fewer false alarms. |
| **Recall** | `TP / (TP + FN)`. Higher = fewer missed leaks. |
| **F1** | Quality / 100; harmonic mean of precision and recall. |
| **Disk size** | GGUF size on disk. |
| **Peak VRAM** | Max GPU memory during serving (`nvidia-smi`). |
| **Total** | Wall-clock to anonymise the 5-PDF corpus end-to-end. |

### Curated set: usable models (Quality >= 50)

The catalog shows every model that scored in the Usable band or
better as a curated download with full benchmark numbers on the
card. The top 5 ship as built-in presets out of the box; the
others are reachable via the Model Manager free-text search or
the Curated downloads tab.

| # | Profile | **Quality** | Precision | Recall | F1 | Peak VRAM | Total |
|---|---|---|---|---|---|---|---|
| 🥇 | `ministral-3-8b-reasoning-bf16` | **83** | 75.5 % | **90.9 %** | **82.5 %** | 18 940 MB | 244 s |
| 🥈 | `rtila-qwen3.5-9b-q4` | 82 | 74.1 % | **90.9 %** | 82.0 % | **7 135 MB** | **79 s** |
| 🥉 ★ | `jackrong-qwen3.5-4b-distill-q4` | 78 | 79.1 % | 77.3 % | 78.0 % | **4 820 MB** | 185 s |
| 4 | `qwen3.5-9b-bf16` | 78 | **80.5 %** | 75.0 % | 77.7 % | 18 024 MB | 210 s |
| 5 | `ministral-3-8b-reasoning-q5` *(Q5_K_M)* | 76 | 65.6 % | **90.9 %** | 76.2 % | 9 171 MB | 112 s |
| 6 | `opus4.7-godsghost-codex-4b-q4` | 72 | 78.4 % | 65.9 % | 71.6 % | 4 798 MB | 155 s |
| 7 | `qwopus3.5-4b-v3-q4` | 71 | 76.3 % | 65.9 % | 70.7 % | 4 765 MB | 456 s |
| 8 | `opus4.7-godsghost-codex-4b-q4-mirror` *(WithinUsAI)* | 69 | 69.8 % | 68.2 % | 69.0 % | 4 785 MB | 155 s |
| 9 | `omnicoder-9b-q4` | 69 | 69.8 % | 68.2 % | 69.0 % | 6 673 MB | 113 s |
| 10 | `ministral-3-14b-reasoning-q4` | 67 | 57.0 % | 82.0 % | 67.0 % | 10 965 MB | 174 s |
| 11 | `omniclaw-qwen3.5-9b-uncensored-v2-q4` | 67 | 53.4 % | 88.6 % | 66.7 % | 7 069 MB | 260 s |
| 12 | `jackrong-qwen3.5-4b-distill-v2-q4` | 67 | 73.0 % | 61.4 % | 66.7 % | 4 833 MB | 198 s |
| 13 | `qwen3-4b-claude-sonnet-x-gemini-reasoning-iq4` | 65 | 56.9 % | 75.0 % | 64.7 % | 5 584 MB | 241 s |
| 14 | `ministral-3-8b-bf16` *(Instruct)* | 65 | 51.4 % | 86.4 % | 64.5 % | 18 768 MB | 214 s |
| 15 | `default` *(Jackrong 4B distill Q4 CPU)* | 64 | — | — | — | RAM | — |
| 16 | `qwen3-4b-thinking-minimax-m2.1-coder-q4` | 64 | 48.2 % | 93.2 % | 63.6 % | 5 708 MB | 382 s |
| 17 | `granite-4.1-8b-bf16` | 63 | 62.2 % | 63.6 % | 62.9 % | 19 908 MB | 100 s |
| 18 | `qwen3-space-agent-claude-uncensored-4b-q4` | 62 | 47.6 % | 90.9 % | 62.5 % | 5 662 MB | 177 s |
| 19 | `openthinker2-7b-q4` | 62 | 72.7 % | 54.5 % | 62.3 % | 6 412 MB | 437 s |
| 20 | `qwen3-4b-thinking-2507-minimax-m2.1-distill-q4` | 60 | 62.5 % | 56.8 % | 59.5 % | 5 783 MB | 396 s |
| 21 | `qwen3-4b-thinking-2507-gemini-3-pro-distill-q4` | 58 | 63.2 % | 54.5 % | 58.5 % | 5 861 MB | 188 s |
| 22 | `opensonnet-lite-q8` | 57 | 55.0 % | 59.0 % | 57.0 % | 7 416 MB | 393 s |
| 23 | `mistral-nemo-instruct-q4` | 56 | 48.0 % | 66.0 % | 56.0 % | 10 239 MB | 146 s |
| 24 | `jackrong-qwen3.5-0.8b-distill-q8` | 56 | 67.7 % | 47.7 % | 56.0 % | **2 903 MB** | 164 s |
| 25 | `qwen3.5-9b-deepseek-v4-flash-q4` | 55 | 82.0 % | 41.0 % | 55.0 % | 7 088 MB | 624 s |
| 26 | `qwen3-4b-2507-geminized-v1-q4` | 54 | 78.3 % | 40.9 % | 53.7 % | 5 846 MB | 424 s |
| 27 | `deepthink-reasoning-7b-q4` | 53 | 54.0 % | 52.0 % | 53.0 % | 6 449 MB | 240 s |

### Benchmarked but below the Usable cut (Quality < 50)

Reachable via the Model Manager free-text search; each entry shows a
⚠️ badge with the benchmark numbers and the reason it didn't make
the curated cut.

| # | Profile | **Quality** | Precision | Recall | F1 | Peak VRAM | Total |
|---|---|---|---|---|---|---|---|
| 28 | `unsloth/qwen3.5-2b-ud-q4-k-xl` | 49 | 65.4 % | 38.6 % | 48.6 % | 3 265 MB | 120 s |
| 29 | `liontix/qwen3-4b-sonnet-4-gpt-5-distill-q4` | 48 | 36.0 % | 70.5 % | 47.7 % | 5 684 MB | 147 s |
| 30 | `meta-llama-3-8b-instruct-q4` | 47 | 39.0 % | 61.0 % | 47.0 % | 7 437 MB | 124 s |
| 31 | `wavecoder-ultra-6.7b-iq4` | 47 | 54.5 % | 40.9 % | 46.8 % | 11 113 MB | 515 s |
| 32 | `within-us-coder-4b-q4` | 47 | 54.5 % | 40.9 % | 46.8 % | 4 762 MB | 157 s |
| 33 | `jackrong-qwen3.5-2b-distill-q4` | 46 | 82.4 % | 31.8 % | 45.9 % | 3 367 MB | 124 s |
| 34 | `unsloth/qwen3.5-0.8b-ud-q8-k-xl` | 44 | 47.4 % | 40.9 % | 43.9 % | 3 067 MB | 160 s |
| 35 | `opensonnet-lite-q4` | 42 | 35.0 % | 52.0 % | 42.0 % | 5 694 MB | 436 s |
| 36 | `darwin-2b-opus-q4` | 42 | 38.5 % | 45.5 % | 41.7 % | 3 128 MB | 106 s |
| 37 | `evelyn67/qwen3.5-2b-uncensored-q6` | 42 | 31.1 % | 63.6 % | 41.8 % | 3 400 MB | 186 s |
| 38 | `unsloth/qwen3.5-0.8b-ud-q4-k-xl` | 41 | 39.6 % | 43.2 % | 41.3 % | 2 530 MB | 144 s |
| 39 | `glm-4.6v-flash-q5` | 40 | 40.0 % | 41.0 % | 40.0 % | 8 532 MB | 80 s |
| 40 | `ministral-3-3b-reasoning-bf16` | 40 | 28.0 % | 68.2 % | 39.7 % | 9 610 MB | 141 s |
| 41 | `lfm-2.5-1.2b-f16` | 38 | 63.2 % | 27.3 % | 38.1 % | 3 880 MB | 119 s |
| 42 | `nvidia-agentic-coder-4b-q4` | 37 | 60.0 % | 27.3 % | 37.5 % | 4 253 MB | 33 s |
| 43 | `seed-coder-8b-reasoning-q4` | 36 | 55.0 % | 27.0 % | 36.0 % | 7 694 MB | 660 s |
| 44 | `agent-nano-coder-2b-q4` | 35 | 29.7 % | 43.2 % | 35.2 % | 4 102 MB | 543 s |
| 45 | `deepseek-coder-6.7b-f16` | 34 | 44.4 % | 27.3 % | 33.8 % | 22 221 MB | 339 s |
| 46 | `nemotron-3-nano-4b-q4` | 34 | 44.4 % | 27.3 % | 33.8 % | 4 436 MB | 40 s |
| 47 | `qwen3-4b-reasoning-slerp-q8` | 34 | 66.7 % | 22.7 % | 33.8 % | 8 172 MB | 387 s |
| 48 | `olympiccoder-7b-q4` | 33 | 81.8 % | 20.5 % | 32.7 % | 6 412 MB | 453 s |
| 49 | `ibm-opus4.7-obscure-reasoner-3b-q4` | 32 | 75.0 % | 20.5 % | 32.1 % | 4 452 MB | 112 s |
| 50 | `skywork-or1-7b-preview-q4` | 32 | 38.7 % | 27.3 % | 32.0 % | 6 412 MB | 219 s |
| 51 | `rikunarita-2-qwen3.5-2b-claude-opus-v2-q5-imat` | 31 | 24.7 % | 43.2 % | 31.4 % | 3 261 MB | 146 s |
| 52 | `llama-3.2-3b-instruct-q5` | 31 | 23.0 % | 46.0 % | 31.0 % | 5 172 MB | 196 s |
| 53 | `llama-3.2-3b-reason-reflect-lite-q4` | 31 | 60.0 % | 20.0 % | 31.0 % | 4 797 MB | 35 s |
| 54 | `magistral-small-2507-q4` *(24B)* | 30 | 80.0 % | 18.0 % | 30.0 % | 16 782 MB | 698 s |
| 55 | `mythoseek-q4` | 30 | 29.2 % | 31.8 % | 30.4 % | 7 139 MB | 533 s |
| 56 | `llada-moe-7b-q4` | 30 | 80.0 % | 18.2 % | 29.6 % | 5 687 MB | 22 s |
| 57 | `smollm2-135m-instruct-q4` | 30 | 80.0 % | 18.2 % | 29.6 % | 1 690 MB | 70 s |
| 58 | `zeta-q4` | 30 | 80.0 % | 18.2 % | 29.6 % | 6 410 MB | 185 s |
| 59 | `smollm3-3b-q4` | 30 | 22.9 % | 43.2 % | 29.9 % | 4 241 MB | 272 s |
| 60 | `opencoder-1.5b-instruct-q4` | 30 | 80.0 % | 18.2 % | 29.6 % | 5 134 MB | 22 s |
| 61 | `openhands-lm-1.5b-q4` | 30 | 80.0 % | 18.2 % | 29.6 % | 2 881 MB | 168 s |
| 62 | `openreasoning-nemotron-1.5b-q4` | 30 | 80.0 % | 18.2 % | 29.6 % | 2 882 MB | 281 s |
| 63 | `llama-coyote-coder-4b-q4` | 30 | 80.0 % | 18.2 % | 29.6 % | 8 140 MB | 218 s |
| 64 | `qwenseek-2b-bf16` | 30 | 23.9 % | 38.6 % | 29.5 % | 5 597 MB | 175 s |
| 65 | `opus-1.5-q4` | 30 | 80.0 % | 18.2 % | 29.6 % | 2 417 MB | 22 s |
| 66 | `deecon-securityanalyst-1.5b-q8` | 30 | 80.0 % | 18.2 % | 29.6 % | 3 692 MB | 56 s |
| 67 | `deepseek-r1-opus-q8` | 30 | 80.0 % | 18.2 % | 29.6 % | 3 914 MB | 208 s |
| 68 | `cicikus-v3-1.4b-opus4.6-q8` | 30 | 80.0 % | 18.2 % | 29.6 % | 3 778 MB | 99 s |
| 69 | `qwen3-zero-coder-reasoning-v2-0.8b-f16` | 30 | 43.5 % | 22.7 % | 29.9 % | 5 338 MB | 135 s |
| 70 | `qwen-researcher-f16` | 29 | 72.7 % | 18.2 % | 29.1 % | 2 784 MB | 148 s |
| 71 | `qwen3-4b-thinking-2507-q4` *(MaziyarPanahi)* | 29 | 66.7 % | 18.2 % | 28.6 % | 5 676 MB | 512 s |
| 72 | `wizardlm-2-7b-q4` | 29 | 25.0 % | 34.1 % | 28.8 % | 7 028 MB | 280 s |
| 73 | `deepseek-r1-distill-qwen-1.5b-ud-q4` | 27 | 33.3 % | 22.7 % | 27.0 % | 2 885 MB | 85 s |
| 74 | `security-slm-unsloth-1.5b-f16` | 27 | 50.0 % | 18.2 % | 26.7 % | 3 172 MB | 217 s |
| 75 | `falcon3-3b-instruct-q4` | 26 | 20.5 % | 34.1 % | 25.6 % | 4 332 MB | 130 s |
| 76 | `zr1-1.5b-q4` | 26 | 31.2 % | 22.7 % | 26.3 % | 2 866 MB | 130 s |
| 77 | `lfm2.5-1.2b-thinking-pony-alpha-distill-q4` | 26 | 37.5 % | 20.5 % | 26.5 % | 2 302 MB | 142 s |
| 78 | `bonsai-8b-q1_0` *(experimental)* | 23 | 16.8 % | 38.6 % | 23.4 % | 4 373 MB | 393 s |
| 79 | `cogito-v1-preview-llama-3b-q4` | 23 | 14.6 % | 52.3 % | 22.8 % | 4 834 MB | 311 s |
| 80 | `gemma-3-4b-opus-reasoning-distill-q4` | 21 | 17.0 % | 25.0 % | 21.0 % | 4 618 MB | 522 s |
| 81 | `ernie-4.5-0.3b-q4` | 21 | 22.0 % | 20.5 % | 21.2 % | 1 917 MB | 14 s |
| 82 | `exaone-4.0-1.2b-q4` | 21 | 17.5 % | 25.0 % | 20.6 % | 2 970 MB | 76 s |
| 83 | `jairodanielmt/qwen3-1.7b-opus-finetune-q4` | 21 | 19.2 % | 22.7 % | 20.8 % | 3 962 MB | 240 s |
| 84 | `qwen2.5-coder-3b-q4` | 19 | 18.0 % | 20.5 % | 19.1 % | 3 864 MB | 261 s |
| 85 | `jackrong-qwen3-1.7b-gemini-3-pro-distill-q4` | 19 | 16.4 % | 22.7 % | 19.0 % | 4 029 MB | 319 s |
| 86 | `teichai-qwen3-1.7b-gemini-2.5-flash-lite-distill-f16` | 19 | 12.6 % | 36.4 % | 18.7 % | 6 391 MB | 287 s |
| 87 | `aya-23-8b-iq4` | 15 | 9.2 % | 43.2 % | 15.2 % | 7 907 MB | 428 s |
| 88 | `ibm-grok4-ultra-fast-coder-1b-q4` | 14 | 10.3 % | 22.7 % | 14.2 % | 3 410 MB | 231 s |
| 89 | `mradermacher-qwen3-0.6b-claude-opus-distill-q4` | 14 | 9.6 % | 25.0 % | 13.8 % | 3 280 MB | 159 s |
| 90 | `llama3.2-agent-hermes-coder-3b-q4` | 4 | 2.6 % | 13.6 % | 4.4 % | 4 820 MB | 402 s |

### Architecturally or behaviourally incompatible

These collapse to the Tier-0 deterministic regex baseline (around
30 out of 100) because their LLM never produces usable JSON
candidates. Marked ❌ in the Model Manager. Different root causes:

| Profile | **Quality score** | Root cause |
|---|---|---|
| gemma-4-e4b-it-bf16 | 30 / 100 | Gemma 4 SWA-1024 ¹ |
| gemma-4-e2b-it-bf16 | 30 / 100 | Same SWA-1024 ¹ |
| qwen3guard-gen-4b-f16 | 30 / 100 | Safety-tuned model refuses arbitrary-JSON tasks |
| qwen3guard-gen-8b-f16 | 30 / 100 | Same as 4B |
| hy-mt1.5-1.8b-2bit | n/a | 2-bit GGUF quantisation `tensor type 2` not supported by the bundled llama.cpp build (`load_model: failed to load model`); model never starts, so quality is undefined. Recompile llama.cpp with the appropriate `-DGGML_…` flags or pick a Q4_K_M / Q8_0 quant of the same model when one is published. |

<sub>¹ Gemma 4 architecture uses Sliding Window Attention (SWA, 1024-token window on 20 of 24 layers, visible in `llama-server`'s ``creating SWA KV cache, size = 1024 cells, 20 layers``). Our `system_detector.txt` is ~3700 tokens; SWA layers only see the last 1024, which drops the JSON-output instructions. Manual short-prompt tests succeed; the long detector prompt does not. Switching the chat template (peg-native vs. peg-gemma4) does not help: the limitation is structural, not template-related.</sub>

## How to pick

Pick the row that matches your hardware. The "Why" column is the
trade-off you are accepting.

| Hardware or goal | Pick | Why |
|---|---|---|
| 18 GB VRAM or more, top quality | `ministral-3-8b-reasoning-bf16` | Quality 83, catches around 9 out of every 10 leaks. |
| 18 GB VRAM or more, fewest false alarms | `qwen3.5-9b-bf16` | Precision 80.5 %. Lower recall, so a few more leaks reach Review. |
| Around 7 GB VRAM, near-leader quality | `rtila-qwen3.5-9b-q4` | Quality 82 at one-third the VRAM of the BF16 leader and the fastest run on the corpus (79 s). |
| Around 6 GB VRAM, smallest "good" model **★ recommended** | `jackrong-qwen3.5-4b-distill-q4` | Quality 78 at 2.5 GB on disk. Best small + good pick on the curated set, and the second-best precision (79.1 %, behind `qwen3.5-9b-bf16` at 80.5 % which needs almost 4x the VRAM). Recommended starting point on any GPU. |
| Around 10 GB VRAM, reasoning quality | `ministral-3-8b-reasoning-q5` | Quality 76 at half the VRAM of the BF16 leader. Recall matches BF16; precision drops about 10 points. |
| No GPU | `default` | The shipped CPU profile. Same Jackrong Qwen 3.5 4B Q4_K_M weights as `jackrong-qwen3.5-4b-distill-q4`, just configured with `n_gpu_layers: 0` (Quality 78 at around 2.5 GB on disk, expect roughly 10x slower than the GPU run). |
| Smallest GGUF, quality is not a priority | `jackrong-qwen3.5-0.8b-distill-q8` | 0.8 GB on disk, Quality 56 (below the curated cut but the best of the sub-1 GB tier; Peak VRAM 2.9 GB). |

The reasoning models are fed `enable_thinking: false` so they emit
JSON directly, without burning the token budget on `<think>` blocks.

## Why these context sizes

The pipeline is **chunked**: the detector splits each input segment
into ~5000-character chunks via the structure-aware chunker
([anonymize/structure_chunker.py](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/structure_chunker.py))
and the LLM only ever sees one chunk per request. Measured request
size (calibrated against the real prompts):

| Component | Tokens (measured) |
|---|---|
| `system_detector.txt` (12 748 chars at ~3.5 char/tok) | ~3 700 |
| Few-shot examples (top 8 from `decisions_history.jsonl`) | ~250 |
| Chunk body (5000 chars worst case) | ~1 430 |
| Output JSON budget (`max_tokens` in `LLMClient`) | 2 048 |
| **Worst-case per request** | **~7 430** |

That sets a floor: **slot ≥ 7 430 tokens** (`slot = ctx_size /
parallel`). The pre-flight check in
[anonymize/budget.py](https://github.com/nemmusu/report-anonymizer/blob/master/anonymize/budget.py) refuses any preset that
violates it; the unit test in
[`tests/test_preset_budget.py`](https://github.com/nemmusu/report-anonymizer/blob/master/tests/test_preset_budget.py) keeps
the curated catalog honest.

Slot status of the shipped presets (matches
[config/server_profiles.yml](https://github.com/nemmusu/report-anonymizer/blob/master/config/server_profiles.yml)):

| Preset | ctx | parallel | slot | required | headroom | fits |
|---|---|---|---|---|---|---|
| `default` *(Jackrong Qwen 3.5 4B Q4_K_M, CPU)* | 12 288 | 1 | 12 288 | ~7 430 | +4 858 | ✓ |
| `jackrong-qwen3.5-4b-distill-q4` | 12 288 | 1 | 12 288 | ~7 430 | +4 858 | ✓ |
| `rtila-qwen3.5-9b-q4` | 12 288 | 1 | 12 288 | ~7 430 | +4 858 | ✓ |
| `ministral-3-8b-reasoning-bf16` | 16 384 | 2 | 8 192 | ~7 430 | +762 | ✓ |
| `ministral-3-8b-reasoning-q5` *(Q5_K_M)* | 16 384 | 2 | 8 192 | ~7 430 | +762 | ✓ |
| `qwen3.5-9b-bf16` | 16 384 | 1 | 16 384 | ~7 430 | +8 954 | ✓ |

## Same-model quant comparison

Different quants of the same base model, same corpus.

| Model | Quant | F1 | Δ vs. reference |
|---|---|---|---|
| Qwen 3.5 4B | BF16 | 63.8 % | reference |
| Qwen 3.5 4B | Q4_K_XL | 58.2 % | -5.6 pts |
| Ministral 3 8B Instruct | BF16 | 64.5 % | reference |
| Ministral 3 8B Instruct | Q8_K_XL | 64.4 % | within noise |
| Ministral 3 8B Reasoning | BF16 | **82.5 %** | reference |
| Ministral 3 8B Reasoning | Q5_K_M | 76.2 % | -6.3 pts (recall identical, precision -10 pts, around half the VRAM) |
| Granite 4.1 8B | BF16 | 62.9 % | reference |
| Granite 4.1 8B | Q8_K_XL | 65.3 % | +2.4 pts (recall lower, 70.5 % vs 86.4 %) |
| OpenSonnet Lite | Q8_0 | 57.0 % | reference |
| OpenSonnet Lite | Q4_K_M | 42.0 % | -15 pts (heavy precision drop) |

Rule of thumb: BF16 wins same-model comparisons, and the gap widens
on smaller quants. KV cache stays at f16 across the board.

## Distill vs. base model

Some Q4_K_M *distills* outscore the BF16 build of their base
model. Different training objective, not just a different quant.

| Base model | Build | Quant | F1 | Δ vs. base BF16 |
|---|---|---|---|---|
| Qwen 3.5 9B | base (unsloth) | BF16 | 77.7 % | reference |
| Qwen 3.5 9B | rtila Assistant Lite | Q4_K_M | 82.0 % | **+4.3 pts** at around 7 GB VRAM (vs 18 GB for the base) |
| Qwen 3.5 9B | Jackrong Claude-Opus distill | Q4_K_M | 76.0 % | -1.7 pts (within noise) at around 7 GB |
| Qwen 3.5 4B | base (unsloth) | BF16 | 63.8 % | reference |
| Qwen 3.5 4B | Jackrong Claude-Opus distill | Q4_K_M | 78.0 % | **+14.2 pts** at 2.5 GB on disk |

Practical takeaway: when picking a small model, prefer a
purpose-trained distill of a strong base over the base in BF16. The
distill captures the JSON-output discipline the anonymizer needs at
a fraction of the VRAM.

## Reproducing

You need a corpus folder of your own (the 5-PDF corpus used here is
private). Point `BENCH_CORPUS_ROOT` at any folder of PDFs you have
ground truth for, then pick a folder for the run output (anywhere
on disk, the path below uses a `bench_runs/` directory next to the
repo):

```bash
export BENCH_CORPUS_ROOT=/path/to/your/pdfs
OUT=./bench_runs/precision_top5

# Run the benchmark for the curated presets.
PYTHONPATH=$(pwd) QT_QPA_PLATFORM=offscreen \
    .venv/bin/python bench/run_precision_benchmark.py \
    --profiles default \
               jackrong-qwen3.5-4b-distill-q4 \
               rtila-qwen3.5-9b-q4 \
               ministral-3-8b-reasoning-bf16 \
               ministral-3-8b-reasoning-q5 \
               qwen3.5-9b-bf16 \
    --out-root "$OUT"

# Patch the catalog + presets with the measured numbers.
PYTHONPATH=$(pwd) .venv/bin/python bench/apply_bench_to_catalog.py \
    "$OUT/report.json"
```

The runner emits both `report.md` (human-readable, with per-PDF
breakdown plus miss and extra lists) and `report.json`
(machine-readable, consumed by `apply_bench_to_catalog.py`).

## Per-PDF breakdown

The runner writes a per-profile, per-PDF report into
`<out-root>/<profile>/`, with miss and extra lists for every PDF
and a top-level `report.md` summarising the run.
