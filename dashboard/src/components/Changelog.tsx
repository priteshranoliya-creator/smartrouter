import { Fragment } from "preact";
import { useState } from "preact/hooks";
import { ABOUT, CHANGELOG, type ChangeTag } from "../changelog";

type Filter = "All" | ChangeTag;
const FILTERS: Filter[] = ["All", "Feature", "Decision", "Fix", "Design"];

/** Existing pill tones only: green for features, neutral ink for decisions, amber for fixes, plain for design. */
const TONE: Record<ChangeTag, string> = { Feature: "pass", Decision: "switch", Fix: "verdict miss", Design: "" };

function formatDate(date: string): string {
  return new Date(date + "T00:00:00").toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

export function Changelog() {
  const [filter, setFilter] = useState<Filter>("All");
  const shown = filter === "All" ? CHANGELOG : CHANGELOG.filter((entry) => entry.tag === filter);

  return (
    <>
      <div class="page-intro">
        <h1>Changelog</h1>
        <p class="lede">What was built and decided for smartrouter, newest first.</p>
      </div>

      <section class="card about" aria-labelledby="about-title">
        <h2 id="about-title">{ABOUT.title}</h2>
        <p>{ABOUT.intro}</p>
        <ol class="about-steps">
          {ABOUT.steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
        <div class="flow" role="group" aria-label="Request path">
          {ABOUT.flow.map((node, i) => (
            <Fragment key={node}>
              {i > 0 && (
                <span class="arrow" aria-hidden="true">
                  →
                </span>
              )}
              <span class="flow-node">{node}</span>
            </Fragment>
          ))}
        </div>
        <p>{ABOUT.modes}</p>
      </section>

      <div class="chip-row" role="group" aria-label="Filter changes by type">
        {FILTERS.map((f) => (
          <button
            type="button"
            class={"chip-btn" + (filter === f ? " on" : "")}
            id={"chip-" + f.toLowerCase()}
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
          >
            {f}
          </button>
        ))}
      </div>

      {shown.length === 0 ? (
        <p class="empty">No entries of this type.</p>
      ) : (
        <ol class="timeline" aria-label="Changes, newest first">
          {shown.map((entry) => (
            <li class="tl-item" key={entry.id}>
              <time class="tl-date" dateTime={entry.date}>
                {formatDate(entry.date)}
              </time>
              <span class="tl-rail" aria-hidden="true" />
              <article class="card tl-card" aria-labelledby={"tl-" + entry.id}>
                <div class="badges tl-meta">
                  <span class={"badge" + (TONE[entry.tag] ? " " + TONE[entry.tag] : "")}>{entry.tag}</span>
                  {entry.tickets.map((ticket) => (
                    <span class="badge" key={ticket}>
                      <b>{ticket}</b>
                    </span>
                  ))}
                  {entry.commit && <code>{entry.commit}</code>}
                </div>
                <h3 id={"tl-" + entry.id}>{entry.title}</h3>
                <p class="tl-analogy">{entry.analogy}</p>
                <p class="tl-summary">{entry.summary}</p>
                <ul class="tl-bullets">
                  {entry.bullets.map((bullet) => (
                    <li key={bullet}>{bullet}</li>
                  ))}
                </ul>
              </article>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}
