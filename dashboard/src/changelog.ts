// Static changelog for the dashboard.
// Built from `git log --format='%ad %s' --date=short` and docs/tdd-changes.md (decision rows 1-34).
// Entries are newest first. Each decision row becomes a plain-words bullet in the entry it belongs to.
// Keep this file free of personal paths, emails and keys.

export type ChangeTag = "Feature" | "Decision" | "Fix" | "Design";

export interface ChangeEntry {
  id: string;
  /** YYYY-MM-DD */
  date: string;
  tag: ChangeTag;
  tickets: string[];
  title: string;
  /** One line: which part of the picture this step built, in everyday terms. */
  analogy: string;
  summary: string;
  /** 2 to 4 short bullets. */
  bullets: string[];
  /** Short git hash of the main commit for this entry. */
  commit?: string;
}

export interface About {
  title: string;
  intro: string;
  steps: string[];
  flow: string[];
  modes: string;
}

export const ABOUT: About = {
  title: "What is smartrouter?",
  intro:
    "smartrouter sits between Claude Code and Anthropic and picks the model for each request. Think of a receptionist who reads each request and sends it to the right specialist: Opus is the senior expert for the hardest work, Sonnet the regular staff, and Haiku the junior for simple tasks.",
  steps: [
    "Claude Code sends each request to the router, not straight to Anthropic.",
    "A small hook tells the router about each new prompt, and a decision engine rates how hard the work is.",
    "The policy weighs the switch. Changing model is like handing a file to a new person who must re-read all of it, so a switch has to be worth that cost.",
    "The proxy forwards the request to Anthropic. Only in active mode is it sent to the model the router chose.",
    "Every request and decision is logged, and this dashboard shows it.",
  ],
  flow: ["Claude Code", "smartrouter", "Anthropic (Opus, Sonnet or Haiku)"],
  modes:
    "Off passes everything through. Shadow only watches and records what it would have done. Active really switches models.",
};

