// The shapes the server returns, field for field. A field is nullable only where the
// server can send null, and optional only where the server sometimes leaves it out.

export type AgentRunStatus = 'in_progress' | 'completed'

export type AgentLiveState = 'queued' | 'running' | 'completed'

// What a run's agents wrote: the implementation for the task's tests, or tests for its implementation.
export type AgentTaskKind = 'implementation' | 'test_generation'

// What a finding means for the grade: the task cannot be trusted, the harness failed,
// or it is recorded for the audit and the task is graded normally.
export type AgentSeverity = 'security' | 'infra' | 'warning'

export type TokenUsage = {
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
}

export type AgentRunSummary = {
  run_id: string
  run_name: string
  status: AgentRunStatus
  created_at_utc: string
  finished_at_utc: string | null
  num_tasks: number
  gradable_tasks: number
  untrusted_tasks: number
  syntax_passed: number
  compile_passed: number
  tests_passed: number
  syntax_pass_rate: number
  compile_pass_rate: number
  tests_pass_rate: number
  usage: TokenUsage
  usage_complete: boolean
  total_usd_cost: number | null
  run_wall_seconds: number | null
  agent_wall_seconds: number
  run_egress: {
    domains_observed: string[]
    blocked_domains: string[]
    non_allowed_domains: string[]
    request_count: number
    unattributed_request_count: number
  }
  // Finding flag -> the tasks it was recorded on.
  flagged: Record<string, string[]>
  // All zero for an implementation run.
  test_generation: {
    mutants_caught: number
    mutants_total: number
    mutant_catch_rate: number
    mean_task_catch_rate: number
    tests_pass_on_correct_code: number
    real_bugs_caught: number
    real_bugs_total: number
    // Some task graded fewer mutants than it has: a quick development run, not comparable to a full one.
    mutants_capped: boolean
  }
}

export type AgentTaskStatusCode =
  | 'queued'
  | 'running'
  | 'success'
  | 'usage_limit'
  | 'tests_failed'
  | 'build_failed'
  | 'parse_failed'
  | 'infra_failed'
  | 'security_violation'
  | 'other_error'

// A test-generation task's mutants: how many its tests caught, of how many were built and how many it has.
export type AgentTaskMutants = {
  caught: number
  built: number
  total: number
  // Why none was built, e.g. the test file does not compile; null when they were.
  skipped: string | null
}

export type AgentTaskCell = {
  task_id: string
  status_code: AgentTaskStatusCode
  mutants: AgentTaskMutants | null
  timed_out: boolean
  tests_succeeded: number
  tests_total: number
  usd_cost: number | null
  usd_cost_is_lower_bound: boolean
  input_tokens: number | null
  cached_input_tokens: number | null
  output_tokens: number | null
  input_cost: number | null
  cached_input_cost: number | null
  output_cost: number | null
}

export type AgentMatrixRunItem = {
  run_id: string
  archived: boolean
  task_kind: AgentTaskKind
  summary: AgentRunSummary
  task_statuses: Record<string, AgentTaskCell>
  // Only the statuses at least one task has.
  status_counts: Partial<Record<AgentTaskStatusCode, number>>
}

export type AgentMatrixResponse = {
  task_kind: AgentTaskKind
  task_ids: string[]
  task_header_colors: Record<string, string>
  // Every finding flag the benchmark can record, and its severity.
  task_flags: Record<string, AgentSeverity>
  // The tasks that ship with the package; the rest come from task lists added with `locations.configure()`.
  package_task_ids: string[]
  items: AgentMatrixRunItem[]
}

export type AgentFinding = {
  flag: string
  detail: string
}

export type AgentGrade = {
  syntax_passed: boolean
  compile_passed: boolean
  tests_passed: boolean
  test_results: Record<string, boolean>
  syntax_error: string | null
  compile_error: string | null
  tests_error: string | null
  // A test-generation grade also carries its mutants; an implementation grade does not have these.
  mutants?: AgentMutantResult[]
  mutants_total?: number
  real_bugs_total?: number
}

export type AgentMutantResult = {
  id: string
  source: 'llm' | 'real-bug'
  kind: string
  grade: AgentGrade
}

// The agent's scripts against the correct code and each mutant, as the task view draws it.
export type AgentMutantMatrix = {
  scripts: string[]
  correct: Record<string, boolean>
  skipped: string | null
  total: number
  rows: {
    id: string
    source: 'llm' | 'real-bug'
    kind: string
    caught: boolean
    // Null when the test file no longer built against the mutant.
    results: Record<string, boolean | null> | null
    error: string | null
  }[]
}

