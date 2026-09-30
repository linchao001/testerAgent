/** 与后端 dd §10.2 / §2.3 / §2.4 对齐的手写类型（WP-F1/F2）。 */

export type TaskStatus =
  | 'waiting_input'
  | 'waiting_confirm'
  | 'running'
  | 'cancelling'
  | 'completed'
  | 'failed'
  | 'aborted'

export type StageName = 'link_identify' | 'point_write' | 'coverage_design' | 'point_design'

export type GateKind = 'plan_confirm' | 'review_decision'

export type LinkRef = {
  link_id: string
  title: string
  summary: string
  hit: boolean
  entry_id: string | null
  entry_version: string | null
  confidence: number
  story_ids: string[]
}

export type StoryRef = {
  story_id: string
  link_id: string
  title: string
  summary: string
  hit: boolean
  entry_id: string | null
  entry_version: string | null
  confidence: number
  rationale: string
  related_clause_ids: string[]
}

export type NewLinkSuggestion = {
  suggested_link_title: string
  suggested_story_title: string
  reason: string
  source_clause_ids: string[]
}

export type LinkPlan = {
  links: LinkRef[]
  stories: StoryRef[]
  new_suggestions: NewLinkSuggestion[]
}

export type TestPoint = {
  point_id: string
  story_id: string
  title: string
  angle: string
  method: string
  clause_ids: string[]
  source_entry_ids: string[]
  priority: 'P0' | 'P1' | 'P2'
}

export type PointPlan = {
  points: TestPoint[]
}

export type ArtifactDetail = {
  id: string
  task_id: string
  stage: string
  stage_version: number
  origin: string
  status: string
  confirmed_by: string | null
  created_at: string
  payload: LinkPlan | PointPlan | Record<string, unknown>
}

export type ConfirmOut = {
  task_id: string
  status: string
  artifact_id: string
  stage_version: number
}

export type KbConfig = {
  kb_id: string
  knowledge_bases_dir?: string
  knowledge_dir?: string
  create_knowledge_base?: boolean
  options?: Record<string, unknown>
}

export type Workspace = {
  id: string
  name: string
  description: string
  /** 空 = 默认 data/workspaces/{id}/；非空 = 绝对路径 */
  root_dir: string
  kb_config: KbConfig | Record<string, unknown>
  created_at: string
}

export type ReMeCapabilities = {
  metadata_filter: boolean
  entry_version: boolean
  passage_api: boolean
}

export type KbTestOut = {
  ok: boolean
  latency_ms: number
  capabilities: ReMeCapabilities | null
  error_code: string | null
}

export type Agent = {
  id: string
  name: string
  agent_type: string
  config: Record<string, unknown>
  builtin: boolean
  created_at: string
}

export type ModelConfig = {
  base_url: string
  api_key: string
  model: string
  temperature: number
  top_p: number
  timeout: number
}

export type ModelTestOut = {
  ok: boolean
  latency_ms: number
  model: string | null
  error_code: string | null
}

export type Conversation = {
  id: string
  workspace_id: string
  title: string
  created_at: string
  updated_at: string
}

export type Message = {
  id: string
  conversation_id: string
  task_id: string | null
  role: string
  kind: string
  content: string
  ref_artifact_id: string | null
  payload: Record<string, unknown>
  created_at: string
  author: string
}

/** POST /conversations/{id}/messages 响应（chat 含 assistant+tool_trace）。 */
export type SendMessageOut = {
  user: Message
  assistant: Message | null
  started_task_id?: string | null
}

export type ArtifactSummary = {
  id: string
  stage: string
  stage_version: number
  origin: string
  status: string
  confirmed_by: string | null
  created_at: string
}

export type Task = {
  id: string
  workspace_id: string
  conversation_id: string
  status: TaskStatus | string
  current_stage: string
  active_artifacts: Record<string, ArtifactSummary>
  progress: Record<string, unknown[]>
  error_info: Record<string, unknown> | null
  stale: boolean
  created_at: string
  updated_at: string
}

export type RunHandle = {
  task_id: string
  graph_run_id: string
  events_url: string
  resume_from: Record<string, unknown>
}

export type ClarificationQuestion = {
  id: string
  question: string
  options?: string[]
}

export type CheckpointWaiting = {
  stage: string
  artifact_id: string
  stage_version: number
  gate_kind?: GateKind
  step_id?: string
}

