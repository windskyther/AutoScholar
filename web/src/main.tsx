import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import 'antd/dist/reset.css';
import './styles.css';
import App from './App';
import { ConnectionProvider } from './connection';
import Appearance from './appearance';

const cache = new QueryClient({ defaultOptions: {
  queries: { retry: false, staleTime: 15000, refetchOnWindowFocus: false, refetchOnReconnect: false },
  mutations: { retry: false },
} });

createRoot(document.getElementById('root')!).render(<StrictMode>
  <Appearance>
    <QueryClientProvider client={cache}><ConnectionProvider><BrowserRouter><App /></BrowserRouter></ConnectionProvider></QueryClientProvider>
  </Appearance>
</StrictMode>);
