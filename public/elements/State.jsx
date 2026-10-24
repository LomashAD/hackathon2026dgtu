// Заголовок состояния ответа: уточняющий вопрос, шаг заполнения шаблона, готовый черновик, нет ответа, проверка.
// props: kind (clarify | fill | ready | refused | review), label, title, step, total
export default function State() {
  const { kind, label, title, step, total } = props;
  return (
    <div className={`g-state g-state--${kind}`}>
      <div className="g-state__row">
        <span className="g-state__dot" aria-hidden="true" />
        <span className="g-micro">{label}</span>
        {total ? <span className="g-state__count">{step} / {total}</span> : null}
      </div>
      {title ? <div className="g-state__title">{title}</div> : null}
      {total ? (
        <div className="g-steps" role="progressbar" aria-label={`Вопрос ${step} из ${total}`}
             aria-valuemin={1} aria-valuemax={total} aria-valuenow={step}>
          {Array.from({ length: total }, (_, i) => (
            <span key={i} className={i < step - 1 ? 'done' : i === step - 1 ? 'current' : ''} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
