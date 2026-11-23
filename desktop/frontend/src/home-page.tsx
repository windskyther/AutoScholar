import { ArrowRightOutlined, BookOutlined, BranchesOutlined, DatabaseOutlined,
  ExperimentOutlined, FilePdfOutlined, FileSearchOutlined } from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { usePageReady } from './page-transition';
import { researchEntryLabels } from './research-entry';

const researchModes = [
  { entry: 'research', icon: FileSearchOutlined, tone: 'blue' },
  { entry: 'literature_review', icon: BookOutlined, tone: 'lilac' },
  { entry: 'comparison', icon: BranchesOutlined, tone: 'peach' },
  { entry: 'research_proposal', icon: ExperimentOutlined, tone: 'mint' },
  { entry: 'document_analysis', icon: FilePdfOutlined, tone: 'rose' },
  { entry: 'evidence', icon: DatabaseOutlined, tone: 'sand' },
] as const;

export default function HomePage() {
  usePageReady(true);
  return <div className="home-page">
    <section className="home-intro" aria-labelledby="home-title">
      <div className="home-constellation" aria-hidden="true">{researchModes.map(({ entry, icon: Icon, tone }) =>
        <span key={entry} className={`home-float tone-${tone}`}><Icon /></span>)}</div>
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
      <div className="research-grid">{researchModes.map(({ entry, tone, icon: Icon }) =>
        <article key={entry} className={`research-card tone-${tone}`}>
          <h3><Link to={`/projects?research=${entry}`} className="research-open">{researchEntryLabels[entry]}</Link></h3>
          <div className="research-art" aria-hidden="true"><span className="research-glyph"><Icon /></span></div>
          <ArrowRightOutlined className="research-arrow" aria-hidden="true" />
        </article>
      )}</div>
    </section>
  </div>;
}
