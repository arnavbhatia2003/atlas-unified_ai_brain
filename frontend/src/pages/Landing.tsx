import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { useAsync } from '@/lib/useAsync'
import type { Overview } from '@/lib/types'
import './landing.css'

// ── utilities ported from the reference landing page ───────────────────

function reduced(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

function pad(n: number, l = 2): string {
  let s = String(n)
  while (s.length < l) s = '0' + s
  return s
}
/** UTC clock in the nav. */
function useClock(): string {
  const [t, setT] = useState('00:00:00 UTC')
  useEffect(() => {
    const tick = () => {
      const d = new Date()
      setT(`${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`)
    }
    tick()
    const id = setInterval(tick, 250)
    return () => clearInterval(id)
  }, [])
  return t
}

/** Scroll progress bar (0–100). */
function useScrollProgress(): number {
  const [p, setP] = useState(0)
  useEffect(() => {
    let ticking = false
    const onScroll = () => {
      if (ticking) return
      ticking = true
      requestAnimationFrame(() => {
        const h = document.documentElement.scrollHeight - window.innerHeight
        setP(h > 0 ? (window.scrollY / h) * 100 : 0)
        ticking = false
      })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => window.removeEventListener('scroll', onScroll)
  }, [])
  return p
}

/** Reveal-on-scroll: adds `.in` to [data-reveal] children when they enter view. */
function useReveal(): React.RefObject<HTMLDivElement | null> {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const els = ref.current?.querySelectorAll('[data-reveal]')
    if (!els || !els.length) return
    if (reduced() || !('IntersectionObserver' in window)) {
      els.forEach((el) => el.classList.add('in'))
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add('in')
            io.unobserve(e.target)
          }
        }
      },
      { threshold: 0.18, rootMargin: '0px 0px -30px' },
    )
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [])
  return ref
}

// ── section components ────────────────────────────────────────────────

const NAV_LINKS = [
  ['CAPABILITIES', '#capabilities'],
  ['SYSTEMS', '#systems'],
  ['EVIDENCE', '#grid'],
  ['CONNECTORS', '#network'],
  ['TOPOLOGY', '#procurement'],
  ['DOCTRINE', '#manifesto'],
] as const

function Nav({ clock }: { clock: string }) {
  const [open, setOpen] = useState(false)
  return (
    <header className={`l-nav${open ? ' l-nav--open' : ''}`}>
      <Link className="brand" to="/" aria-label="Atlas Industrial Brain home">
        <span className="brand__mark" aria-hidden />
        <span>
          <span className="brand__text">
            ATLAS<em>&nbsp;/&nbsp;</em>BRAIN
          </span>
          <br />
          <span className="brand__sub">SOURCE EVIDENCE + HUMAN REVIEW</span>
        </span>
      </Link>
      <nav className="nav__links" aria-label="Primary">
        {NAV_LINKS.map(([label, href]) => (
          <a key={href} href={href} onClick={() => setOpen(false)}>
            {label}
          </a>
        ))}
      </nav>
      <div className="nav__meta">
        <span className="status">
          <i aria-hidden />
          <span>BRAIN.ONLINE</span>
        </span>
        <span className="clock" aria-hidden>
          {clock}
        </span>
        <Link className="btn btn--red btn--sm nav-cta" to="/ask">
          LAUNCH COPILOT
        </Link>
        <button
          className="burger"
          aria-expanded={open}
          aria-label="Toggle menu"
          onClick={() => setOpen((o) => !o)}
        >
          <span />
          <span />
          <span />
        </button>
      </div>
    </header>
  )
}

