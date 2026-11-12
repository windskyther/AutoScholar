import type { ReactNode } from 'react';
import { ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';

export default function Appearance({ children }: { children: ReactNode }) {
  return <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={{
    algorithm: theme.defaultAlgorithm,
    token: {
      colorPrimary: '#141414', colorInfo: '#0066cc',
      colorLink: '#0066cc', colorLinkHover: '#004f9e',
      colorText: '#141414', colorTextSecondary: '#696969', colorTextPlaceholder: '#737373',
      colorBgContainer: '#ffffff', colorBgElevated: '#ffffff',
      colorBorder: '#d9d9d9', colorBorderSecondary: '#e8e8e8',
      colorFillAlter: '#f7f7f7', borderRadius: 10, borderRadiusLG: 20, fontSize: 16, controlHeight: 44,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Helvetica Neue", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
      boxShadow: '0 12px 40px rgb(0 0 0 / 0.08)',
    },
    components: {
      Button: { primaryColor: '#ffffff', primaryShadow: '0 3px 8px rgb(0 0 0 / 0.06)', fontWeight: 500 },
      Table: { headerBg: '#f7f7f7', headerColor: '#141414', rowHoverBg: '#fafafa', cellPaddingBlock: 18, fontSize: 15 },
      Modal: { contentBg: '#ffffff', headerBg: '#ffffff', borderRadiusLG: 24 },
      Segmented: { trackBg: '#f7f7f7', itemSelectedBg: '#ffffff' },
      Input: { activeBorderColor: '#0066cc', hoverBorderColor: '#989898' },
    },
  }}>{children}</ConfigProvider>;
}
