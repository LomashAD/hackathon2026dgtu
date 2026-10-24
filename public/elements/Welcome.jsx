// Приветствие чата (DESIGN.md: hero — заголовок, пояснение, «полый светящийся многоугольник» справа).
// Данные — props из app/main.py: title, lead, role, label, examples [{tag, label, text}], hint.
// Пример по нажатию уходит в чат как вопрос пользователя.
import { ArrowRight, BookOpenText, FilePen, ListChecks } from 'lucide-react';

const ICONS = [BookOpenText, ListChecks, FilePen];

function Art() {
  // Luminous Polygon Graphic: повёрнутые скруглённые квадраты, белые линии, размытые копии и редкий акцент спектра
  const sq = (r, s, o, extra = {}) => (
    <rect x={110 - s / 2} y={110 - s / 2} width={s} height={s} rx="16" fill="none"
          transform={`rotate(${r} 110 110)`} strokeOpacity={o} {...extra} />
  );
  return (
    <svg className="g-welcome__art" viewBox="0 0 220 220" aria-hidden="true">
      <defs>
        <linearGradient id="g-welcome-spectrum" x1="0" y1="0" x2="1" y2="0">
          <stop offset=".15" stopColor="#ffb931" /><stop offset=".3" stopColor="#ffffff" />
          <stop offset=".4" stopColor="#ffffff" /><stop offset=".5" stopColor="#2e73fc" />
        </linearGradient>
        <filter id="g-welcome-blur"><feGaussianBlur stdDeviation="4" /></filter>
      </defs>
      <g stroke="var(--g-text)" strokeWidth="1.5">
        <g filter="url(#g-welcome-blur)">{sq(-14, 120, 0.35)}</g>
        {sq(-14, 120, 0.9)}
        {sq(8, 96, 0.45)}
        {sq(30, 150, 0.18)}
      </g>
      {sq(22, 72, 1, { stroke: 'url(#g-welcome-spectrum)', strokeWidth: 1.5 })}
    </svg>
  );
}

export default function Welcome() {
  const examples = props.examples || [];
  return (
    <section className="g-welcome" aria-label={props.title}>
      <Art />
      <div className="g-welcome__body">
        <span className="g-badge"><span>Роль: <b>{props.role}</b></span></span>
        <h1 className="g-welcome__title">{props.title}</h1>
        <p className="g-welcome__lead">{props.lead}</p>
      </div>
      <div className="g-micro g-welcome__label">{props.label}</div>
      <div className="g-welcome__grid">
        {examples.map((e, i) => {
          const Icon = ICONS[i % ICONS.length];
          return (
            <button key={i} type="button" className="g-card g-example" title={e.text}
                    onClick={() => sendUserMessage(e.text)}>
              <Icon className="g-example__icon" strokeWidth={1.5} aria-hidden="true" />
              <span className="g-caption">{e.tag}</span>
              <span className="g-example__text">{e.label}</span>
              <span className="g-link">Спросить <ArrowRight aria-hidden="true" /></span>
            </button>
          );
        })}
      </div>
      <p className="g-welcome__hint">
        {(props.hint || []).map((part, i) => (i % 2 ? <b key={i}>{part}</b> : <span key={i}>{part}</span>))}
      </p>
    </section>
  );
}
