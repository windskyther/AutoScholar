import type { Overview, Page, Project, Session, Task } from './types';

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

export function isSession(value: unknown): value is Session {
  if (!record(value) || value.status !== 'connected' || value.authentication !== 'single_operator_bearer'
    || !string(value.api_version) || !record(value.capabilities)) return false;
  const caps = value.capabilities;
  return ['llm_configured', 'web_search_configured', 'task_streaming'].every((key) => typeof caps[key] === 'boolean')
    && ['research_backend', 'filesystem_backend', 'experiment_backend'].every((key) => string(caps[key]));
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
