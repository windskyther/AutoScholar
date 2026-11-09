import { ApiOutlined, BarChartOutlined, FolderOpenOutlined } from '@ant-design/icons';
import { Link, useLocation } from 'react-router-dom';
import GlassSurface from './glass-surface';

const destinations = [
  { path: '/projects', label: '项目与任务', icon: FolderOpenOutlined, section: 'projects' },
  { path: '/models', label: '模型管理', icon: ApiOutlined, section: 'models' },
  { path: '/usage', label: '用量统计', icon: BarChartOutlined, section: 'usage' },
];

export default function WorkspaceDock() {
  const { pathname } = useLocation();
  const selected = pathname === '/models' ? 'models' : pathname === '/usage' ? 'usage'
    : pathname === '/projects' || pathname.startsWith('/projects/') ? 'projects' : null;
  return <nav className="workspace-dock" aria-label="主导航">
    <GlassSurface><div className="dock-items">{destinations.map(({ path, label, icon: Icon, section }) =>
      <Link key={path} to={path} className={`dock-item${selected === section ? ' is-current' : ''}`}
        aria-current={selected === section ? 'page' : undefined}>
        <span className="dock-icon" aria-hidden="true"><Icon /></span>
        <span className="dock-label">{label}</span>
        <span className="dock-selection" aria-hidden="true" />
      </Link>
    )}</div></GlassSurface>
  </nav>;
}
