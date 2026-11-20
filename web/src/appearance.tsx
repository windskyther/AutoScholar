import { createContext, useContext, useLayoutEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { ConfigProvider, theme } from 'antd';
import type { ThemeConfig } from 'antd';
import { MoonOutlined, SunOutlined } from '@ant-design/icons';
import zhCN from 'antd/locale/zh_CN';

type Mode = 'light' | 'dark';
const storageKey = 'autoscholar-theme';
const ThemeContext = createContext<{ dark: boolean; toggle: () => void } | null>(null);

const lightColors = {
  page: '#ffffff', surface: '#ffffff', elevated: '#ffffff', inset: '#f7f7f7',
  ink: '#141414', muted: '#696969', placeholder: '#737373',
  border: '#d9d9d9', borderSoft: '#e8e8e8', borderHover: '#989898',
  link: '#0066cc', linkHover: '#004f9e', primaryText: '#ffffff', rowHover: '#fafafa',
  selectedOption: '#222225', selectedOptionHover: '#37373d', selectedOptionText: '#ffffff',
};
const darkColors = {
  page: '#141416', surface: '#1f2023', elevated: '#26272b', inset: '#27282c',
  ink: '#f1f1f3', muted: '#b4b4bc', placeholder: '#a2a3ad',
  border: '#55565f', borderSoft: '#34353d', borderHover: '#8a8b94',
  link: '#8cbdff', linkHover: '#b3d4ff', primaryText: '#141416', rowHover: '#2b2c31',
  selectedOption: '#3e3f46', selectedOptionHover: '#505159', selectedOptionText: '#f1f1f3',
};

export default function Appearance({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(() => {
    try { return localStorage.getItem(storageKey) === 'dark' ? 'dark' : 'light'; }
    catch { return 'light'; }
  });
  const dark = mode === 'dark';
  const colors = dark ? darkColors : lightColors;
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = mode;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', colors.page);
    try {
      if (mode === 'dark') localStorage.setItem(storageKey, mode);
      else localStorage.removeItem(storageKey);
    } catch { /* Storage may be disabled. */ }
  }, [mode, colors.page]);

  const config: ThemeConfig = {
    algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: colors.ink, colorInfo: colors.link,
      colorLink: colors.link, colorLinkHover: colors.linkHover,
      colorText: colors.ink, colorTextSecondary: colors.muted, colorTextPlaceholder: colors.placeholder,
      colorBgBase: colors.page, colorBgContainer: colors.surface, colorBgElevated: colors.elevated,
      colorBorder: colors.border, colorBorderSecondary: colors.borderSoft,
      colorFillAlter: colors.inset, borderRadius: 10, borderRadiusLG: 20, fontSize: 16, controlHeight: 44,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Helvetica Neue", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
      boxShadow: dark ? '0 12px 40px rgb(0 0 0 / .35)' : '0 12px 40px rgb(0 0 0 / .08)',
    },
    components: {
      Button: { primaryColor: colors.primaryText, primaryShadow: '0 3px 8px rgb(0 0 0 / .06)', fontWeight: 500 },
      Table: {
        headerBg: colors.inset, headerColor: colors.muted, headerSplitColor: 'transparent', headerBorderRadius: 10,
        borderColor: colors.borderSoft, rowHoverBg: colors.rowHover, fontSize: 14,
        cellPaddingBlock: 11, cellPaddingBlockSM: 9, cellPaddingInline: 14, cellPaddingInlineSM: 14,
      },
      Modal: { contentBg: colors.elevated, headerBg: colors.elevated, borderRadiusLG: 24 },
      Segmented: { trackBg: colors.inset, itemSelectedBg: colors.surface },
      Select: {
        optionSelectedBg: colors.selectedOption, controlItemBgActiveHover: colors.selectedOptionHover,
        optionSelectedColor: colors.selectedOptionText,
      },
      Input: { activeBorderColor: colors.link, hoverBorderColor: colors.borderHover },
    },
  };
  return <ThemeContext.Provider value={{ dark, toggle: () => setMode(current => current === 'dark' ? 'light' : 'dark') }}>
    <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={config}>{children}</ConfigProvider>
  </ThemeContext.Provider>;
}

export function ThemeToggle() {
  const appearance = useContext(ThemeContext);
  if (!appearance) return null;
  const { dark, toggle } = appearance;
  const label = dark ? '切换到浅色模式' : '切换到夜间模式';
  return <button type="button" className="theme-toggle" aria-label={label} title={label} onClick={toggle}>
    {dark ? <SunOutlined aria-hidden="true" /> : <MoonOutlined aria-hidden="true" />}
  </button>;
}
