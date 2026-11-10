import { useState } from 'react';
import { Modal } from 'antd';
import { ArrowRightOutlined, BookOutlined, BranchesOutlined, CloseOutlined, DatabaseOutlined,
  ExperimentOutlined, FilePdfOutlined, FileSearchOutlined, SearchOutlined } from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { usePageReady } from './page-transition';

const researchModes = [
  { name: '资料调研', icon: FileSearchOutlined, tone: 'blue', medium: '论文 · 网页 · PDF', keywords: '搜索 检索 网页 资料 research',
    summary: '从多种来源寻找线索，整理有引用的答案。',
    detail: '适合了解新主题、追踪近期进展，或围绕一个具体问题收集资料。明确时间范围与关注点，让结论更有针对性。',
    example: '梳理近一年多模态检索的研究进展，列出主要方法、适用场景与原始来源。' },
  { name: '论文综述', icon: BookOutlined, tone: 'lilac', medium: '方法 · 脉络 · 开放问题', keywords: '文献 论文 阅读 综述 review literature',
    summary: '梳理研究脉络，读懂方法之间的联系。',
    detail: '适合进入一个研究方向，整理代表性论文、技术演进与尚未解决的问题。可结合论文检索与项目内的 PDF。',
    example: '综述检索增强生成中的证据选择方法，比较代表性论文的思路、局限与开放问题。' },
  { name: '技术对比', icon: BranchesOutlined, tone: 'peach', medium: '维度 · 证据 · 取舍', keywords: '对比 比较 评估 选型 comparison',
    summary: '用统一的维度，找到适合你的方案。',
    detail: '适合技术选型与方法比较。给出候选方案、实际约束和需要比较的维度，研究会结合来源说明优势、局限和适用条件。',
    example: '比较三种向量检索方案，从检索质量、部署成本和维护复杂度分析各自的适用场景。' },
  { name: '研究方案', icon: ExperimentOutlined, tone: 'mint', medium: '假设 · 方法 · 评估', keywords: '设计 方案 假设 实验 plan proposal',
    summary: '把一个想法，展开为可验证的研究路径。',
    detail: '适合把初步问题转化为研究假设、方法与评估流程。可以说明可用数据与资源约束；拟议实验会与已经得到的证据分开表述。',
    example: '为小样本文档分类设计研究方案，说明假设、基线、评估指标与需要排查的风险。' },
  { name: '项目文档分析', icon: FilePdfOutlined, tone: 'rose', medium: '项目 PDF · 原文依据', keywords: 'pdf 文件 文档 本地 阅读 document',
    summary: '围绕自己的资料提问，让回答回到原文。',
    detail: '只根据项目内已经就绪的 PDF 分析和回答。先向项目上传文档，再选择分析范围，适合交叉阅读和整理已有资料。',
    example: '根据项目中的论文，整理各方法使用的数据集与评估设置，并标明对应的原文依据。' },
  { name: '项目证据库', icon: DatabaseOutlined, tone: 'sand', medium: '积累 · 复核 · 复用', keywords: '知识 证据 成果 共享 复用 evidence',
    summary: '让已经核验的发现，成为下一次探索的起点。',
    detail: '在项目的证据库中查看已通过审查的研究成果、原文依据和贡献目标。新研究可先复用相关成果，再补充资料；也可以复核或移除已有证据。',
    example: '在同一项目中继续追问相关问题，结合已有证据补充新的来源和发现。' },
];

export default function HomePage() {
  usePageReady(true);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<(typeof researchModes)[number] | null>(null);
  const search = query.trim().toLocaleLowerCase();
  const visibleModes = researchModes.filter(mode => `${mode.name} ${mode.summary} ${mode.keywords}`.toLocaleLowerCase().includes(search));
  return <div className="home-page">
    <section className="home-intro" aria-labelledby="home-title">
      <div className="home-constellation" aria-hidden="true">{researchModes.map(({ name, icon: Icon, tone }) =>
        <span key={name} className={`home-float tone-${tone}`}><Icon /></span>)}</div>
      <div className="home-copy">
        <h1 id="home-title"><span>让好奇，</span><span>成为新的发现。</span></h1>
        <p>从论文、网页到你的项目资料。<br />让每一个问题，都有据可循。</p>
        <div className="home-actions">
          <Link to="/projects" className="home-primary">开始研究<ArrowRightOutlined aria-hidden="true" /></Link>
          <Link to="/models" className="home-secondary">管理模型<ArrowRightOutlined aria-hidden="true" /></Link>
        </div>
      </div>
    </section>
    <section className="research-library" aria-labelledby="research-library-title">
      <div className="research-library-intro">
        <h2 id="research-library-title">每个问题，<br />都有合适的研究方式。</h2>
        <p>寻找线索、梳理文献，或深入自己的资料。<br />从你想做的事情开始。</p>
        <div className="research-search" role="search">
          <SearchOutlined aria-hidden="true" />
          <input type="search" aria-label="搜索研究方式" placeholder="搜索研究方式，例如：论文、PDF、对比" value={query} onChange={event => setQuery(event.target.value)} />
          {query && <button type="button" aria-label="清空搜索" onClick={() => setQuery('')}><CloseOutlined aria-hidden="true" /></button>}
        </div>
      </div>
      <div className="research-results" aria-live="polite">{search && (visibleModes.length ? `找到 ${visibleModes.length} 种研究方式` : '没有找到相关的研究方式，试试“论文”“资料”或“PDF”。')}</div>
      <div className="research-grid">{visibleModes.map(mode => {
        const Icon = mode.icon;
        return <article key={mode.name} className={`research-card tone-${mode.tone}`}>
          <h3><button type="button" className="research-open" aria-label={`了解${mode.name}`} onClick={() => setSelected(mode)}>{mode.name}</button></h3>
          <div className="research-art" aria-hidden="true"><span className="research-glyph"><Icon /></span><span className="research-medium">{mode.medium}</span></div>
          <p>{mode.summary}</p>
          <ArrowRightOutlined className="research-arrow" aria-hidden="true" />
        </article>;
      })}</div>
    </section>
    <footer className="home-footer"><strong>AutoScholar</strong><span>为每次探索，留下可靠依据。</span></footer>
    <Modal title={selected?.name} open={!!selected} onCancel={() => setSelected(null)} footer={null}>
      {selected && <div className="research-guide"><p>{selected.detail}</p><h3>可以这样开始</h3><blockquote>{selected.example}</blockquote>
        <p>{selected.name === '项目证据库' ? '进入项目后，打开项目证据库查看已有成果。' : '进入项目后，创建研究任务并选择对应的任务类型。'}</p>
        <Link to="/projects" className="home-primary" onClick={() => setSelected(null)}>查看项目<ArrowRightOutlined aria-hidden="true" /></Link>
      </div>}
    </Modal>
  </div>;
}
