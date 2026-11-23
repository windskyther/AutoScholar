import { useEffect, useId, useRef, useState } from 'react';
import type { ReactNode, CSSProperties } from 'react';
import { createRefractionMap } from './glass-refraction';
import type { RefractionMap } from './glass-refraction';

// SVG backdrop filters have been visually verified on Chromium. Other engines
// retain the CSS material instead of relying on syntax support as a render test.
function canRefract() {
  return /Chrom(?:e|ium)\/|Edg\//.test(navigator.userAgent)
    && typeof CSS !== 'undefined' && CSS.supports('backdrop-filter', 'url("#glass")');
}

export default function GlassSurface({ children }: { children: ReactNode }) {
  const surface = useRef<HTMLDivElement>(null);
  const id = 'glass-' + useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const [map, setMap] = useState<RefractionMap | null>(null);
  useEffect(() => {
    const element = surface.current;
    if (!element || !canRefract()) return;
    const preferences = [
      '(prefers-reduced-motion: reduce)', '(prefers-reduced-transparency: reduce)',
      '(prefers-contrast: more)', '(forced-colors: active)',
    ].map(query => window.matchMedia(query));
    let frame = 0;
    let previous = '';
    let disposed = false;
    function update() {
      frame = 0;
      if (disposed || !element) return;
      if (preferences.some(preference => preference.matches)) {
        previous = ''; setMap(null); return;
      }
      const width = element.clientWidth;
      const height = element.clientHeight;
      const radius = parseFloat(getComputedStyle(element).borderTopLeftRadius) || 0;
      const key = `${width}:${height}:${radius}`;
      if (previous === key) return;
      previous = key;
      try { setMap(createRefractionMap(width, height, radius)); }
      catch { setMap(null); }
    }
    function schedule() {
      if (!disposed && !frame) frame = requestAnimationFrame(update);
    }
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(schedule) : null;
    observer?.observe(element);
    window.addEventListener('resize', schedule);
    preferences.forEach(preference => preference.addEventListener('change', schedule));
    schedule();
    return () => {
      disposed = true;
      observer?.disconnect();
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', schedule);
      preferences.forEach(preference => preference.removeEventListener('change', schedule));
    };
  }, []);
  const style: CSSProperties | undefined = map ? {
    backdropFilter: `url("#${id}") blur(3px) saturate(1.1)`,
    WebkitBackdropFilter: `url("#${id}") blur(3px) saturate(1.1)`,
  } : undefined;
  return <div ref={surface} className="glass-surface" data-refraction={map ? 'svg' : 'css'}>
    {map && <svg className="glass-definitions" width="0" height="0" aria-hidden="true" focusable="false">
      <defs><filter id={id} x="0" y="0" width={map.width} height={map.height}
        filterUnits="userSpaceOnUse" colorInterpolationFilters="sRGB">
        <feImage href={map.url} width={map.width} height={map.height} preserveAspectRatio="none" result="refraction-map" />
        <feDisplacementMap in="SourceGraphic" in2="refraction-map" scale={map.scale} xChannelSelector="R" yChannelSelector="G" />
      </filter></defs>
    </svg>}
    <span className="glass-backdrop" style={style} aria-hidden="true" />
    <span className="glass-rim" aria-hidden="true" />
    <div className="glass-content">{children}</div>
  </div>;
}
