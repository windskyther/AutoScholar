import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import './styles.css';
import App from './App';
import { ConnectionProvider } from './connection';

const cache = new QueryClient({ defaultOptions: {
  queries: { retry: false, staleTime: 15000, refetchOnWindowFocus: false, refetchOnReconnect: false },
  mutations: { retry: false },
} });

createRoot(document.getElementById('root')!).render(<StrictMode>
  <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={{ token: {
    colorPrimary: '#1f7165', borderRadius: 6, fontSize: 14,
    fontFamily: '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
  } }}>
    <QueryClientProvider client={cache}><ConnectionProvider><BrowserRouter><App /></BrowserRouter></ConnectionProvider></QueryClientProvider>
  </ConfigProvider>
</StrictMode>);
