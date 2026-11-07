import { useEffect, useRef, useState } from 'react';
import { Alert, Button } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import { useConnection } from './connection';
import { errorMessage } from './api';

function filename(objective: string, taskId: string): string {
  const title = Array.from(objective.replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, '_').trim()).slice(0, 80).join('').replace(/[. ]+$/g, '');
  return `${title || '研究报告'}-${taskId.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 8)}.md`;
}

export default function ExportMarkdown({ taskId, objective, available }: { taskId: string; objective: string; available: boolean }) {
  const { api, session } = useConnection();
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null);
  const active = useRef<AbortController | null>(null);
  const download = useRef<{ url: string; timer: ReturnType<typeof setTimeout> } | null>(null);
  function release() { if (download.current) { clearTimeout(download.current.timer); URL.revokeObjectURL(download.current.url); download.current = null; } }
  useEffect(() => () => { active.current?.abort(); release(); }, [taskId]);
  async function exportFile() {
    if (!api || active.current || !available) return;
    const controller = new AbortController(); active.current = controller; setBusy(true); setError(null);
    try {
      const blob = await api.downloadMarkdown(taskId, controller.signal);
      if (controller.signal.aborted) return;
      release();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename(objective, taskId);
      anchor.click();
      download.current = { url, timer: setTimeout(release, 1000) };
    } catch (failure) { if (!controller.signal.aborted) setError(failure); }
    finally { if (active.current === controller) { active.current = null; setBusy(false); } }
  }
  if (!session?.capabilities.markdown_export) return null;
  return <><Button aria-label="导出 Markdown" icon={<DownloadOutlined />} loading={busy} disabled={!available} onClick={() => void exportFile()}>导出 Markdown</Button>
    {error !== null && <Alert type="error" title={errorMessage(error)} />}</>;
}