export const CHANGELOG: ChangeEntry[] = [
  {
    id: "2026-10-21-dashboard-revamp",
    date: "2026-10-21",
    tag: "Design",
    tickets: [],
    title: "Dashboard revamp",
    analogy: "The window's glass: the same view, cleaned up so anyone can read it.",
    summary: "The dashboard opens on a plain-language Overview, and the detailed numbers moved to a Technical details tab.",
    bullets: [
      "Two tabs: Overview for everyday questions, Technical details for the raw numbers.",
      "Cards use plain words, and on phones the sidebar becomes a bar at the top.",
      "The page no longer jumps while it loads, and the feed count matches the switch ledger.",
      "Decision quality now counts engine errors as well as timeouts.",
    ],
    commit: "c60e44f",
  },
  {
    id: "2026-10-20-decision-error",
    date: "2026-10-20",
    tag: "Fix",
    tickets: [],
    title: "Engine errors are no longer logged as slow answers",
    analogy: "The receptionist's sick-day flag: a broken engine is reported as broken, not as slow.",
    summary: "When the decision engine fails outright, the router records a failure instead of calling it a timeout.",
    bullets: [
      "Before this, an engine error looked like a slow answer, which hid real faults.",
      "Errors show in the decisions-by-rule breakdown; the timeout count now holds only real timeouts.",
      "A one-time database update makes room for the new label on existing records.",
    ],
    commit: "1f40789",
  },
  {
    id: "t6-2-active-mode",
    date: "2026-10-19",
    tag: "Feature",
    tickets: ["T6.2"],
    title: "Active mode: the router switches models",
    analogy: "The switchboard: calls are actually moved to the chosen specialist, not just noted.",
    summary:
      "In active mode the router rewrites the model on a request without waiting for the engine, and it falls back safely when a model refuses.",
    bullets: [
      "The turn's first request goes out at once; a decision already ready applies to it, otherwise the choice applies from the next request.",
      "Only main requests are rewritten, and a tier named in the prompt still counts.",
      "A model that refuses the request (400 or 404) gets one retry on the original, and the rest of the turn stays there.",
      "Each request records how long the router held it, and rows are saved after the reply, so logging never adds to the wait.",
    ],
    commit: "4563ab8",
  },
  {
    id: "t6-1-go-no-go",
    date: "2026-10-19",
    tag: "Decision",
    tickets: ["T6.1"],
    title: "Go/no-go review and the active-mode decision",
    analogy: "The sign-off: a written check on the record before the switchboard is allowed to move calls.",
    summary:
      "Active mode was only allowed after a written review checked a week of shadow data and the eval results against the targets.",
    bullets: [
      "The review counts only after at least 7 days and 50 turns of shadow decisions, so one busy afternoon cannot pass it.",
      "Added delay is estimated per turn as the engine's time minus the head start the router gets from reading the prompt early.",
      "Active mode is refused unless the config explicitly allows it, so turning it on is a deliberate step.",
    ],
    commit: "834ebb1",
  },
  {
    id: "t6-3-service",
    date: "2026-10-19",
    tag: "Feature",
    tickets: ["T6.3"],
    title: "Runs as a background service on the Mac",
    analogy: "The night porter: the router keeps working on the Mac with no terminal window open.",
    summary: "The router can be installed as a macOS background service, so it keeps running after the terminal is closed.",
    bullets: [
      "Install, uninstall and status are separate commands, so the service can be checked at any time.",
      "It uses macOS's built-in launch system, so nothing extra has to be installed.",
    ],
    commit: "6f4d375",
  },
  {
    id: "t5-3-runner",
    date: "2026-10-19",
    tag: "Feature",
    tickets: ["T5.3", "T5.4", "T5.5"],
    title: "Eval runner, calibration and the eval report",
    analogy: "The marking: each answer is checked against the known right answer, and the report shows the score.",
    summary:
      "The eval runs the same test questions through the router and through fixed reference runs, marks each answer, and shows the results in the Eval panel.",
    bullets: [
      "Calibration tunes how sure the router must be before it switches, using only the complexity question, since that is the only one the test set labels.",
      "A stronger judge, Opus, re-marks a fixed sample to check the first judge's marks.",
      "The dashboard was rebuilt in a modern framework; the built files are committed, so the router runs without extra tools.",
    ],
    commit: "c5716a9",
  },
  {
    id: "t5-1-dataset",
    date: "2026-10-19",
    tag: "Feature",
    tickets: ["T5.1", "T5.2"],
    title: "Test set and eval providers",
    analogy: "The exam: a fixed set of real questions with known right answers, and the markers who check them.",
    summary: "A test set of questions with known right answers is loaded and checked, and the eval can call Claude and Gemini.",
    bullets: [
      "Claude is called through the Claude command line with a fixed short system prompt, so no API key is needed.",
      "All current Gemini models are in the pool; two run by default and the rest are optional.",
      "The question set stays out of the public repository, because it is drafted from real prompts.",
    ],
    commit: "20bdb43",
  },
  {
    id: "t4-dashboard",
    date: "2026-10-19",
    tag: "Design",
    tickets: ["T4.1", "T4.2", "T4.3", "T4.4"],
    title: "The live dashboard",
    analogy: "The window into the office: the dashboard.",
    summary: "A live page shows spend, decisions, switches and model mix, and it updates as turns happen.",
    bullets: [
      "The page reads from the router's own read-only APIs, and a live stream pushes new turns as they arrive.",
      "Mode can be switched from the page: off and shadow work, while active stays locked until the review.",
      "An export writes one row per request to a CSV file that matches the totals on screen.",
      "Follow-up fixes covered light-theme contrast, the active sidebar item, and a browser check that clicks every control.",
    ],
    commit: "59f876a",
  },
  {
    id: "t3-3-policy",
    date: "2026-10-19",
    tag: "Decision",
    tickets: ["T3.3", "T3.4"],
    title: "Routing rules and shadow decisions",
    analogy: "The rulebook: the rules that turn a judgement into a switch or a stay.",
    summary:
      "Seven ordered rules decide each turn. In shadow mode the router records what it would have decided, and changes nothing.",
    bullets: [
      "The cache is tracked per model and per session; the saved context lasts one hour on this account, so switching back to a recent model costs almost nothing.",
      "A switch needs enough confidence and must pay back the cost of re-reading the file; when no rule fires, the decision is to stay.",
      "Shadow mode prices a model the session never used as cold, so the savings it reports are conservative. A decision not ready in time is logged as a timeout.",
    ],
    commit: "b3c2463",
  },
  {
    id: "t3-1-engines",
    date: "2026-10-19",
    tag: "Feature",
    tickets: ["T3.1", "T3.2"],
    title: "Decision engines behind one interface",
    analogy: "The receptionist's judgement: deciding who should handle each request.",
    summary:
      "ScaleDown rates how hard each request is; Laya can be plugged in behind the same interface as a second opinion.",
    bullets: [
      "ScaleDown is asked one question per turn, how complex the work is, because each call takes about 1.2 seconds.",
      "Engines report a chance for each label, and the policy decides from the chance that the tier is right, since Laya's own confidence score never rose above 0.26.",
      "The time allowed for a decision comes from the config and is sized for the network call, so the 300 ms latency goal is waived for this proof of concept.",
      "Long prompts are shortened first, and facts are dropped only after that, so each engine gets a fair share of its token budget.",
    ],
    commit: "a37736c",
  },
  {
    id: "t2-3-sessions",
    date: "2026-10-19",
    tag: "Feature",
    tickets: ["T2.3", "T2.4"],
    title: "Sessions and turns, and project install",
    analogy: "The door key: only projects that opt in are wired to the front desk.",
    summary:
      "Each conversation and turn is tracked, and the install command wires up one project at a time, refusing anything that looks broken.",
    bullets: [
      "Install is opt-in per project; only those projects send prompts to the outside classifier, and all other projects are left untouched.",
      "Install writes its settings to a local, git-ignored file, not the shared project file.",
      "Compaction and sub-agent requests are labelled, so they are not counted as ordinary turns.",
      "Install refuses a missing project folder or a broken settings file, and says why.",
    ],
    commit: "c20abfa",
  },
  {
    id: "t2-1-proxy",
    date: "2026-10-19",
    tag: "Feature",
    tickets: ["T2.1", "T2.2"],
    title: "Hooks and the proxy",
    analogy: "The front desk: every request now passes through the router.",
    summary: "Claude Code's requests pass through the router, which forwards each one to Anthropic and records its usage and cost.",
    bullets: [
      "Small hook scripts report each prompt, and always exit quietly so they never block Claude Code.",
      "Each request is matched to its session and turn using the hint headers Claude Code sends.",
      "The router can chain to another local proxy through an environment setting.",
    ],
    commit: "9a34854",
  },
  {
    id: "t1-foundation",
    date: "2026-10-19",
    tag: "Feature",
    tickets: ["T1.1", "T1.2", "T1.3", "T1.4", "T1.5"],
    title: "Foundation: config, database and prices",
    analogy: "The building: the database, the price list and the config files everything else sits on.",
    summary: "The project skeleton, checked config files, a local database and a dated price list that turns usage into dollar figures.",
    bullets: [
      "Dollar figures are API-equivalent estimates: what the same usage would cost on the API, not a bill.",
      "Prices sit in one dated file, so a price change never rewrites old costs.",
      "Everything is kept in one local database file, and changes to it are numbered and applied once.",
      "The project is named smartrouter; Laya is an optional extra to keep the default install small.",
    ],
    commit: "cc91332",
  },
  {
    id: "m0-spikes",
    date: "2026-10-19",
    tag: "Decision",
    tickets: ["M0"],
    title: "Spikes: which engine, and how sessions are tracked",
    analogy: "The test kitchen: small trials that decide which ingredients go into the recipe.",
    summary: "Quick trials of the session key, model switching, and two decision engines settled the design before real code was built.",
    bullets: [
      "ScaleDown scored 16 of 20 on the spike set and Laya 13 to 14, so ScaleDown is the default engine and Laya the second.",
      "Claude Code's hint headers identify the session and turn for each request, so the router does not have to guess.",
      "A switch-back test showed that returning to a recently used model re-reads most of the context cheaply.",
    ],
    commit: "403665c",
  },
];
