import type { ComponentChildren } from "preact";

interface CardProps {
  id: string;
  title: string;
  subtitle: string;
  icon: ComponentChildren;
  highlight?: boolean;
  wide?: boolean;
  meta?: ComponentChildren;
  children: ComponentChildren;
}

export function Card({ id, title, subtitle, icon, highlight, wide, meta, children }: CardProps) {
  const cls = "card" + (highlight ? " hl" : "") + (wide ? " wide" : "");
  return (
    <section class={cls} id={id}>
      <div class="card-top">
        <span class="icon-sq">{icon}</span>
        {meta}
      </div>
      <div class="card-body">{children}</div>
      <div class="card-foot">
        <h3>{title}</h3>
        <p>{subtitle}</p>
      </div>
    </section>
  );
}