export type ReviewProposalItemAction =
  | 'adopt'
  | 'edit_adopt'
  | 'reject'
  | 'add_point'
  | 'add_case'
  | 'repair'

export type ReviewProposalItem = {
  target_id: string
  action: ReviewProposalItemAction
  rationale: string
  confidence: number
  patch?: Record<string, unknown> | null
}

export type ReviewProposal = {
  scope: string
  items: ReviewProposalItem[]
  matrix_ref?: string | null
  degraded?: boolean
}

export type ImpactAnalysis = {
  target_stage: string
  affected_point_ids?: string[]
  summary?: string
  downstream?: Array<{
    stage: string
    affected_ids: string[]
    unaffected_ids: string[]
  }>
}

export type RollbackOut = {
  graph_run_id: string
  impact: ImpactAnalysis
}

export type CaseStatus = 'active' | 'obsolete'

export type ReviewStatus =
  | 'pending'
  | 'adopted'
  | 'edited_adopted'
  | 'rejected'

export type ReviewAction = 'adopt' | 'reject' | 'edited_adopted'

export type Lineage = {
  root_case_id: string
  regenerated_from_case_id: string | null
}

export type TraceRefs = {
  clause_ids: string[]
  entry_ids: string[]
  point_ids: string[]
}

export type CaseSummary = {
  id: string
  point_id: string
  stage_version: number
  title: string
  status: CaseStatus | string
  review_status: ReviewStatus | string
  batch_id: string
  error_info: { code?: string; [k: string]: unknown } | null
  created_at: string
  updated_at: string
}

export type CaseDetail = {
  id: string
  point_id: string
  stage_version: number
  lineage: Lineage
  review_status: ReviewStatus | string
  markdown: string
  content_hash: string
  trace_refs: TraceRefs
  error_info: { code?: string; [k: string]: unknown } | null
}

export type ReviewResultItem = {
  case_id: string
  ok: boolean
  review_status: string | null
  error: string | null
}

export type ReviewOut = {
  results: ReviewResultItem[]
}

export type DegradedStep = {
  step: string
  reason: string
  fallback: string
}

export type CandidateView = {
  entry_id: string
  entry_version: string
  title: string
  score: number
  source_channel: string
  entry_type: string
  kept: boolean
  drop_reason: string | null
  latency_ms?: number | null
  error?: string | null
}

export type TraceSummary = {
  id: string
  task_id: string
  graph_run_id: string
  stage: string
  stage_version: number
  node: string
  batch_id: string | null
  query_count: number
  candidate_count: number
  kept_count: number
  injected_count: number
  referenced_count: number
  degraded_count: number
  created_at: string
}

export type TraceDetail = {
  id: string
  task_id: string
  graph_run_id: string
  stage: string
  stage_version: number
  node: string
  batch_id: string | null
  query_variant: Record<string, unknown>
  candidates: CandidateView[]
  injected_ids: string[]
  referenced_ids: string[]
  hallucinated_ids: string[]
  weak_ref_ids: string[]
  degraded: DegradedStep[]
  created_at: string
}

export type SnapshotSummary = {
  id: string
  task_id: string
  graph_run_id: string
  stage: string
  stage_version: number
  node: string
  batch_id: string | null
  item_count: number
  total_tokens_est: number
  budget: number | null
  truncated: boolean
  has_full: boolean
  prompt_template_ver: string
  created_at: string
}

export type SnapshotItemMeta = {
  entry_id: string
  entry_version: string
  title: string
  tokens_est: number
  position: number
  char_offset: number | null
  byte_length: number | null
}

export type SnapshotDetail = SnapshotSummary & {
  items: SnapshotItemMeta[]
  model_ref: Record<string, unknown>
  usage: Record<string, unknown>
  latencies: Record<string, unknown>
}

export type SnapshotLine = {
  position: number
  entry_id: string
  entry_version: string
  title: string
  content: string
}

export type FunnelCounts = {
  total: number
  kept: number
  errors: number
  dropped: Record<string, number>
}

export type PlaygroundOut = {
  funnel: FunnelCounts
  candidates: CandidateView[]
  injected: Array<Record<string, unknown>>
  degraded: DegradedStep[]
  latencies: Record<string, unknown>
  token_est: number
  truncated: boolean
}

export type FunnelStage = {
  key: 'recall' | 'filter' | 'rerank' | 'inject'
  label: string
  count: number
}
