import type { Approval, BudgetLimits, ControlReceipt, ControlState, EventPage, ExpectedState, ExperimentSpecification, Overview, Page, PDFDocument, Project, Session, SubmissionReceipt, Task, WorkflowEvent } from './types';

type RecordValue = Record<string, unknown>;
function record(value: unknown): value is RecordValue {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
const string = (value: unknown): value is string => typeof value === 'string';
const nullableString = (value: unknown) => value === null || string(value);
const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const integer = (value: unknown): value is number => number(value) && Number.isSafeInteger(value);
const numbers = (value: unknown) => record(value) && Object.values(value).every(number);
const statuses = new Set(['running', 'succeeded', 'partial', 'failed', 'budget_exceeded',
  'queued', 'pause_requested', 'paused', 'awaiting_approval', 'cancel_requested', 'cancelled', 'recovery_required']);
export const isTaskStatus = (value: unknown): value is Task['status'] => string(value) && statuses.has(value);
export const eventKinds = new Set(['submitted', 'claimed', 'call_reconciled', 'recovery_verified', 'checkpoint_rejected',
  'approval_consumed', 'unit_started', 'budget_saved', 'call_started', 'call_finished', 'checkpoint_saved',
  'pause', 'resume', 'cancel', 'memory_retrieved', 'memory_enabled_changed', 'experience_recorded',
  'approval_requested', 'approval_decided', 'other']);

export function isWorkflowEvent(value: unknown): value is WorkflowEvent {
  if (!record(value) || !string(value.task_id) || !integer(value.sequence) || value.sequence < 1
    || !string(value.kind) || !eventKinds.has(value.kind) || !string(value.created_at) || !record(value.payload)) return false;
  return Object.entries(value.payload).every(([key, item]) => {
    if (['status', 'previous'].includes(key)) return isTaskStatus(item);
    if (key === 'stage') return string(item) && ['planner', 'executor', 'reviewer', 'replanner', 'writer', 'done'].includes(item);
    if (key === 'kind_name') return string(item) && ['llm', 'search', 'training', 'sandbox', 'tool', 'repair', 'embedding', 'mcp'].includes(item);
    if (key === 'decision') return string(item) && ['approve', 'reject', 'modify'].includes(item);
    if (key === 'enabled') return typeof item === 'boolean';
    return ['sequence', 'version', 'project_version'].includes(key) && integer(item);
  });
}
export function isEventPage(value: unknown): value is EventPage {
  if (!record(value) || !Array.isArray(value.items) || value.items.length > 100 || !value.items.every(isWorkflowEvent)) return false;
  const items = value.items;
  return string(value.task_id) && isTaskStatus(value.status) && typeof value.durable === 'boolean'
    && items.every((item, index) => item.task_id === value.task_id
      && (index === 0 || item.sequence === items[index - 1].sequence + 1))
    && integer(value.next_cursor) && (items.length === 0 || value.next_cursor === items.at(-1)!.sequence)
    && typeof value.has_more === 'boolean' && typeof value.has_older === 'boolean';
}

export function isSession(value: unknown): value is Session {
  if (!record(value) || value.status !== 'connected' || value.authentication !== 'single_operator_bearer'
    || !string(value.api_version) || !record(value.capabilities)) return false;
  const caps = value.capabilities;
  return ['llm_configured', 'web_search_configured', 'task_streaming'].every((key) => typeof caps[key] === 'boolean')
    && ['research_backend', 'filesystem_backend', 'experiment_backend'].every((key) => string(caps[key]))
    && (caps.document_max_bytes === undefined || (integer(caps.document_max_bytes) && caps.document_max_bytes > 0))
    && (caps.document_max_pages === undefined || (integer(caps.document_max_pages) && caps.document_max_pages > 0))
    && (caps.budget_limits === undefined || isBudget(caps.budget_limits))
    && (caps.task_controls === undefined || typeof caps.task_controls === 'boolean');
}
export function isBudget(value: unknown): value is BudgetLimits {
  return record(value) && ['steps', 'replans', 'model_calls', 'tool_calls', 'search_queries', 'code_repairs',
    'training_runs', 'sandbox_runs', 'total_tokens', 'wall_seconds'].every((key) => integer(value[key]));
}
export function isDocument(value: unknown): value is PDFDocument {
  return record(value) && ['id', 'project_id', 'original_filename', 'title', 'content_type', 'sha256',
    'created_at', 'updated_at'].every((key) => string(value[key])) && integer(value.size_bytes)
    && string(value.status) && ['queued', 'processing', 'ready', 'failed', 'deleting'].includes(value.status)
    && (value.page_count === null || integer(value.page_count)) && integer(value.chunk_count)
    && integer(value.index_version) && nullableString(value.embedding_model)
    && nullableString(value.error_code) && nullableString(value.error_message);
}
export function isReceipt(value: unknown): value is SubmissionReceipt {
  return record(value) && string(value.task_id) && string(value.status) && statuses.has(value.status)
    && typeof value.created === 'boolean' && string(value.status_url);
}
export function isProject(value: unknown): value is Project {
  return record(value) && string(value.id) && string(value.name) && nullableString(value.description)
    && string(value.created_at) && string(value.updated_at);
}
export function isTask(value: unknown): value is Task {
  return record(value) && string(value.task_id) && nullableString(value.project_id) && nullableString(value.parent_task_id)
    && string(value.objective) && string(value.mode) && string(value.status) && statuses.has(value.status)
    && numbers(value.metrics) && nullableString(value.error_code) && string(value.created_at) && string(value.updated_at);
}
export function isPage<T>(value: unknown, item: (value: unknown) => value is T): value is Page<T> {
  return record(value) && Array.isArray(value.items) && integer(value.limit) && value.limit > 0
    && value.limit <= 100 && value.items.length <= value.limit && value.items.every(item)
    && integer(value.total) && value.total >= value.items.length && integer(value.offset);
}
export function isOverview(value: unknown): value is Overview {
  if (!record(value) || !isTask(value.task) || !isPage(value.children, isTask)
    || !nullableString(value.answer) || typeof value.answer_truncated !== 'boolean'
    || value.usage_scope !== 'root_task_only' || value.monetary_cost !== null || !record(value.resources)) return false;
  const resources = value.resources;
  if (!['evidence', 'experiments', 'artifacts'].every((key) => integer(resources[key]))) return false;
  if (value.current_plan !== null) {
    const current = value.current_plan;
    if (!record(current) || !integer(current.version) || !record(current.plan)
      || !string(current.plan.goal) || !Array.isArray(current.plan.steps) || current.plan.steps.length > 20
      || !current.plan.steps.every((step: unknown) => record(step) && string(step.id) && string(step.type)
        && string(step.description) && string(step.expected_output) && Array.isArray(step.dependencies)
        && step.dependencies.every(string))) return false;
  }
  if (value.execution !== null) {
    const run = value.execution;
    if (!record(run) || !string(run.status) || !statuses.has(run.status) || !nullableString(run.stage)
      || !(run.plan_version === null || integer(run.plan_version)) || !integer(run.checkpoint_sequence)
      || !numbers(run.budget_used) || !(run.budget_limits === null || numbers(run.budget_limits))
      || !number(run.active_seconds) || !integer(run.pending_call_count) || !nullableString(run.error_code)) return false;
  }
  return true;
}

export function isExpectedState(value: unknown): value is ExpectedState {
  return record(value) && isTaskStatus(value.status) && integer(value.checkpoint_sequence)
    && value.checkpoint_sequence >= 1 && integer(value.event_sequence);
}
export function isControlReceipt(value: unknown): value is ControlReceipt {
  return record(value) && string(value.task_id) && isTaskStatus(value.status);
}
export function isControlState(value: unknown): value is ControlState {
  if (!isControlReceipt(value) || !record(value) || !Array.isArray(value.actions)
    || value.actions.length > 3 || new Set(value.actions).size !== value.actions.length) return false;
  if (value.expected === null) return value.actions.length === 0;
  if (!isExpectedState(value.expected) || value.expected.status !== value.status) return false;
  const allowed = { pause: ['queued', 'running'], resume: ['paused'],
    cancel: ['queued', 'running', 'paused', 'pause_requested', 'awaiting_approval', 'recovery_required'] };
  return value.actions.every((action: unknown) => string(action) && action in allowed
    && allowed[action as keyof typeof allowed].includes(value.status));
}
export function isSpecification(value: unknown): value is ExperimentSpecification {
  if (!record(value)) return false;
  const bounded = (key: string, min: number, max: number) => integer(value[key]) && value[key] >= min && value[key] <= max;
  return value.schema_version === 1 && value.dataset === 'mnist' && value.device === 'cpu'
    && value.primary_metric === 'test_accuracy' && string(value.name)
    && /^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$/.test(value.name)
    && Array.isArray(value.models) && value.models.length === 2 && value.models[0] === 'mlp' && value.models[1] === 'cnn'
    && bounded('seed', 0, 4294967295) && bounded('epochs', 1, 10) && bounded('batch_size', 8, 256)
    && bounded('train_samples', 128, 60000) && bounded('test_samples', 128, 10000)
    && number(value.learning_rate) && value.learning_rate > 0 && value.learning_rate <= 1;
}
export function isApproval(value: unknown): value is Approval {
  return record(value) && string(value.id) && string(value.operation_sha256) && /^[0-9a-f]{64}$/.test(value.operation_sha256)
    && string(value.status) && ['pending', 'approved', 'rejected', 'expired', 'superseded', 'consumed'].includes(value.status)
    && integer(value.plan_version) && value.plan_version >= 1 && string(value.step_id) && isSpecification(value.specification)
    && isBudget(value.budget_limits) && integer(value.cost_units)
    && value.cost_units === value.specification.epochs * value.specification.train_samples * 2
    && value.risk_level === 3 && string(value.reason) && value.reason.length <= 2000
    && string(value.expires_at) && Number.isFinite(Date.parse(value.expires_at))
    && Array.isArray(value.actions) && value.actions.length <= 3 && new Set(value.actions).size === value.actions.length
    && value.actions.every((action: unknown) => ['approve', 'reject', 'modify'].includes(String(action)))
    && (value.status === 'pending' || !value.actions.includes('approve'));
}
