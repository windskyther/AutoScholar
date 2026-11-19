import { useState } from 'react';
import { ArrowRightOutlined, BookOutlined, BranchesOutlined, CloseOutlined, DatabaseOutlined,
  ExperimentOutlined, FilePdfOutlined, FileSearchOutlined, SearchOutlined } from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { usePageReady } from './page-transition';

const researchModes = [
  { name: '资料调研', icon: FileSearchOutlined, tone: 'blue', keywords: '搜索 检索 网页 资料 research' },
  { name: '论文综述', icon: BookOutlined, tone: 'lilac', keywords: '文献 论文 阅读 综述 review literature' },
  { name: '技术对比', icon: BranchesOutlined, tone: 'peach', keywords: '对比 比较 评估 选型 comparison' },
  { name: '研究方案', icon: ExperimentOutlined, tone: 'mint', keywords: '设计 方案 假设 实验 plan proposal' },
  { name: '项目文档分析', icon: FilePdfOutlined, tone: 'rose', keywords: 'pdf 文件 文档 本地 阅读 document' },
  { name: '项目证据库', icon: DatabaseOutlined, tone: 'sand', keywords: '知识 证据 成果 共享 复用 evidence' },
];

export default function HomePage() {
  usePageReady(true);
  const [query, setQuery] = useState('');
  const search = query.trim().toLocaleLowerCase();
  const visibleModes = researchModes.filter(mode => `${mode.name} ${mode.keywords}`.toLocaleLowerCase().includes(search));
  return <div className="home-page">
    <section className="home-intro" aria-labelledby="home-title">
      <div className="home-constellation" aria-hidden="true">{researchModes.map(({ name, icon: Icon, tone }) =>
        <span key={name} className={`home-float tone-${tone}`}><Icon /></span>)}</div>
      <div className="home-copy">
        <h1 id="home-title">AutoScholar</h1>
        <div className="home-actions">
          <Link to="/projects" className="home-primary">开始研究<ArrowRightOutlined aria-hidden="true" /></Link>
          <Link to="/models" className="home-secondary">管理模型<ArrowRightOutlined aria-hidden="true" /></Link>
        </div>
      </div>
    </section>
    <section className="research-library" aria-labelledby="research-library-title">
      <div className="research-library-intro">
        <div className="research-library-copy">
          <h2 id="research-library-title">研究方式</h2>
        </div>
        <div className="research-search" role="search">
          <SearchOutlined aria-hidden="true" />
          <input type="search" aria-label="搜索研究方式" placeholder="搜索研究方式" value={query} onChange={event => setQuery(event.target.value)} />
          {query && <button type="button" aria-label="清空搜索" onClick={() => setQuery('')}><CloseOutlined aria-hidden="true" /></button>}
        </div>
      </div>
      <div className="research-results" aria-live="polite">{search && (visibleModes.length ? `${visibleModes.length} 项` : '无匹配结果')}</div>
      <div className="research-grid">{visibleModes.map(mode => {
        const Icon = mode.icon;
        return <article key={mode.name} className={`research-card tone-${mode.tone}`}>
          <h3><Link to="/projects" className="research-open">{mode.name}</Link></h3>
          <div className="research-art" aria-hidden="true"><span className="research-glyph"><Icon /></span></div>
          <ArrowRightOutlined className="research-arrow" aria-hidden="true" />
        </article>;
      })}</div>
    </section>
  </div>;
}
