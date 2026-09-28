# Methodology and threats to validity

## Design principles

This project evaluates a software-engineering agent as a system: model, prompt,
tool interface, execution limits, repository state, and evaluator all contribute
to the measured result. SWE-bench motivates repository-level issue resolution,
fixed base revisions, and acceptance/regression tests. SWE-agent shows that the
agent-computer interface affects performance. Agentless demonstrates that
carefully staged localization and patch validation can be competitive and that
benchmark issue descriptions and tests require scrutiny.

The primary outcome is binary task success for one run. A run succeeds only
when the benchmark evaluator passes all required hidden acceptance tests and
configured regression checks, with no infrastructure or evaluator error. A
model's task success rate is the number of successful runs divided by all
valid runs; infrastructure/evaluator errors are reported separately and are
not silently converted into model failures.

Each repeated trial starts from the same immutable commit in a fresh Git
worktree. Each run gets a unique ID, seed where supported, configuration hash,
event log, final diff, and evaluation record. Public tests and build commands
may be available to the agent. Hidden test sources are staged only after the
agent terminates. The agent sees only the task description, fixed system
prompt, tool schemas, and tool results.

## Task curation protocol

For every task, record the repository URL/path, immutable base commit, license,
language/runtime, task category, task version, and command environment. Write
acceptance criteria before implementation. Verify the public tests and build
on the base revision. Verify hidden acceptance tests fail on the unmodified
base where the task requires a behavioral change, and pass on a reference
solution. Check that tests describe the requested behavior rather than a
particular implementation. Have a human unfamiliar with the solution attempt
to understand and solve the task from the task description and repository.
Record any ambiguity and exclude or revise tasks that cannot be resolved
without hidden assumptions.

Creation tasks need tests for new behavior plus existing regression tests.
Refactoring tasks need behavior-preservation tests and, where relevant, a
separate structural/static check for the requested design change. Analysis-only
tasks need a machine-checkable rubric (required findings, locations, and
severity/causal relation); free-form subjective grading is not a primary score.

## Controlled variables

Within a comparison, hold task/version, base commit, system and task prompts,
tool schemas/implementations, evaluator, timeout, iteration/tool/token limits,
temperature, top-p, seed policy, and hardware/backend constant. Model name,
version, quantization, configured context, backend, and inference settings are
recorded for every run. Any intentional change to a controlled variable
defines a different experiment configuration. Run order should be randomized
or interleaved to reduce thermal, cache, and background-load effects.

For LM Studio, the provider applies the profile's configured context at model
load time through the native model-management API and records the context
confirmed by the server. If an instance of the selected model is already loaded
with a different context, that instance is unloaded and reloaded before the
trial. The LM Studio backend may round the request upward to a 512-token
boundary; `configured_context` and `loaded_context` preserve both values. The
OpenAI-compatible chat-completions endpoint itself does not set the model's
context length. `loaded_context` is model capacity; it must not be confused
with observed `peak_context`/prompt usage.

For stochastic models, use repeated independent trials. Report raw runs,
success rate with a binomial confidence interval, and descriptive statistics
(mean, median, standard deviation, min/max, and requested percentiles) for
continuous metrics. Do not rank on a combined score. Comparisons across a
small task set or few repetitions are exploratory, not evidence of statistical
significance.

## Metrics and provenance

Store raw provider usage separately from estimates. `configured_context` is
the context limit configured in the model profile; `peak_context` and related
values are observed prompt-token counts only when returned by the provider.
Missing measurements are null with an availability/source indicator. Do not
infer context use from the configured limit. For OpenAI-compatible LM Studio,
request usage fields when supported; performance details such as TTFT,
generation duration, and tokens/sec are measured by the client and identified
as client-side timings. Hardware utilization is optional telemetry and must
include its sampler/source.

Preserve tool request/result events, model request/response metadata, command
exit codes, test output, patch, repository commit, prompt/tool/config hashes,
OS/runtime/backend/model metadata, and failure classification. Redact API
credentials. Keep unmodified raw JSONL so reports can be regenerated.

## Threats to validity

* **Stochasticity:** a single run is noisy; repeated runs and confidence
  intervals are required. Seeds may be ignored by backends.
* **Task sample and contamination:** public GitHub issues, patches, and tests
  may be present in model training data. Prefer original, versioned local tasks
  and record provenance; no benchmark can prove non-contamination.
* **Task and test quality:** underspecified prompts, narrow tests, flaky tests,
  or incomplete acceptance criteria create false positives/negatives. Curate,
  baseline, and version every task/test suite.
* **Quantization and model identity:** quantization, GGUF conversion, model
  revisions, chat templates, and inference backends can change behavior. Record
  exact artifacts and LM Studio/runtime versions; compare quantizations within
  the same model family when possible.
* **Prompt and scaffold effects:** tool descriptions, loop policy, context
  compaction, and error formatting affect outcomes. Freeze and hash them.
* **Context measurement:** OpenAI-compatible endpoints may omit usage or
  performance fields. Missing telemetry must remain missing; loaded context
  capacity is not actual context use.
* **Hardware and thermal state:** GPU/CPU/RAM, drivers, offload, concurrent
  workloads, temperature, and power settings affect throughput. Record host
  configuration and interleave runs.
* **Caching and warm-up:** prompt/KV/model loading caches can affect timing.
  Define warm/cold protocol and do not mix them in one aggregate.
* **Isolation:** Git worktrees control source state, not operating-system
  security. Command execution must be restricted; for untrusted models or task
  code, run the entire benchmark worker in a disposable OS/container sandbox.
  Never expose secrets to the worker.
* **Analysis task scoring:** text-only analysis is hard to judge objectively.
  Use required, location-grounded findings and a deterministic rubric; report
  it separately from code-changing task success.
* **External validity:** a small curated set and a single machine measure this
  local setup, not all software work, deployments, hardware, or agent products.
* **Repeated-trial dependence:** model server state and thermal/load conditions
  can correlate runs; randomized interleaving and recorded run order reduce
  this risk but do not remove it.

## References

* Jimenez et al. (2024), [SWE-bench: Can Language Models Resolve Real-World
  GitHub Issues?](https://arxiv.org/abs/2310.06770).
* Yang et al. (2024), [SWE-agent: Agent-Computer Interfaces Enable Automated
  Software Engineering](https://arxiv.org/abs/2405.15793).
* Xia et al. (2024), [Agentless: Demystifying LLM-based Software Engineering
  Agents](https://arxiv.org/abs/2407.01489).
* OpenAI, [Introducing SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/)
  and [Why SWE-bench Verified no longer measures frontier coding
  capabilities](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/).
* LM Studio, [OpenAI-compatible tool use](https://lmstudio.ai/docs/developer/openai-compat/tools)
  and [API endpoint comparison](https://lmstudio.ai/docs/developer/rest).
