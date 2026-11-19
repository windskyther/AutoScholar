import { ArrowRightOutlined, BookOutlined, BranchesOutlined, DatabaseOutlined,
  ExperimentOutlined, FilePdfOutlined, FileSearchOutlined } from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { usePageReady } from './page-transition';

const researchModes = [
  { name: '资料调研', icon: FileSearchOutlined, tone: 'blue' },
  { name: '论文综述', icon: BookOutlined, tone: 'lilac' },
  { name: '技术对比', icon: BranchesOutlined, tone: 'peach' },
  { name: '研究方案', icon: ExperimentOutlined, tone: 'mint' },
  { name: '项目文档分析', icon: FilePdfOutlined, tone: 'rose' },
  { name: '项目证据库', icon: DatabaseOutlined, tone: 'sand' },
];

export default function HomePage() {
  usePageReady(true);
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
      <h2 id="research-library-title">研究方式</h2>
      <div className="research-grid">{researchModes.map(({ name, tone, icon: Icon }) =>
        <article key={name} className={`research-card tone-${tone}`}>
          <h3><Link to="/projects" className="research-open">{name}</Link></h3>
          <div className="research-art" aria-hidden="true"><span className="research-glyph"><Icon /></span></div>
          <ArrowRightOutlined className="research-arrow" aria-hidden="true" />
        </article>
      )}</div>
    </section>
  </div>;
}
