import type { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import { IconChevron } from "./icons";

/** A collapsed block that mounts its children only when opened, so hidden charts never measure a zero-size box. */
export function Disclosure({ summary, hint, children }: { summary: string; hint?: string; children: ComponentChildren }) {
  const [open, setOpen] = useState(false);
  return (
    <div class={"disclosure" + (open ? " open" : "")}>
      <button type="button" class="disc-btn" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span class="chev">
          <IconChevron />
        </span>
        {summary}
        {hint && <span class="muted disc-hint">{hint}</span>}
      </button>
      {open && <div class="disc-body">{children}</div>}
    </div>
  );
}
