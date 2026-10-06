export type TaskStatus = 'running' | 'succeeded' | 'partial' | 'failed' | 'budget_exceeded'
  | 'queued' | 'pause_requested' | 'paused' | 'awaiting_approval' | 'cancel_requested'
  | 'cancelled' | 'recovery_required';

export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export interface Project {
  id: string; name: string; description: string | null; created_at: string; updated_at: string;
}
export interface BudgetLimits {
  steps: number; replans: number; model_calls: number; tool_calls: number; search_queries: number;
  code_repairs: number; training_runs: number; sandbox_runs: number; total_tokens: number; wall_seconds: number;
}
export type DocumentStatus = 'queued' | 'processing' | 'ready' | 'failed' | 'deleting';
export interface PDFDocument {
  id: string; project_id: string; original_filename: string; title: string; content_type: string;
  sha256: string; size_bytes: number; status: DocumentStatus; page_count: number | null; chunk_count: number;
  embedding_model: string | null; index_version: number; error_code: string | null;
  error_message: string | null; created_at: string; updated_at: string;
}
export interface TaskSubmission {
  objective: string; mode: 'autonomous'; project_id: string; document_ids: string[] | null;
  retrieval_mode: 'dense' | 'sparse' | 'hybrid' | 'hybrid_rerank'; research_sources: ('web' | 'paper')[];
  budget: BudgetLimits;
}
export interface SubmissionReceipt { task_id: string; status: TaskStatus; created: boolean; status_url: string }
export interface Session {
  status: 'connected'; authentication: 'single_operator_bearer'; api_version: string;
  capabilities: {
    llm_configured: boolean; web_search_configured: boolean; research_backend: string;
    filesystem_backend: string; experiment_backend: string; task_streaming: boolean;
    document_max_bytes?: number; document_max_pages?: number; budget_limits?: BudgetLimits;
  };
}
export interface Task {
  task_id: string; project_id: string | null; parent_task_id: string | null;
  objective: string; mode: string; status: TaskStatus; metrics: Record<string, number>;
  error_code: string | null; created_at: string; updated_at: string;
}
export interface PlanStep {
  id: string; type: string; description: string; dependencies: string[]; expected_output: string;
}
export interface Overview {
  task: Task; children: Page<Task>;
  current_plan: { version: number; plan: { goal: string; steps: PlanStep[] } } | null;
  execution: {
    status: TaskStatus; stage: string | null; plan_version: number | null;
    checkpoint_sequence: number; budget_used: Record<string, number>;
    budget_limits: Record<string, number> | null; active_seconds: number;
    pending_call_count: number; error_code: string | null;
  } | null;
  resources: { evidence: number; experiments: number; artifacts: number };
  answer: string | null; answer_truncated: boolean; usage_scope: 'root_task_only'; monetary_cost: null;
}

export interface WorkflowEvent {
  task_id: string; sequence: number; kind: string; created_at: string;
  payload: Record<string, string | number | boolean>;
}
export interface EventPage {
  task_id: string; status: TaskStatus; durable: boolean; items: WorkflowEvent[];
  next_cursor: number; has_more: boolean; has_older: boolean;
}
export type StreamMessage =
  | { type: 'workflow'; event: WorkflowEvent }
  | { type: 'ready'; status: TaskStatus; durable: boolean }
  | { type: 'end'; status: TaskStatus; reason: 'terminal' | 'unsupported' | 'rotate' };