function Hero() {
  const { data } = useAsync<Overview>(s => api.overview(s))
  return (
    <section className="hero" id="top">
      <span className="cross cross--tl" aria-hidden>+</span>
      <span className="cross cross--tr" aria-hidden>+</span>
      <span className="cross cross--ml" aria-hidden>+</span>
      <span className="cross cross--mr" aria-hidden>+</span>
      <span className="cross cross--bl" aria-hidden>+</span>
      <span className="cross cross--br" aria-hidden>+</span>

      <div className="hero__top">
        <p className="tag">
          <b>[ ATLAS-CORE ]</b> /// INDUSTRIAL KNOWLEDGE INTELLIGENCE
        </p>
        <p className="tag">POSTGRESQL 16 + PGVECTOR (2048-DIM) + RECURSIVE SQL CTES</p>
      </div>

      <h1 className="hero__title">
        <span data-reveal>EVIDENCE</span>
        <span className="stroke" data-reveal style={{ ['--d' as string]: '0.09s' }}>
          AND ASSET CONTEXT<span className="sq" aria-hidden />
        </span>
      </h1>

      <div className="hero__foot">
        <p className="hero__sub" data-reveal style={{ ['--d' as string]: '0.14s' }}>
          ASK QUESTIONS OF YOUR PDFs. CONNECT JSON OR PUBLIC APIs TO BUILD A <b>REVIEWABLE ASSET GRAPH</b>. CITE SOURCES, EXPOSE CONFLICTS, AND KEEP UNCERTAIN MATCHES FOR HUMAN REVIEW.
        </p>
        <div className="hero__ctas" data-reveal style={{ ['--d' as string]: '0.2s' }}>
          <Link className="btn btn--red" to="/graph">
            OPEN 3D GRAPH <span className="arr">&gt;&gt;</span>
          </Link>
          <Link className="btn" to="/workflows/rca">
            RUN MULTI-HOP RCA
          </Link>
        </div>
        <dl className="telemetry" data-reveal style={{ ['--d' as string]: '0.26s' }}>
          <div>
            <dt>SOURCE RECORDS</dt>
            <dd>
              <span>{data?.source_records ?? '—'}</span>
              <small>STORED</small>
            </dd>
          </div>
          <div>
            <dt>UNIFIED ASSETS</dt>
            <dd>{data?.unified_assets ?? '—'}</dd>
          </div>
          <div>
            <dt>OPEN REVIEWS</dt>
            <dd>
              <span>{data?.review_open ?? '—'}</span>
              <small>PENDING</small>
            </dd>
          </div>
          <div>
            <dt>GRAPH EDGES</dt>
            <dd>
              <span>{data?.edges_total ?? '—'}</span>
              <small>STORED</small>
            </dd>
          </div>
        </dl>
      </div>
    </section>
  )
}

const MQ_ITEMS = [
  'RESOLUTION: REVIEWABLE MATCHES',
  'PDF SOURCE HANDLE VALIDATION',
  'SINGLE POSTGRESQL ENGINE',
  'ANSI SQL:1999 RECURSIVE CTES',
  'NVIDIA NEMOTRON-3 2048-DIM',
  'DOCLING TABLEFORMER SHA-256',
  'DETERMINISTIC SQL COMPLIANCE',
  'OCR REQUIRES THE FULL BACKEND',
]

function Marquee() {
  const group = (
    <div className="marquee__group" aria-hidden>
      {MQ_ITEMS.map((m) => (
        <span key={m}>
          <span className="mq-item">{m}</span>
          <span className="mq-sep">+++</span>
        </span>
      ))}
    </div>
  )
  return (
    <div className="marquee" aria-hidden>
      <div className="marquee__track">
        {group}
        {group}
      </div>
    </div>
  )
}

const CAPS = [
  { idx: '01', h: '[ INGESTION ]', p: 'Docling visual layout analysis + TableFormer PDF parsing. Two-Phase Chain-of-Custody with SHA-256 boundary persistence.', stat: 'DURABLE CUSTODY HASH' },
  { idx: '02', h: '[ DISCOVERY ]', p: 'Autonomous Tri-Signal Voting (Pattern ≥0.80, Cardinality ≥0.80, Semantics ≥0.80) with sequential ID arithmetic guards.', stat: 'INFERRED SCHEMA NEEDS REVIEW' },
  { idx: '03', h: '[ RESOLUTION ]', p: 'Physics-weighted Union-Find clustering (Serial w=0.95, MAC w=0.90) with Transitive Bridge-Conflict Guard (×0.40 penalty).', stat: 'MATCH QUALITY REQUIRES EVALUATION' },
  { idx: '04', h: '[ GRAPH CTE ]', p: 'Dual-Layer Knowledge Graph projected into PostgreSQL. Multi-hop traversal via ANSI SQL:1999 Recursive CTEs.', stat: 'BOUNDED SQL TRAVERSAL' },
  { idx: '05', h: '[ HYBRID RRF ]', p: 'Tri-modal search combining pgvector (2048-dim), PostgreSQL full-text search, and OKF dossiers with Nemotron Cross-Encoder reranking.', stat: 'RRF FUSION (K=60)' },
  { idx: '06', h: '[ COMPLIANCE ]', p: 'Deterministic SQL anti-join (NOT EXISTS) regulatory safety audits. The LLM only authors prose; SQL decides compliance.', stat: 'DETERMINISTIC RULE CHECKS' },
]

