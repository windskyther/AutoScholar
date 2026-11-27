import { useEffect, useState } from 'react';
import { Alert, Button, Form, Input, Popconfirm, Select, Typography } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, desktopModelCache } from './api';
import { usePageReady } from './page-transition';

declare global { interface Window { __autoscholarSession?: string } }

export default function DesktopSettings() {
  usePageReady(true);
  const [current, setCurrent] = useState('');
  const [missing, setMissing] = useState<{ id: string; name: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const queryClient = useQueryClient();
  const cacheKey = ['desktop-model-cache'];
  const cache = useQuery({ queryKey: cacheKey, refetchInterval: 10000,
    queryFn: ({ signal }) => desktopModelCache('GET', signal) });
  const clearCache = useMutation({
    mutationFn: () => desktopModelCache('DELETE'),
    onSuccess: async bytes => {
      await queryClient.cancelQueries({ queryKey: cacheKey });
      queryClient.setQueryData(cacheKey, bytes);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: cacheKey }),
  });
  const headers = { 'X-AutoScholar-Session': window.__autoscholarSession ?? '' };
  useEffect(() => {
    const controller = new AbortController();
    fetch('/api/desktop/settings', { headers, signal: controller.signal })
      .then(r => r.json()).then(r => { setCurrent(r.directory); setMissing(r.profiles); })
      .catch(() => { if (!controller.signal.aborted) setMessage('无法读取桌面设置。'); });
    return () => controller.abort();
  }, []);
  async function save(values: Record<string, string>) {
    setBusy(true);
    try {
      const response = await fetch('/api/desktop/settings', {
        method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.fromEntries(Object.entries(values).filter(([, v]) => v))),
      });
      setMessage(response.ok ? '已保存。退出并重新启动 AutoScholar 后生效。' :
        '保存失败。请检查目录权限及可用空间。');
    } catch { setMessage('与本地服务的连接中断，请检查后再试。'); }
    finally { setBusy(false); }
  }
  return <section>
    <Typography.Title level={2}>桌面设置</Typography.Title>
    <div className="settings-columns">
      <div>
        <Typography.Paragraph>当前资料库　{current}</Typography.Paragraph>
        <Form layout="vertical" onFinish={save}>
          <Form.Item name="directory" label="新资料库的父目录" extra="留空以继续使用当前资料库。填写目录将创建新的资料库，原资料库保留。"><Input placeholder="D:\AutoScholar" /></Form.Item>
          <Form.Item name="web" label="Tavily API Key"><Input.Password autoComplete="new-password" /></Form.Item>
          <Form.Item name="paper" label="Semantic Scholar API Key"><Input.Password autoComplete="new-password" /></Form.Item>
          {missing.length > 0 && <>
            <Form.Item name="profile" label="缺少密钥的模型"><Select allowClear options={missing.map(p => ({ value: p.id, label: p.name }))} /></Form.Item>
            <Form.Item name="profile_key" label="重新填写模型 API Key"><Input.Password autoComplete="new-password" /></Form.Item>
          </>}
          <Button htmlType="submit" type="primary" loading={busy}>保存设置</Button>
        </Form>
        {message && <Alert role="status" message={message} />}
      </div>
      <div>
        <Typography.Title level={3}>本地检索模型缓存</Typography.Title>
        <Typography.Paragraph>{cache.data !== undefined
          ? `已占用 ${(cache.data / 1048576).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} MiB`
          : cache.isError ? '无法读取缓存大小。' : '正在读取缓存大小…'}</Typography.Paragraph>
        <Popconfirm title="清除当前资料库的模型缓存？"
          description="下次使用 PDF 检索时需要联网重新下载模型。项目、文档和报告会保留。"
          okText="清除" cancelText="取消" onConfirm={() => clearCache.mutate()}>
          <Button loading={clearCache.isPending} disabled={cache.data === 0}>清除模型缓存</Button>
        </Popconfirm>
        {clearCache.isSuccess && <Alert role="status" type="success" message="模型缓存已清除。" />}
        {clearCache.isError && <Alert role="alert" type="warning" message={
          clearCache.error instanceof ApiError && clearCache.error.code === 'model_cache_busy'
            ? '模型缓存正在使用，请稍后再试。' : '清除失败，请检查缓存目录的权限或文件占用后重试。'
        } />}
      </div>
    </div>
  </section>;
}