export type AgentFileChange = {
  path: string
  kind: 'added' | 'modified' | 'deleted' | 'unknown'
}

export type AgentSubAgent = {
  thread_id: string
  prompt: string
  model: string | null
  reasoning_effort: string | null
  model_verified: boolean
  reasoning_effort_verified: boolean
}

export type AgentRuntimeIdentity = {
  model_expected: string
  model_reported: string | null
  model_verified: boolean
  reasoning_effort_expected: string | null
  reasoning_effort_reported: string | null
  reasoning_effort_verified: boolean
  model_provider: string | null
  cli_version: string | null
}

export type AgentThreadUsage = {
  usage: TokenUsage
  usage_reported: boolean
  turn_completed: boolean
}

export type AgentForbiddenToolCall = {
  type: 'web_search' | 'mcp_tool_call' | 'dynamic_tool_call' | 'sub_agent_activity'
  id: string | null
  query: string | null
  server: string | null
  tool: string | null
  event_type: 'item.started' | 'item.updated' | 'item.completed' | null
}

export type AgentSuspiciousCommand = {
  id: string | null
  command: string
  matched: string
}

export type AgentEgressSummary = {
  // One entry per request the proxy logged, in the proxy's own shape.
  events: Array<Record<string, unknown>>
  domains_observed: string[]
  blocked_domains: string[]
  non_allowed_domains: string[]
  allowed_domain_suffixes: string[]
  // Hosts allowed by exact name, without their subdomains.
  allowed_hosts: string[]
}

export type AgentWorkspaceAudit = {
  available: boolean
  target_file_changes: AgentFileChange[]
  protected_file_changes: AgentFileChange[]
  source_changes_outside_targets: AgentFileChange[]
  build_outputs: AgentFileChange[]
  codex_home_changes: AgentFileChange[]
  other_changes: AgentFileChange[]
}

// One file the container wrapper saw change, with the diff it took at that moment.
export type AgentFileChangeDiff = {
  path: string
  kind: string
  before_missing: boolean
  after_missing: boolean
  before_snapshot: string | null
  after_snapshot: string | null
  diff_unified: string | null
}

// One Codex notification as the driver recorded it. `event` is the canonical form the
// audit reads and `raw_event` the notification as received; both are Codex's own shapes.
export type AgentStdoutEvent = {
  line_no: number
  captured_at_utc: string
  schema_version: string
  source_transport: string
  event: Record<string, unknown>
  raw_event: Record<string, unknown>
  // Only on an event that changed files.
  file_change_diffs?: AgentFileChangeDiff[]
}

export type AgentAttempt = {
  command: string[]
  model: string
  approval_policy: string
  event_schema_version: string
  final_message: string | null
  sub_agents: AgentSubAgent[]
  returncode: number
  wall_seconds: number
  timed_out: boolean
  export_timed_out: boolean
  quota_limit_detected: boolean
  quota_retry_count: number
  usage: TokenUsage | null
  usage_by_thread: Record<string, AgentThreadUsage>
  usage_complete: boolean
  usd_cost: number | null
  // The output sidecars; null when the record was read without them.
  stdout: string | null
  stderr: string | null
  stdout_events: AgentStdoutEvent[] | null
  forbidden_tool_calls: AgentForbiddenToolCall[]
  suspicious_commands: AgentSuspiciousCommand[]
  out_of_workspace_writes: AgentFileChange[]
  egress: AgentEgressSummary
  runtime_identity: AgentRuntimeIdentity | null
  workspace_audit: AgentWorkspaceAudit
}

export type AgentGroundTruthControl = {
  grade: AgentGrade
  cached: boolean
  image_id: string
  evaluated_at_utc: string
  build_outputs_removed: number | null
}

export type AgentRepoCopyIntegrity = {
  ok: boolean
  offending: Array<Record<string, unknown>>
  partial_matches: Array<Record<string, unknown>>
  git_entries: string[]
  symlinks_outside_repo_copy: Array<Record<string, unknown>>
  targets_checked: string[]
  targets_skipped_too_short: string[]
  files_scanned: number
  bytes_scanned: number
  elapsed_seconds: number
  repo_copy_digest: string
  pruned_archives: Array<{ path: string; entries: string[] }>
}

