# Architecture and schemas

## Components

```text
CLI/config → task catalog → isolated Git worktree → Agent interface
                                           ├─ LMStudioAgent → OpenAI-compatible API
                                           │                 └─ native model load API
                                           └─ future adapters (for example Cline)
                         Agent ↔ deterministic tools ↔ repository
                         → post-run evaluator → raw events/results → reports
```

The first implementation uses a small Python package and standard library for
the runtime. YAML task files are the extension point; bundled `.yaml` examples
use JSON syntax (valid YAML) so the core works offline, and optional PyYAML
enables idiomatic YAML. The provider is separate from the agent loop;
the agent is separate from tools and repository lifecycle; evaluation runs
after the agent has stopped. Before inference, the LM Studio provider uses the
native model-management API to load (or reuse) the profile's model at its
configured context length, then sends inference requests through the
OpenAI-compatible chat-completions API. Tool schemas and the system prompt are versioned
and hashed. A future adapter can implement the same `Agent` contract without
changing task/evaluation code.

## Task schema (YAML, schema version 1)

```yaml
schema_version: 1
id: refactor-orders-001
version: 1
category: refactoring # creation | refactoring | analysis
language: python
repository:
  path: ../../repositories/orders
  bundle: ../../repositories/orders.bundle # optional bootstrap for portable fixtures
  base_commit: "<full immutable Git commit>"
task:
  description: |-
    Refactor the order pricing module while preserving its externally visible behavior.
  acceptance_criteria:
    - Pricing uses the shared policy abstraction.
build:
  command: [python, -m, compileall, -q, src]
tests:
  public_command: [python, -m, unittest, discover, -s, tests]
  hidden_command: [python, -m, unittest, discover, -s, .benchmark_hidden]
  hidden_files:
    - source: hidden/test_acceptance.py
      destination: .benchmark_hidden/test_acceptance.py
evaluation:
  analysis_rubric: # analysis tasks only
    required_terms: [negative, quantity, calculate_total]
    required_paths: [pricing.py]
evaluation:
  require_clean_command_exit: true
  require_all_hidden_tests: true
limits:
  timeout_seconds: 900
  max_iterations: 30
  max_tool_calls: 100
  max_tokens: 32768
  allowed_commands: [python, git]
```

Paths are relative to the task file except `repository.path` and `repository.bundle`, which may be
absolute or relative to the task file. A missing repository path is cloned from
the optional Git bundle into the ignored local cache. Commands are argument arrays, never
shell strings. Hidden files live under the task directory and are copied into
a separate evaluation workspace only after the agent stops. The tool API does
not expose them, but an unrestricted host process could read parent directories;
use an OS/container sandbox with only the repository mounted for a true security
boundary. Do not put solution code, gold patches, or evaluator answers in the
task description/public metadata.

For analysis-only tasks, the final response is scored against deterministic
`required_terms` and `required_paths`. This baseline rubric is intentionally
simple; benchmark maintainers should use narrow, causal findings and locations,
and should not treat exact prose matching as a general natural-language judge.

## Run/result schema (JSON)

One `run.json` is written per run; `events.jsonl` is append-only. Required
top-level groups are:

```json
{
  "schema_version": 1,
  "run_id": "uuid",
  "experiment_id": "sha256-of-controlled-config",
  "status": "success | failure | timeout | iteration_limit | token_limit | tool_limit | infrastructure_error | evaluation_error",
  "success": false,
  "failure_class": "FAIL_TEST_FAILURE",
  "failure_source": "model | agent | tool | infrastructure | evaluation | null",
  "failure_details": {"stage": "commit_validation", "exception_type": "RuntimeError", "message": "...", "traceback": "..."},
  "started_at": "RFC3339 UTC",
  "duration_seconds": 0.0,
  "task": {"id": "...", "version": 1, "category": "creation", "base_commit": "..."},
  "model": {"profile": "...", "reported_name": "...", "revision": null, "quantization": null, "backend": null, "lmstudio_version": null, "lmstudio_instance_id": null, "loaded_context": null, "model_load_seconds": null},
  "controls": {"temperature": 0.2, "top_p": 1.0, "seed": null, "configured_context": 32768, "max_tokens": 32768, "system_prompt_sha256": "...", "tool_schema_sha256": "..."},
  "llm": {"requests": 0, "prompt_tokens": null, "completion_tokens": null, "total_tokens": null, "peak_context": null, "average_context": null, "final_context": null, "context_by_iteration": [], "ttft_seconds": null, "generation_seconds": null, "tokens_per_second": null, "telemetry_source": "provider_usage_and_client_timing"},
  "agent": {"iterations": 0, "tool_calls": 0, "successful_tool_calls": 0, "failed_tool_calls": 0, "commands_executed": 0, "files_read": 0, "files_modified": 0, "lines_added": 0, "lines_removed": 0, "self_corrected": false},
  "evaluation": {"build": null, "public_tests": null, "hidden_tests": null, "analysis": null, "exit_codes": {}},
  "repository": {"base_commit": "...", "patch_sha256": "...", "files_changed": [], "worktree": null},
  "host": {"os": "...", "cpu": null, "gpu": null, "vram_bytes": null, "ram_bytes": null}
}
```

