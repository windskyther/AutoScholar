import { ArrowRightOutlined } from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { usePageReady } from './page-transition';

export default function HomePage() {
  usePageReady(true);
  return <section className="home-intro" aria-labelledby="home-title">
    <h1 id="home-title"><span>让好奇，</span><span>成为新的发现。</span></h1>
    <p>从一个问题出发，连接资料、证据与思考。<br />在这里，展开你的下一次研究。</p>
    <div className="home-actions">
      <Link to="/projects" className="home-primary">开始研究<ArrowRightOutlined aria-hidden="true" /></Link>
      <Link to="/models" className="home-secondary">管理模型<ArrowRightOutlined aria-hidden="true" /></Link>
    </div>
  </section>;
}