export type AgentAnswerFileSnapshot = {
  answer_file: string
  path_in_copy: string
  original: string | null
  generated: string | null
  captured_at_utc: string
}

export type AgentTask = {
  task_id: string
  task_safe_name: string
  test_file: string
  impl_files: string[]
  live_state: AgentLiveState
  queued_at_utc: string | null
  started_at_utc: string | null
  finished_at_utc: string | null
  live_updated_at_utc: string | null
  repo_copy: string | null
  findings: AgentFinding[]
  // Null until the task has run.
  grade: AgentGrade | null
  ground_truth_control: AgentGroundTruthControl | null
  repo_copy_integrity: AgentRepoCopyIntegrity | null
  answer_file_snapshots: AgentAnswerFileSnapshot[]
  attempts: AgentAttempt | null
}

// The kinds of event the task's cost is split into.
export type AgentCostCategory =
  | 'user_message'
  | 'reasoning'
  | 'agent_message'
  | 'file_change'
  | 'command_execution'
  | 'cache_miss'
  | 'unattributed'

// What one completed item cost over the whole task. Every cost is null when the model has no price.
export type AgentItemCost = {
  // Tokens it put into the conversation: what the model wrote plus any tool result.
  tokens: number
  written_tokens: number
  written_usd: number | null
  first_send_usd: number | null
  // How many later requests sent it again from cache.
  resends: number
  resend_usd: number | null
  total_usd: number | null
  category: AgentCostCategory
}

// One request. The miss is what the request paid fresh for tokens that should have been cached.
export type AgentRequestCost = {
  index: number
  line_no: number
  // A request of another thread, which is not split over events.
  sub_agent: boolean
  fresh: number
  cached: number
  out: number
  usd: number | null
  miss_tokens: number
  miss_usd: number | null
  unattributed_tokens: number
  unattributed_usd: number | null
}

export type AgentCategoryCost = {
  key: AgentCostCategory
  tokens: number
  usd: number | null
}

// The task's cost split over its events. The items, misses and unattributed parts add up to the requests.
export type AgentEventCosts = {
  model: string | null
  priced: boolean
  total_usd: number | null
  // Keyed by the line number of the item's event.
  items: Record<string, AgentItemCost>
  requests: AgentRequestCost[]
  categories: AgentCategoryCost[]
}

export type AgentTaskDetailResponse = {
  run_id: string
  task_id: string
  task_file_path: string
  is_live: boolean
  task: AgentTask
  // The task's matrix cell, as of this response.
  cell: AgentTaskCell
  // Events of a task that is still running; a finished task carries them on its attempt.
  live_events: AgentStdoutEvent[]
  event_costs: AgentEventCosts
  answer_file_views: Array<{
    answer_file: string
    path_in_copy: string
    original_text: string | null
    generated_text: string | null
    // Where each text came from: the snapshot the task took, the file on the server, or nowhere.
    original_source: 'snapshot' | 'current_source' | 'missing'
    generated_source: 'snapshot' | 'missing'
  }>
  // Null for an implementation task.
  mutant_matrix: AgentMutantMatrix | null
  terminal_log: {
    path: string | null
    content: string | null
    truncated: boolean
    total_chars: number
  }
}

export type MutationSource = 'llm' | 'real-bug'

// A task's mutation as the catalogue's list carries it: enough to filter and search by.
export type MutationSummary = {
  id: string
  kind: string
  source: MutationSource
}

export type MutationCatalogueTask = {
  task_id: string
  // The task id as a file name: the key the task's mutations are fetched by.
  file_name: string
  repo: string
  short_name: string
  mutations: MutationSummary[]
}

export type MutationCatalogueResponse = {
  tasks: MutationCatalogueTask[]
}

export type MutationValidationOutcome = 'not-impl-only' | 'does-not-apply' | 'does-not-compile' | 'survived' | 'killed'

export type CatalogueMutation = MutationSummary & {
  reason: string
  patch: string
  target_script: string | null
  commit: string | null
  // Null when no validation report lists the mutation.
  validation: {
    outcome: MutationValidationOutcome
    outcome_detail: string | null
    // Grading keys, `<file>:<script>`, of the original test file's scripts the mutation makes fail.
    newly_failing: string[]
  } | null
}

export type TaskMutationsResponse = {
  task_id: string
  // The original test file's scripts; null without a validation report.
  scripts: number | null
  mutations: CatalogueMutation[]
  // The original text of each patched file that is checked out, by repository-relative path.
  files: Record<string, string>
}