Unavailable measurements are JSON `null`, never fabricated zeroes. Per-test
counts can be added under `evaluation` when a test adapter can reliably parse
them. Store full tool/API trajectories separately in JSONL, with timestamps,
iteration, event type, request/result, duration, and error. Secrets must be
redacted.

When a run raises or a required external command returns a failure,
`failure_details` records the runner stage, exception type, message, and
traceback. This distinguishes setup failures (for example, Git rejecting a
repository's ownership) from evaluator results; stages not reached remain
`null` and are not represented as test outcomes.

## Experiment protocol

1. Resolve a task and model profile; freeze all control settings and compute an
   experiment ID from canonical configuration plus task/version.
2. Request the LM Studio model load with the profile's `context_length`; record
   and verify the effective load configuration. Reuse an exact-context loaded
   instance; unload and reload only instances of the selected model whose
   context differs. The OpenAI-compatible chat-completions request does not set
   model context length.
3. For each independent trial, generate a run ID and create a detached Git
   worktree at the pinned commit. Fail closed if the commit is unavailable or
   the worktree is dirty/unexpected.
4. Give the model the fixed system prompt, task description, and identical
   tool schemas. Log every request and tool call. Enforce iteration, call,
   token, wall-clock, and per-command time limits.
5. Stop the agent, capture status/diff, and only then stage hidden tests and
   run the evaluator. Save stdout, stderr, exit codes, and test results.
6. Preserve artifacts and remove only the isolated worktree. Never reuse a
   prior run's directory. Do not run two models concurrently against one LM
   Studio server unless concurrency is explicitly the experimental variable.
7. Aggregate raw runs with success rate and descriptive distributions. Keep
   invalid infrastructure/evaluation runs identifiable and separate.

When a profile has a seed, it is the starting seed; trial `n` uses
`(starting_seed + n) mod 2^31`. Profiles with the same starting seed therefore
use paired seeds across models, while successive trials use distinct seeds.
Each effective seed is stored in the run record. Backends may ignore seeds.

## Failure taxonomy

`FAIL_TASK_UNDERSTANDING`, `FAIL_WRONG_FILE`, `FAIL_INCOMPLETE_IMPLEMENTATION`,
`FAIL_TEST_FAILURE`, `FAIL_REGRESSION`, `FAIL_TOOL_USAGE`, `FAIL_CONTEXT`,
`FAIL_TIMEOUT`, `FAIL_ITERATION_LIMIT`, `FAIL_TOKEN_LIMIT`, `FAIL_TOOL_LIMIT`,
`FAIL_MODEL_ERROR`, `FAIL_INFRASTRUCTURE`, `FAIL_EVALUATION`, and
`FAIL_UNKNOWN`. Classification is a diagnosis separate from primary success;
automated heuristics must be labeled as such and never imply a causal finding.

## Phase plan

1. Infrastructure: configs, LM Studio client, tool loop, Git isolation, raw logs.
2. Tasks: curated creation/refactoring/debugging tasks and analysis rubrics.
3. Evaluation: hidden tests, patch metadata, failure taxonomy.
4. Metrics: agent/provider metrics and optional host telemetry.
5. Reporting: JSON/CSV/HTML and context/token plots.
6. Validation: controlled multi-model trials, reset/isolation checks, data audit.

The initial implementation establishes phases 1–3 with a small bundled sample
task and tests. Hardware telemetry, Cline, statistical inference, and polished
HTML reporting are extension work, not claimed as completed validation.
