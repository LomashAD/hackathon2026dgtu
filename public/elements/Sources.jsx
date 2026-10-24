// Источники ответа: документ, редакция, пункт или приложение, процент релевантности — из Reply.sources.
// props: title, latency, items [{n, where, doc, version, date, head, percent, related, archived, basis}]
export default function Sources() {
  const items = props.items || [];
  return (
    <section className="g-sources" aria-label={props.title}>
      <div className="g-sources__head">
        <span className="g-micro">{props.title}</span>
        <span className="g-caption">{items.length}</span>
        {props.latency ? <span className="g-caption g-sources__latency">{props.latency}</span> : null}
      </div>
      <ol className="g-sources__list">
        {items.map((s) => (
          <li key={s.n} className="g-card g-source">
            <span className="g-source__n">{s.n}</span>
            <div>
              <div className="g-source__where">
                {s.where}
                {s.archived ? <span className="g-badge g-badge--archived">архивная редакция</span> : null}
              </div>
              {s.doc ? (
                <div className="g-source__doc">
                  {[s.doc, s.version && `ред. ${s.version}`, s.date && `с ${s.date}`].filter(Boolean).join(' · ')}
                </div>
              ) : null}
              {s.head ? <div className="g-source__head">{s.head}</div> : null}
              {s.related ? <div className="g-source__head">связан с п. {s.related}</div> : null}
            </div>
            {s.percent != null ? (
              <div className="g-rel" title={`Релевантность запросу: ${s.percent} %`}>
                <span className="g-rel__label">релевантность</span>
                <span className="g-rel__value">{s.percent} %</span>
                <span className="g-rel__bar"><span style={{ width: `${Math.max(0, Math.min(100, s.percent))}%` }} /></span>
              </div>
            ) : (
              <div className="g-rel g-rel--none">
                <span className="g-rel__label">{s.basis || 'основание'}</span>
              </div>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
