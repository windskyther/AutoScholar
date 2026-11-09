import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';

export default function Appearance({ children }: { children: ReactNode }) {
  const [dark, setDark] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches);
  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const update = () => setDark(query.matches);
    update(); query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  const surface = dark ? '#242426' : '#ffffff';
  const inset = dark ? '#303033' : '#f5f5f7';
  const foreground = dark ? '#f5f5f7' : '#1d1d1f';
  return <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={{
    algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: foreground, colorInfo: dark ? '#78b5ff' : '#0066cc',
      colorLink: dark ? '#78b5ff' : '#0066cc', colorLinkHover: dark ? '#a5cdff' : '#004f9e',
      colorText: foreground, colorTextSecondary: dark ? '#b6b6bf' : '#606068',
      colorTextPlaceholder: dark ? '#a4a4ae' : '#73737c',
      colorBgContainer: surface, colorBgElevated: dark ? '#2c2c2f' : '#ffffff',
      colorBorder: dark ? '#54545c' : '#d1d1d8', colorBorderSecondary: dark ? '#414146' : '#e7e7ec',
      colorFillAlter: inset, borderRadius: 10, borderRadiusLG: 18, fontSize: 15, controlHeight: 40,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
      boxShadow: '0 12px 40px rgb(0 0 0 / 0.12)',
    },
    components: {
      Button: { primaryColor: dark ? '#1d1d1f' : '#ffffff', primaryShadow: '0 3px 8px rgb(0 0 0 / 0.10)', fontWeight: 500 },
      Table: { headerBg: inset, headerColor: foreground, rowHoverBg: dark ? '#303034' : '#f8f8fa', cellPaddingBlock: 16 },
      Modal: { contentBg: surface, headerBg: surface, borderRadiusLG: 24 },
      Segmented: { trackBg: inset, itemSelectedBg: surface },
      Input: { activeBorderColor: dark ? '#78b5ff' : '#0066cc', hoverBorderColor: dark ? '#8e8e98' : '#9898a3' },
    },
  }}>{children}</ConfigProvider>;
}
