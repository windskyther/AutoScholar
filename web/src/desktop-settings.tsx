import { useEffect, useState } from 'react';
import { Alert, Button, Form, Input, Select, Typography } from 'antd';

declare global { interface Window { __autoscholarSession?: string } }

export default function DesktopSettings() {
  const [current, setCurrent] = useState('');
  const [missing, setMissing] = useState<{ id: string; name: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
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
        '保存失败。请检查目录权限、迁移包及可用空间。');
    } catch { setMessage('与本地服务的连接中断，请检查后再试。'); }
    finally { setBusy(false); }
  }
  return <section style={{ maxWidth: 720, margin: '2rem auto' }}>
    <Typography.Title level={2}>桌面设置</Typography.Title>
    <Typography.Paragraph>当前资料库　{current}</Typography.Paragraph>
    <Form layout="vertical" onFinish={save}>
      <Form.Item name="directory" label="新资料库的父目录" extra="留空以继续使用当前资料库。填写目录将创建新的资料库，原资料库保留。"><Input placeholder="D:\AutoScholar" /></Form.Item>
      <Form.Item name="package" label="导入旧版迁移包" extra="填写导出的 ZIP 完整路径。导入完成并重启后可查看历史项目。"><Input /></Form.Item>
      <Form.Item name="web" label="Tavily API Key"><Input.Password autoComplete="new-password" /></Form.Item>
      <Form.Item name="paper" label="Semantic Scholar API Key"><Input.Password autoComplete="new-password" /></Form.Item>
      {missing.length > 0 && <>
        <Form.Item name="profile" label="导入的模型"><Select allowClear options={missing.map(p => ({ value: p.id, label: p.name }))} /></Form.Item>
        <Form.Item name="profile_key" label="重新填写模型 API Key"><Input.Password autoComplete="new-password" /></Form.Item>
      </>}
      <Button htmlType="submit" type="primary" loading={busy}>保存设置</Button>
    </Form>
    {message && <Alert role="status" message={message} />}
  </section>;
}
