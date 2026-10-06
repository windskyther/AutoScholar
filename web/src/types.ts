export type TaskStatus = 'running' | 'succeeded' | 'partial' | 'failed' | 'budget_exceeded'
  | 'queued' | 'pause_requested' | 'paused' | 'awaiting_approval' | 'cancel_requested'
  | 'cancelled' | 'recovery_required';

export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export interface Project {
  id: string; name: string; description: string | null; created_at: string; updated_at: string;
}
export interface Session {
  status: 'connected'; authentication: 'single_operator_bearer'; api_version: string;
  capabilities: {
    llm_configured: boolean; web_search_configured: boolean; research_backend: string;
    filesystem_backend: string; experiment_backend: string; task_streaming: boolean;
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