function Capabilities() {
  return (
    <section className="section" id="capabilities">
      <header className="sec-head">
        <span className="sec-head__tag" data-reveal>
          [ SEC.02 ]
        </span>
        <h2 data-reveal style={{ ['--d' as string]: '0.06s' }}>
          ENGINEERING
          <br />
          PILLARS
        </h2>
        <p data-reveal style={{ ['--d' as string]: '0.12s' }}>
          SIX CONNECTED SUBSYSTEMS. MODEL OUTPUTS AND HEURISTIC MATCHES REQUIRE VALIDATION.
        </p>
      </header>
      <div className="cap-grid gridlines">
        {CAPS.map((c, i) => (
          <article className="cap" data-reveal key={c.idx} style={{ ['--d' as string]: `${(i % 3) * 0.06}s` }}>
            <span className="cap__idx" aria-hidden>
              {c.idx}
            </span>
            <h3>{c.h}</h3>
            <p>{c.p}</p>
            <span className="cap__stat">{c.stat}</span>
          </article>
        ))}
      </div>
    </section>
  )
}

const SYSROWS = [
  {
    num: 'AT-01',
    unit: '[ UNIT / AT-01 ] — REASONING COPILOT',
    h: 'ASK COPILOT',
    desc: 'Bounded LangGraph state-machine router streaming token deltas over Server-Sent Events (SSE). Bypasses LLM on structured lookups; enforces verified citations on every answer.',
    specs: [
      ['ROUTING', 'DETERMINISTIC FSM'],
      ['REASONER', 'NEMOTRON-3 SUPER'],
      ['LATENCY', 'DOCUMENT / LOAD DEPENDENT'],
      ['CITATIONS', 'SOURCE HANDLES CHECKED'],
    ],
    to: '/ask',
  },
  {
    num: 'AT-02',
    unit: '[ UNIT / AT-02 ] — MULTI-HOP CAUSAL REASONING',
    h: 'DIAGNOSTIC RCA AGENT',
    desc: '4-stage causal investigation traversing upstream DEPENDS_ON physical links. Enforces a Failure Anomaly Gate that refuses to hallucinate diagnoses for healthy machines.',
    specs: [
      ['TRAVERSAL', 'UPSTREAM SQL CTE'],
      ['HOPS', 'MAX 6 HOPS'],
      ['ANOMALY GATE', 'STRICT GROUNDING'],
      ['OUTPUT', 'RANKED HYPOTHESES'],
    ],
    to: '/workflows/rca',
  },
  {
    num: 'AT-03',
    unit: '[ UNIT / AT-03 ] — 3D GRAPH VISUALIZER',
    h: 'KNOWLEDGE GRAPHSPHERE',
    desc: 'Custom D3-Force-3D HTML5 canvas visualizer with stereoscopic z-depth shading and Level-of-Detail chip clustering. Integrates prefers-reduced-motion to pause GPU loops in low-power mode.',
    specs: [
      ['ENGINE', 'D3-FORCE-3D CANVAS'],
      ['LAYERS', 'PHYSICAL + OPS'],
      ['FRAME RATE', '60 FPS'],
      ['POWER UX', 'LOW-POWER PAUSE'],
    ],
    to: '/graph',
  },
]

