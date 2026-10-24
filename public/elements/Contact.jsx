// «Куда обратиться», когда в базе знаний недостаточно информации. props: label, unit, basis
import { Building2 } from 'lucide-react';

export default function Contact() {
  return (
    <div className="g-card g-contact">
      <Building2 className="g-contact__icon" strokeWidth={1.5} aria-hidden="true" />
      <div>
        <div className="g-micro">{props.label}</div>
        <div className="g-contact__unit">{props.unit}</div>
        {props.basis ? <div className="g-caption">{props.basis}</div> : null}
      </div>
    </div>
  );
}
