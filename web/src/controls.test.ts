import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, isWriteUncertain } from './api';
import { isApproval, isControlState, isSpecification } from './guards';
import type { Approval, ApprovalDecision, ExpectedState } from './types';

const expected: ExpectedState = { status: 'awaiting_approval', checkpoint_sequence: 3, event_sequence: 12 };
const specification = { schema_version: 1 as const, name: 'mnist-comparison', dataset: 'mnist' as const,
  models: ['mlp', 'cnn'] as ['mlp', 'cnn'], seed: 42, epochs: 2, batch_size: 64, learning_rate: 0.001,
  train_samples: 2048, test_samples: 1024, primary_metric: 'test_accuracy' as const, device: 'cpu' as const };
const approval: Approval = { id: 'approval-a', operation_sha256: 'a'.repeat(64), status: 'pending',
  plan_version: 1, step_id: 'train', specification, budget_limits: {
    steps: 20, replans: 3, model_calls: 60, tool_calls: 50, search_queries: 20, code_repairs: 3,
    training_runs: 4, sandbox_runs: 40, total_tokens: 120000, wall_seconds: 1800,
  }, cost_units: 8192, risk_level: 3, reason: 'Public test', expires_at: '2026-10-07T00:00:00Z', actions: ['approve', 'reject', 'modify'] };
const decision: ApprovalDecision = { action: 'approve', operation_sha256: approval.operation_sha256, reason: '', expected };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

describe('guarded controls', () => {
  it('sends state-bound POST and token only in the header', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ task_id: 'task-a', status: 'queued' })); vi.stubGlobal('fetch', fetcher);
    const state = { ...expected, status: 'paused' as const };
    await new ApiClient('public-token').controlTask('task-a', 'resume', state);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe('/api/workbench/tasks/task-a/control/resume');
    expect(options).toMatchObject({ method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error', headers: { Authorization: 'Bearer public-token' } });
    expect(JSON.parse(options.body)).toEqual({ expected: state });
    expect(url).not.toContain('public-token');
  });
  it('sends modification with immutable original fingerprint and complete specification', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ task_id: 'task-a', status: 'paused' })); vi.stubGlobal('fetch', fetcher);
    await new ApiClient('public-token').decideApproval('task-a', 'approval-a', { ...decision, action: 'modify', specification: { ...specification, epochs: 1 } });
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ ...decision, action: 'modify', specification: { ...specification, epochs: 1 } });
  });
  it.each(['network', 'invalid', 'wrong_task', 'server'])('does not retry uncertain %s writes', async (kind) => {
    const fetcher = kind === 'network' ? vi.fn().mockRejectedValue(new Error('lost')) : vi.fn().mockResolvedValue(
      kind === 'invalid' ? json({}) : kind === 'wrong_task' ? json({ task_id: 'other', status: 'paused' }) : json({ error: { code: 'internal_error' } }, 500));
    vi.stubGlobal('fetch', fetcher);
    try { await new ApiClient('public-token').decideApproval('task-a', 'approval-a', decision); throw new Error('Expected failure'); }
    catch (failure) { expect(isWriteUncertain(failure)).toBe(true); }
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('stale state is a known rejection and not retried', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ error: { code: 'workbench_state_stale' } }, 409)); vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('public-token').controlTask('task-a', 'cancel', expected)).rejects.toMatchObject({ code: 'workbench_state_stale', status: 409 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('rejects an inconsistent success status as uncertain', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ task_id: 'task-a', status: 'running' })); vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('public-token').decideApproval('task-a', 'approval-a', decision)).rejects.toMatchObject({ code: 'write_result_unknown' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('authentication failure clears session and never retries', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({}, 401)); vi.stubGlobal('fetch', fetcher);
    const disconnected = vi.fn(); const client = new ApiClient('public-token', disconnected);
    await expect(client.decideApproval('task-a', 'approval-a', decision)).rejects.toMatchObject({ status: 401 });
    expect(disconnected).toHaveBeenCalledTimes(1); expect(client.connected).toBe(false);
  });
  it.each([
    { ...decision, operation_sha256: 'bad' }, { ...decision, action: 'modify', specification: { ...specification, epochs: 0 } },
    { ...decision, specification }, { ...decision, expected: { ...expected, checkpoint_sequence: -1 } },
  ])('rejects invalid decisions before network', async (value) => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('public-token').decideApproval('task-a', 'approval-a', value as ApprovalDecision)).rejects.toMatchObject({ code: 'request_validation_error' });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('rejects cross-task read responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ task_id: 'other', status: 'awaiting_approval', expected, actions: ['cancel'] })));
    await expect(new ApiClient('public-token').controls('task-a')).rejects.toMatchObject({ code: 'invalid_response' });
  });
  it('validates bounded approval pages', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ task_id: 'task-a', items: [approval], total: 1, limit: 20, offset: 20 })); vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('public-token').approvals('task-a', 2)).resolves.toMatchObject({ items: [approval] });
    expect(fetcher.mock.calls[0][0]).toContain('offset=20');
  });
  it('validates legacy state and rejects unsafe action availability', () => {
    expect(isControlState({ task_id: 'legacy', status: 'succeeded', expected: null, actions: [] })).toBe(true);
    expect(isControlState({ task_id: 'task-a', status: 'awaiting_approval', expected, actions: ['resume'] })).toBe(false);
    expect(isControlState({ task_id: 'task-a', status: 'awaiting_approval', expected, actions: ['cancel', 'cancel'] })).toBe(false);
  });
  it('rejects unsupported specifications and misleading cost/risk data', () => {
    expect(isSpecification(specification)).toBe(true); expect(isApproval(approval)).toBe(true);
    for (const fields of [{ models: ['cnn', 'mlp'] }, { learning_rate: Infinity }, { dataset: 'other' }, { seed: -1 }]) {
      expect(isSpecification({ ...specification, ...fields })).toBe(false);
    }
    expect(isApproval({ ...approval, cost_units: 0 })).toBe(false);
    expect(isApproval({ ...approval, risk_level: 0 })).toBe(false);
    expect(isApproval({ ...approval, status: 'consumed' })).toBe(false);
  });
});