function Systems() {
  return (
    <section className="section" id="systems">
      <header className="sec-head">
        <span className="sec-head__tag" data-reveal>
          [ SEC.03 ]
        </span>
        <h2 data-reveal style={{ ['--d' as string]: '0.06s' }}>
          FLAGSHIP
          <br />
          MODULES
        </h2>
        <p data-reveal style={{ ['--d' as string]: '0.12s' }}>
          THREE SPECIALIZED AGENTIC WORKFLOWS ORCHESTRATED OVER THE UNIFIED RECURSIVE GRAPH.
        </p>
      </header>
      <div className="sysrows">
        {SYSROWS.map((s, i) => (
          <article className={`sysrow${i % 2 ? ' alt' : ''}`} data-reveal key={s.num}>
            <span className="sysrow__num" aria-hidden>
              {s.num}
            </span>
            <div className="sysrow__body">
              <p className="tag">
                <b>[ {s.unit} ]</b>
              </p>
              <h3>{s.h}</h3>
              <p className="sysrow__desc">{s.desc}</p>
              <dl className="specs">
                {s.specs.map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
            </div>
            <Link className="sysrow__link" to={s.to} aria-label={`Open ${s.h}`}>
              &gt;&gt;&gt;
            </Link>
          </article>
        ))}
      </div>
    </section>
  )
}

function Telemetry() {
  const { data, error } = useAsync<Overview>(s => api.overview(s))
  return <section className="section" id="grid">
    <header className="sec-head"><span className="sec-head__tag">[ SEC.04 ]</span><h2>STORED EVIDENCE</h2><p>DATABASE SNAPSHOT. OPEN WORKFLOWS TO SEE ACTUAL EXECUTION STATUS.</p></header>
    <div className="term-wrap"><div className="term__bar"><b>ATLAS://EVIDENCE</b><i>ACTUAL SOURCE RECORDS</i></div>
      <div className="term__body">{error ? <p>Backend unavailable — counts could not be loaded.</p> : data?.recent_evidence.length ? data.recent_evidence.map(r => <p key={r.citation}><b>{r.system} · {r.citation}</b> {r.text.slice(0,240)}</p>) : <p>No asset evidence loaded.</p>}</div>
      <aside className="term-side"><h3>// CURRENT INDEX</h3><ul><li><span>SOURCE RECORDS</span><span>{data?.source_records ?? '—'}</span></li><li><span>CANONICAL ASSETS</span><span>{data?.unified_assets ?? '—'}</span></li><li><span>OPEN REVIEWS</span><span>{data?.review_open ?? '—'}</span></li></ul></aside>
    </div>
  </section>
}

const REGIONS = [
  { idx: 'C-01', name: 'SAP PM / ERP', nodes: 'EQUIPMENT & WORK ORDERS', geo: 'REST / RFC API', lat: 9 },
  { idx: 'C-02', name: 'IBM MAXIMO', nodes: 'MAINTENANCE & ASSET TAGS', geo: 'OSLC JSON API', lat: 11 },
  { idx: 'C-03', name: 'SCADA / HISTORIAN', nodes: 'ALARM & TELEMETRY STREAMS', geo: 'OPC-UA / MQTT', lat: 14 },
  { idx: 'C-04', name: 'CISCO CMDB', nodes: 'MAC & TOPOLOGY LINKS', geo: 'RESTCONF', lat: 19 },
  { idx: 'C-05', name: 'DOCLING PDF PARSER', nodes: 'P&ID DRAWINGS & MANUALS', geo: 'TABLEFORMER FAST', lat: 33 },
  { idx: 'C-06', name: 'OSHA / PESO AUDIT', nodes: 'STATUTORY SAFETY RULES', geo: 'SQL ANTI-JOIN', lat: 41 },
]

function Connectors() {
  return (
    <section className="section" id="network">
      <div className="regions-intro">
        <div>
          <p className="tag" data-reveal>
            <b>[ SEC.05 ]</b> /// DATA CONNECTORS
          </p>
          <h3 data-reveal style={{ ['--d' as string]: '0.06s' }}>
            ONE BRAIN.
            <br />
            <em>ALL SILOS.</em>
            <br />
            SOURCE MAPPING.
          </h3>
        </div>
        <div data-reveal style={{ ['--d' as string]: '0.12s' }}>
          <p>
            INCREMENTAL AND ACCUMULATIVE INGESTION. SYNCING ONE SOURCE NEVER WIPES ANOTHER. DATA
            GROWS THE CENTRAL RECURSIVE GRAPH.
          </p>
          <div className="barcode" aria-hidden />
        </div>
      </div>
      <ol className="regions">
        {REGIONS.map((r, i) => (
          <li className="region" data-reveal key={r.idx} style={{ ['--d' as string]: `${i * 0.04}s` }}>
            <span className="region__idx">{r.idx}</span>
            <span className="region__name">{r.name}</span>
            <span className="region__nodes">{r.nodes}</span>
            <span className="region__geo">{r.geo}</span>
            <span className="region__lat">{r.lat} MS</span>
            <span className="region__arrow" aria-hidden>
              &gt;&gt;
            </span>
          </li>
        ))}
      </ol>
    </section>
  )
}

const TIERS = [
  {
    tag: 'TIER / A',
    h: '[ EDGE SLIM ]',
    price: 'TEXT ONLY',
    unit: 'SINGLE-CONTAINER DOCKER',
    rows: [
      ['DATABASE', 'POSTGRESQL + PGVECTOR'],
      ['PARSER', 'PYPDFIUM2 STREAM'],
      ['HARDWARE', '2GB RAM EDGE RACK'],
      ['OCR', 'FULL BACKEND REQUIRED'],
    ],
    to: '/connectors',
    cta: 'DEPLOY SLIM',
    hot: false,
  },
  {
    tag: 'TIER / B',
    h: '[ HYBRID CLOUD ]',
    price: 'DEPLOYMENT OPTION',
    unit: 'SUPABASE + RAILWAY + VERCEL',
    rows: [
      ['DATA TIER', 'SUPABASE POSTGRESQL 16'],
      ['APP TIER', 'FASTAPI DOCKERIZED (RAILWAY)'],
      ['UI TIER', 'VERCEL PRODUCTION EDGE CDN'],
      ['MODELS', 'NVIDIA NIM FREE TIER (35 RPM)'],
    ],
    to: '/ask',
    cta: 'OPEN COPILOT',
    hot: true,
  },
  {
    tag: 'TIER / C',
    h: '[ FUTURE SELF-HOSTED ]',
    price: 'SOVEREIGN',
    unit: 'REQUIRES NEW INFERENCE CONFIGURATION',
    rows: [
      ['INFERENCE', 'LOCAL VLLM / NIM RACK'],
      ['STATUS', 'NOT IMPLEMENTED / VERIFIED'],
      ['COMPLIANCE', 'NO CERTIFICATION CLAIM'],
      ['AUDIT', 'DETERMINISTIC SQL'],
    ],
    to: '/review',
    cta: 'REVIEW ENGINE',
    hot: false,
  },
]

function Topology() {
  return (
    <section className="section" id="procurement">
      <header className="sec-head">
        <span className="sec-head__tag" data-reveal>
          [ SEC.06 ]
        </span>
        <h2 data-reveal style={{ ['--d' as string]: '0.06s' }}>
          DEPLOYMENT
          <br />
          TOPOLOGY
        </h2>
        <p data-reveal style={{ ['--d' as string]: '0.12s' }}>
          LOCAL DEMONSTRATOR WITH HOSTED NVIDIA INFERENCE. PRODUCTION NEEDS AUTHENTICATION, DURABLE JOBS, AND DEPLOYMENT VALIDATION.
        </p>
      </header>
      <div className="proc gridlines">
        {TIERS.map((t, i) => (
          <article className={`proc-card${t.hot ? ' proc-card--hot' : ''}`} data-reveal key={t.tag} style={{ ['--d' as string]: `${i * 0.08}s` }}>
            <span className="proc-card__tag">{t.tag}</span>
            <h3>{t.h}</h3>
            <div className="proc-card__price">{t.price}</div>
            <div className="proc-card__unit">{t.unit}</div>
            <ul>
              {t.rows.map(([k, v]) => (
                <li key={k}>
                  <span>{k}</span>
                  <span>{v}</span>
                </li>
              ))}
            </ul>
            <Link className={`btn proc-card__cta${t.hot ? ' btn--red' : ''}`} to={t.to}>
              {t.cta}
            </Link>
          </article>
        ))}
      </div>
    </section>
  )
}

const BENCH = [
  ['TEST-01', 'REGRESSION', 'CONFLICT AND HUMAN SEPARATION CHECKS — SYNTHETIC FIXTURES'],
  ['TEST-02', 'REGRESSION', 'NEGATED FAULTS AND HEALTHY LOGS — DETERMINISTIC CHECKS'],
  ['TEST-03', 'DOCUMENT QA', 'PAGE CITATIONS AND MISSING-EVIDENCE FALLBACK — BOUNDED PDF RETRIEVAL'],
  ['TEST-04', 'LIMITATION', 'INDUSTRIAL ACCURACY AND CROSS-SECTOR GENERALIZATION ARE NOT ESTABLISHED'],
  ['TEST-05', 'COMPLIANCE', 'RULE AND PERIOD MATCHING — DECISION SUPPORT REQUIRES HUMAN REVIEW'],
]

function Dispatches() {
  return (
    <section className="section" id="transmissions">
      <header className="sec-head">
        <span className="sec-head__tag" data-reveal>
          [ SEC.07 ]
        </span>
        <h2 data-reveal style={{ ['--d' as string]: '0.06s' }}>
          BENCHMARK
          <br />
          DISPATCHES
        </h2>
        <p data-reveal style={{ ['--d' as string]: '0.12s' }}>
          VERIFIED EVALUATION SUITE AUDIT RESULTS (EVAL_SUITE.PY). HONEST OBJECTIVE SIGNALS.
        </p>
      </header>
      <ol className="news">
        {BENCH.map(([idx, date, head], i) => (
          <li className="news-row" data-reveal key={idx} style={{ ['--d' as string]: `${i * 0.05}s` }}>
            <span className="news-row__idx">{idx}</span>
            <time className="news-row__date" dateTime={date}>
              {date}
            </time>
            <span className="news-row__head">{head}</span>
            <span className="news-row__arrow" aria-hidden>
              &gt;&gt;&gt;
            </span>
          </li>
        ))}
      </ol>
    </section>
  )
}

function Manifesto() {
  return (
    <section className="manifesto" id="manifesto">
      <span className="cross cross--tl" aria-hidden>+</span>
      <span className="cross cross--tr" aria-hidden>+</span>
      <span className="cross cross--bl" aria-hidden>+</span>
      <span className="cross cross--br" aria-hidden>+</span>
      <blockquote data-reveal>
        &ldquo;Do not ask an LLM to guess what broke &mdash; <b>traverse the graph and cite the metal.</b>&rdquo;
      </blockquote>
      <cite data-reveal style={{ ['--d' as string]: '0.1s' }}>
        ATLAS ARCHITECTURAL DOCTRINE — SECTION 01 — GROUNDING IS NON-NEGOTIABLE
      </cite>
    </section>
  )
}

function Operate() {
  return (
    <>
      <div className="hazard" aria-hidden />
      <section className="careers" id="careers">
        <p className="tag" data-reveal>
          <b>[ SEC.08 ]</b> /// EXPLORE THE PLATFORM
        </p>
        <h2 data-reveal style={{ ['--d' as string]: '0.06s' }}>
          OPERATE THE
          <br />
          <em>BRAIN</em>
        </h2>
        <p data-reveal style={{ ['--d' as string]: '0.12s' }}>
          ENTERPRISE KNOWLEDGE INTELLIGENCE DEPLOYED IN PRODUCTION. QUERY ASSETS, TRACE UPSTREAM
          FAILURES, AND AUDIT REGULATORY RISK IN REAL TIME.
        </p>
        <div data-reveal style={{ ['--d' as string]: '0.18s' }}>
          <Link className="btn btn--red" to="/ask">
            LAUNCH COPILOT WORKSPACE <span className="arr">&gt;&gt;</span>
          </Link>
        </div>
        <p className="careers__note" data-reveal style={{ ['--d' as string]: '0.24s' }}>
          COURSE UCS503P (SOFTWARE ENGINEERING PROJECT) — THAPAR INSTITUTE OF ENGINEERING &amp; TECHNOLOGY
        </p>
      </section>
      <div className="hazard" aria-hidden />
    </>
  )
}

function Footer() {
  const [sent, setSent] = useState(false)
  return (
    <footer>
      <div className="foot-grid gridlines">
        <div className="foot-col foot-brand">
          <span className="brand" aria-hidden>
            <span className="brand__mark" />
            <span>
              <span className="brand__text">
                ATLAS<em>&nbsp;/&nbsp;</em>BRAIN
              </span>
              <br />
              <span className="brand__sub">SOURCE EVIDENCE + HUMAN REVIEW</span>
            </span>
          </span>
          <p>
            ATLAS INDUSTRIAL KNOWLEDGE INTELLIGENCE PLATFORM. GREEN-COMPUTING COMPLIANT ASSET
            REASONING SUBSTRATE.
          </p>
          <div className="barcode" aria-hidden />
        </div>
        <div className="foot-col">
          <h4>// WORKFLOWS</h4>
          <ul>
            <li>
              <Link to="/graph">3D GRAPHSPHERE</Link>
            </li>
            <li>
              <Link to="/ask">ASK COPILOT</Link>
            </li>
            <li>
              <Link to="/workflows/rca">MULTI-HOP RCA</Link>
            </li>
            <li>
              <Link to="/workflows/compliance">COMPLIANCE AUDIT</Link>
            </li>
          </ul>
        </div>
        <div className="foot-col">
          <h4>// MANAGEMENT</h4>
          <ul>
            <li>
              <Link to="/assets">CANONICAL ASSETS</Link>
            </li>
            <li>
              <Link to="/connectors">DATA CONNECTORS</Link>
            </li>
            <li>
              <Link to="/review">REVIEW QUEUE</Link>
            </li>
            <li>
              <Link to="/workflows/documents">DOCLING PDF INGEST</Link>
            </li>
          </ul>
        </div>
        <div className="foot-col">
          <h4>// STANDARDS</h4>
          <ul>
            <li>
              <a href="#manifesto">ISO 14224 (RELIABILITY)</a>
            </li>
            <li>
              <a href="#manifesto">ISO 55000 (ASSETS)</a>
            </li>
            <li>
              <a href="#manifesto">IEEE 830 (SRS)</a>
            </li>
            <li>
              <a href="#manifesto">GREEN SOFTWARE (IT)</a>
            </li>
          </ul>
        </div>
        <div className="foot-col">
          <h4>// AUDIT BUS</h4>
          <form
            className="news-form"
            onSubmit={(e) => {
              e.preventDefault()
              setSent(true)
              setTimeout(() => setSent(false), 2200)
            }}
          >
            <input type="text" placeholder="QUERY ASSET / SERIAL..." aria-label="Query Asset" required />
            <button type="submit" className={sent ? 'sent' : undefined}>
              {sent ? 'OK' : '→'}
            </button>
          </form>
          <p className="news-note">REAL-TIME DETERMINISTIC EVIDENCE DISPATCH.</p>
        </div>
      </div>
      <div className="foot-bottom">
        <span className="symbols" aria-hidden>
          ©<i>/</i>®<i>/</i>™
        </span>
        <span className="legal">© 2026 ATLAS INDUSTRIAL BRAIN — ALL RIGHTS RESERVED</span>
        <span className="legal">UCS503P SOFTWARE ENGINEERING CAPSTONE — THAPAR INSTITUTE</span>
      </div>
    </footer>
  )
}

export function Landing() {
  const clock = useClock()
  const progress = useScrollProgress()
  const mainRef = useReveal()

  // Hero parallax — main title drifts up as the page scrolls.
  const titleRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    const title = titleRef.current
    if (!title || reduced()) return
    const onScroll = () => {
      if (window.scrollY > window.innerHeight * 1.2) return
      title.style.transform = `translateY(${window.scrollY * -0.08}px)`
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <div className="atlas-landing">
      <div className="noise" aria-hidden />
      <div className="l-progress" style={{ width: `${progress}%` }} />
      <a className="skip" href="#l-main">
        SKIP TO CONTENT
      </a>

      <Nav clock={clock} />

      <main id="l-main" ref={mainRef}>
        <Hero />
        <Marquee />
        <Capabilities />
        <Systems />
        <Telemetry />
        <Connectors />
        <Topology />
        <Dispatches />
        <Manifesto />
        <Operate />
      </main>

      <Footer />
    </div>
  )
}
