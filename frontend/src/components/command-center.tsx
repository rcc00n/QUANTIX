"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowUpRight, ShieldCheck, Radar, Layers } from "lucide-react";
import { useLive, asRecord, asRecords, text, amount } from "@/lib/terminal";
import {
  Kpi,
  Exact,
  LiveStatus,
  Warning,
  Empty,
  Tag,
  SectionTitle,
  JsonDetails,
} from "@/components/terminal-ui";
import { HistoryChart, type SeriesPoint } from "@/components/terminal-charts";
import {
  CollectorReliability,
  PaperHealth,
  TelegramWorkerHealth,
  type ReliabilitySnapshot,
} from "@/components/reliability-health";
import { StorageHealth } from "@/components/storage-health";
import type { PaperSession, PaperPortfolio, PaperMetrics } from "@/lib/paper";
import { preferredPaperSession, utcLabel } from "@/lib/paper";

type Overview = {
  as_of?: string;
  connected_exchanges?: number;
  venue_groups?: number;
  monitored_markets?: number;
  compatible_intersections?: number;
  scanner?: Record<string, unknown>;
  paper?: Record<string, unknown>;
  warnings?: string[];
  recent_opportunities?: Record<string, unknown>[];
  recent_simulations?: Record<string, unknown>[];
  feeds?: Record<string, unknown>[];
};
export function CommandCenter() {
  const live = useLive<Overview>("/terminal/overview", 15000);
  const sessions = useLive<{ items: PaperSession[] }>("/paper/sessions", 60000);
  const [selected, setSelected] = useState("");
  useEffect(() => {
    if (sessions.data && !sessions.data.items.some((x) => x.id === selected))
      setSelected(preferredPaperSession(sessions.data.items)?.id ?? "");
  }, [sessions.data, selected]);
  const session = sessions.data?.items.find((x) => x.id === selected);
  const portfolio = useLive<PaperPortfolio>(
    selected ? `/paper/sessions/${selected}/portfolio` : null,
    15000,
  );
  const activity = useLive<PaperMetrics>(
    selected ? `/paper/sessions/${selected}/metrics` : null,
    15000,
  );
  const history = useLive<{ items: SeriesPoint[] }>(
    selected ? `/analytics/sessions/${selected}/series?limit=180` : null,
    60000,
  );
  const metrics = asRecord(live.data?.scanner?.metrics);
  const clock = asRecord(live.data?.scanner?.clock);
  const opportunities = live.data?.recent_opportunities || [];
  const trades = live.data?.recent_simulations || [];
  return (
    <div className="terminal-workspace command-center">
      <div className="terminal-workspace-top">
        <div className="terminal-session-picker">
          <label htmlFor="command-portfolio">Portfolio scope</label>
          <select
            id="command-portfolio"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {sessions.data?.items.map((x) => (
              <option key={x.id} value={x.id}>
                {x.name} · {x.classification === "RESEARCH" ? "Research only" : "Validated"}
              </option>
            ))}
          </select>
          {session && <Tag value={session.classification} />}
        </div>
        <LiveStatus {...live} />
      </div>
      {session?.classification === "RESEARCH" && (
        <Warning>
          Hypothetical research portfolio. Its capital and results are excluded from validated
          performance.
        </Warning>
      )}
      {(live.error || portfolio.error) && (
        <Warning>
          {live.error || portfolio.error} Source timestamps remain visible; retained values are not
          fresh execution evidence.
        </Warning>
      )}
      <div className="terminal-kpi-grid primary-kpis">
        <Kpi
          label="Virtual portfolio equity"
          value={portfolio.data?.equity_cad}
          unit="CAD"
          note="Liquidation-depth valuation"
          help="Cash and inventory marked using available observed depth and explicit CAD conversion evidence."
        />
        <Kpi
          label="Realized simulated P&L"
          value={portfolio.data?.realized_trading_pnl_cad}
          unit="CAD"
          color
          note="Disposal basis · virtual only"
        />
        <Kpi
          label="Unrealized inventory P&L"
          value={portfolio.data?.unrealized_pnl_cad}
          unit="CAD"
          color
          note="Holding gains and losses"
        />
        <Kpi
          label="Strategy excess"
          value={portfolio.data?.strategy_excess_cad}
          unit="CAD"
          color
          note="Versus no-trading benchmark"
          help="Portfolio equity minus the value of its original funded inventory held unchanged."
        />
      </div>
      <div className="terminal-market-strip">
        <Kpi
          label="Connected exchanges"
          value={live.data?.connected_exchanges}
          note={`${live.data?.venue_groups ?? "—"} independent venue groups`}
        />
        <Kpi
          label="Monitored markets"
          value={live.data?.monitored_markets}
          note="Selected books · not all listings"
        />
        <Kpi
          label="Compatible intersections"
          value={live.data?.compatible_intersections}
          note="Across independent venues"
        />
        <Kpi
          label="Verified / research"
          value={metrics.verified_market}
          note={`${text(metrics.research_only, "—")} research observations`}
        />
      </div>
      <div className="terminal-split equity-split">
        <section className="terminal-panel">
          <SectionTitle eyebrow="CAPITAL PERFORMANCE" title="Equity versus benchmark">
            <Link className="text-link" href={`/analytics?session=${selected}`}>
              Analytics <ArrowUpRight size={14} />
            </Link>
          </SectionTitle>
          <HistoryChart points={history.data?.items || []} />
          <p className="terminal-footnote">
            Recorded checkpoints · CAD · gaps preserve unavailable marks ·{" "}
            <span>
              {portfolio.data?.as_of ? utcLabel(portfolio.data.as_of) : "Awaiting valuation"}
            </span>
          </p>
        </section>
        <aside className="terminal-panel command-status">
          <SectionTitle eyebrow="EXECUTION CONTROL" title="Paper coordinator" />
          <div className="execution-count">
            <strong>{activity.data?.completed_simulations ?? "—"}</strong>
            <span>completed simulated trades</span>
          </div>
          <dl className="terminal-definition-list">
            <div>
              <dt>Session status</dt>
              <dd>
                <Tag value={session?.status} />
              </dd>
            </div>
            <div>
              <dt>Paper coordinator</dt>
              <dd>
                {live.data?.paper?.healthy === true
                  ? "Healthy"
                  : live.data?.paper?.healthy === false
                    ? "Unavailable"
                    : "Unknown"}
              </dd>
            </div>
            <div>
              <dt>Missed candidates</dt>
              <dd>{activity.data?.missed_simulations ?? "—"}</dd>
            </div>
            <div>
              <dt title="Unreserved marked equity includes assets; each route still requires purchase currency and sell inventory.">
                Unreserved marked equity
              </dt>
              <dd>
                <Exact value={portfolio.data?.available_capital_cad} unit="CAD" />
              </dd>
            </div>
            <div>
              <dt>Capital utilization</dt>
              <dd>
                <Exact value={portfolio.data?.capital_utilization_pct} unit="%" />
              </dd>
            </div>
            <div>
              <dt>Maximum drawdown</dt>
              <dd>
                <Exact value={activity.data?.maximum_drawdown_pct} unit="%" />
              </dd>
            </div>
          </dl>
          <Link className="terminal-action-link" href={`/paper-trading?session=${selected}`}>
            <Layers size={15} /> Inspect paper activity <ArrowUpRight size={14} />
          </Link>
        </aside>
      </div>
      <section className="terminal-panel">
        <SectionTitle eyebrow="LIVE SAFEGUARDS" title="System context">
          <Link href="/system-health" className="text-link">
            Inspect health <ArrowUpRight size={14} />
          </Link>
        </SectionTitle>
        <div className="terminal-safeguards">
          <div>
            <ShieldCheck size={18} />
            <span>
              UTC confidence
              <strong>
                {clock.trusted === true ? "Externally corroborated" : "Untrusted / unavailable"}
              </strong>
            </span>
          </div>
          <div>
            <Radar size={18} />
            <span>
              Financial evaluation
              <strong>{text(metrics.processing_latency_ms, "—")} ms mean cycle</strong>
            </span>
          </div>
          <div>
            <Layers size={18} />
            <span>
              Scanner backlog
              <strong>{text(metrics.pending_comparisons, "—")} pending comparisons</strong>
            </span>
          </div>
        </div>
        {(live.data?.warnings || []).slice(0, 5).map((warning, i) => (
          <Warning key={i}>{warning.replaceAll("_", " ")}</Warning>
        ))}
        {clock.host_healthy === false && (
          <Warning>
            Host synchronization is unhealthy. Application UTC is independently checked; no route is
            actionable solely because a spread is positive.
          </Warning>
        )}
      </section>
      <div className="terminal-split">
        <section className="terminal-panel">
          <SectionTitle eyebrow="MARKET INTELLIGENCE" title="Recent opportunities">
            <Link href="/opportunities" className="text-link">
              Open Radar <ArrowUpRight size={14} />
            </Link>
          </SectionTitle>
          {opportunities.length ? (
            <div className="terminal-recent-list">
              {opportunities.slice(0, 6).map((x) => (
                <Link
                  href={`/opportunities?route=${text(x.id, "")}`}
                  key={text(x.id)}
                  className="terminal-recent-row"
                >
                  <div>
                    <strong>{text(x.base_asset)}</strong>
                    <span>
                      {text(x.buy_exchange)} → {text(x.sell_exchange)}
                    </span>
                  </div>
                  <div>
                    <Exact value={x.net_profit_cad} unit="CAD" color />
                    <Tag value={x.status} />
                  </div>
                </Link>
              ))}
            </div>
          ) : (
            <Empty title="No current opportunity records">
              The scanner has not retained a matching current candidate. Positive research estimates
              are never assumed executable.
            </Empty>
          )}
        </section>
        <section className="terminal-panel">
          <SectionTitle eyebrow="VIRTUAL ACTIVITY" title="Recent simulations">
            <Link href="/paper-trading" className="text-link">
              Inspect history <ArrowUpRight size={14} />
            </Link>
          </SectionTitle>
          {trades.length ? (
            <div className="terminal-recent-list">
              {trades.slice(0, 6).map((x) => (
                <Link
                  href={`/paper-trading?session=${text(x.session_id, selected)}`}
                  key={text(x.id)}
                  className="terminal-recent-row"
                >
                  <div>
                    <strong>Simulated · {text(x.id).slice(0, 8)}</strong>
                    <span>{text(x.reason, "No outcome reason recorded").replaceAll("_", " ")}</span>
                  </div>
                  <div>
                    <Tag value={x.status} />
                    <Tag value={x.classification} />
                  </div>
                </Link>
              ))}
            </div>
          ) : (
            <Empty title="Zero recorded simulations">
              No execution history is invented. Configure virtual inventory in the Funding Planner
              to research compatible routes.
            </Empty>
          )}
        </section>
      </div>
    </div>
  );
}

export function SystemHealthWorkspace() {
  const live = useLive<Record<string, unknown>>("/terminal/health", 15000);
  const value = live.data || {};
  const scanner = asRecord(value.scanner);
  const metrics = asRecord(scanner.metrics);
  const clock = asRecord(scanner.clock);
  const paper = asRecord(value.paper);
  const collector = asRecord(value.collector);
  const telemetry = asRecord(value.telemetry);
  const feeds = asRecords(value.feeds);
  const events = asRecords(value.events);
  const quality = asRecord(value.quality_counts || collector.quality_counts);
  const pipeline = asRecord(metrics.pipeline_timings);
  const fresh = asRecord(collector.freshness_seconds);
  const routeLatency = asRecord(pipeline.route_evaluation_ms);
  const assetCoverage = asRecords(collector.asset_coverage);
  return (
    <div className="terminal-workspace">
      <div className="terminal-workspace-top">
        <span className="terminal-scope-note">Operational telemetry · authenticated · UTC</span>
        <LiveStatus {...live} />
      </div>
      {live.error && <Warning>{live.error}</Warning>}
      <div className="terminal-kpi-grid">
        <Kpi
          label="Connected exchanges"
          value={value.connected_exchanges}
          note={`${text(value.venue_groups, "—")} venue groups`}
        />
        <Kpi label="Monitored books" value={value.monitored_markets || metrics.monitored_markets} />
        <Kpi
          label="Scanner cycle mean / peak"
          value={metrics.processing_latency_ms}
          unit="ms"
          note={`${text(metrics.max_processing_latency_ms, "—")} ms peak`}
        />
        <Kpi label="Scanner backlog" value={metrics.pending_comparisons} />
      </div>
      <p className="terminal-scope-note">
        Status generated: {utcLabel(amount(value.as_of))}. Market receipt times are shown
        separately; browser wall time never establishes quote freshness.
      </p>
      <div className="terminal-kpi-grid">
        <Kpi
          label="Book age p95 / p99"
          value={fresh.p95}
          unit="s"
          note={`p99 ${text(fresh.p99, "—")} s · n=${text(fresh.sample_count, "0")}`}
        />
        <Kpi
          label="Route evaluation p95"
          value={routeLatency.p95}
          unit="ms"
          note={`n=${text(routeLatency.sample_count, "0")}; excludes persistence`}
        />
        <Kpi
          label="Source to evaluation p95"
          value={asRecord(pipeline.source_to_evaluation_ms).p95}
          unit="ms"
          note="Oldest supplied source time; unavailable without timestamp"
        />
        <Kpi
          label="Receive to evaluation p95"
          value={asRecord(pipeline.receive_to_evaluation_ms).p95}
          unit="ms"
        />
        <Kpi label="Collector due backlog" value={collector.scheduling_backlog} />
        <Kpi
          label="Oldest recheck"
          value={asRecord(collector.recheck_queue).oldest_seconds}
          unit="s"
        />
        <Kpi label="Oldest comparison" value={metrics.comparison_queue_oldest_seconds} unit="s" />
        <Kpi
          label="Telegram queue to API ack p95"
          value={asRecord(asRecord(value.notification_timings).queue_to_provider_ack_ms).p95}
          unit="ms"
          note="Provider acceptance; device receipt unverified"
        />
      </div>
      <JsonDetails
        title="Pipeline durations, rejected updates and measurement scope"
        value={{
          collector: collector.pipeline_timings,
          scanner: pipeline,
          rejected_updates: collector.rejected_updates,
          notifications: value.notification_timings,
        }}
      />
      <CollectorReliability
        snapshot={collector.reliability as ReliabilitySnapshot | undefined}
        stale={live.stale}
      />
      <TelegramWorkerHealth clockTrusted={clock.trusted === true && !live.stale} />
      <StorageHealth snapshot={value.storage} stale={live.stale} />
      <PaperHealth paper={paper} valuation={asRecord(value.paper_valuation)} stale={live.stale} />
      {assetCoverage.length > 0 && (
        <section className="terminal-panel">
          <SectionTitle title="Market coverage by asset" />
          <div className="table-wrap">
            <table className="terminal-table" aria-label="Market coverage by asset">
              <thead>
                <tr>
                  {[
                    "Asset",
                    "Selected books",
                    "Connected venues",
                    "Same-quote directions",
                    "REST / WS",
                    "Fresh / aging / stale",
                    "Synchronized WS",
                    "Positive CAD research",
                    "Largest executable buy",
                    "Evaluation p50 / p95",
                  ].map((header) => (
                    <th scope="col" key={header}>
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {assetCoverage.map((row) => {
                  const transport = asRecord(row.transport);
                  const freshness = asRecord(row.freshness);
                  const evaluation = asRecord(row.retained_route_evaluation_ms);
                  return (
                    <tr key={text(row.asset_key)}>
                      <th scope="row" title={text(row.asset_key)}>
                        {text(row.asset)}
                        {row.verified_identity === false && <small>Unverified identity</small>}
                      </th>
                      <td>{text(row.monitored_books, "—")}</td>
                      <td>{text(row.connected_venue_groups, "—")}</td>
                      <td>{text(row.same_quote_directed_routes, "—")}</td>
                      <td>
                        {text(transport.rest, "0")} / {text(transport.websocket, "0")}
                      </td>
                      <td>
                        {clock.trusted === true
                          ? `${text(freshness.FRESH, "0")} / ${text(freshness.AGING, "0")} / ${text(freshness.STALE, "0")}`
                          : "Clock untrusted"}
                        {!!freshness.UNAVAILABLE && (
                          <small> · {text(freshness.UNAVAILABLE)} unavailable</small>
                        )}
                        {clock.trusted === true && !!freshness.CLOCK_UNTRUSTED && (
                          <small> · {text(freshness.CLOCK_UNTRUSTED)} invalid timing</small>
                        )}
                      </td>
                      <td>{text(row.synchronized_ws_books, "—")}</td>
                      <td>{text(row.positive_after_cost_routes, "—")}</td>
                      <td>
                        <Exact value={row.maximum_executable_buy_cad} places={2} unit="CAD" />
                      </td>
                      <td>
                        {text(evaluation.p50, "—")} / {text(evaluation.p95, "—")} ms · n=
                        {text(evaluation.sample_count, "0")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="terminal-description">
            Same-quote directions use verified asset identities on distinct venues. Connected venues
            have recent healthy or degraded books. Quote age and WebSocket synchronization are
            measured independently. Size, profitability and timing summarize unexpired scanner
            evaluations. Two-sided funding depends on the selected portfolio and is measured in the{" "}
            <Link href="/portfolio">Funding Planner</Link>.
          </p>
        </section>
      )}
      <div className="terminal-split">
        <section className="terminal-panel">
          <SectionTitle eyebrow="FINANCIAL TIMING" title="Clock confidence" />
          <dl className="terminal-definition-list">
            <div>
              <dt>Application UTC</dt>
              <dd>
                <Tag value={clock.trusted === true ? "TRUSTED" : "UNTRUSTED"} />
              </dd>
            </div>
            <div>
              <dt>Host synchronized</dt>
              <dd>
                {clock.host_healthy === true
                  ? "Healthy"
                  : clock.host_healthy === false
                    ? "Unhealthy"
                    : "Unavailable"}
              </dd>
            </div>
            <div>
              <dt>Uncertainty</dt>
              <dd>{text(clock.uncertainty_ms, "—")} ms</dd>
            </div>
            <div>
              <dt>Reference age</dt>
              <dd>{text(clock.reference_age_seconds, "—")} s</dd>
            </div>
            <div>
              <dt>Observed wall steps</dt>
              <dd>{text(clock.wall_steps, "—")}</dd>
            </div>
            <div>
              <dt>Verified UTC</dt>
              <dd>{utcLabel(amount(clock.utc_now))}</dd>
            </div>
          </dl>
          {clock.host_healthy !== true && (
            <Warning>
              Host synchronization has not passed. Financial confidence and external UTC gates
              remain enforced.
            </Warning>
          )}
        </section>
        <section className="terminal-panel">
          <SectionTitle eyebrow="SERVICE STATE" title="Runtime and persistence" />
          <dl className="terminal-definition-list">
            <div>
              <dt>Database</dt>
              <dd>{text(value.database)}</dd>
            </div>
            <div>
              <dt>Schema revision</dt>
              <dd>{text(value.schema_revision)}</dd>
            </div>
            <div>
              <dt>API uptime</dt>
              <dd>{text(value.uptime_seconds, "—")} s</dd>
            </div>
            <div>
              <dt>Scanner</dt>
              <dd>{scanner.healthy === true ? "Healthy" : "Unavailable"}</dd>
            </div>
            <div>
              <dt>Paper coordinator</dt>
              <dd>{paper.healthy === true ? "Healthy" : "Unavailable"}</dd>
            </div>
            <div>
              <dt>Recheck queue</dt>
              <dd>
                {text(asRecord(collector.recheck_queue).pending, "—")} /{" "}
                {text(asRecord(collector.recheck_queue).limit, "—")}
              </dd>
            </div>
            <div>
              <dt>Last source update</dt>
              <dd>{utcLabel(amount(value.as_of))}</dd>
            </div>
          </dl>
        </section>
      </div>
      <section className="terminal-panel">
        <SectionTitle eyebrow="RESOURCE MEASUREMENTS" title="Process telemetry" />
        <div className="terminal-kpi-grid secondary-kpis">
          <Kpi
            label="REST latency mean"
            value={asRecord(collector.api_latency_ms).mean}
            unit="ms"
            note={`${text(asRecord(collector.api_latency_ms).sample_count, "—")} bounded samples`}
          />
          <Kpi
            label="REST latency peak"
            value={asRecord(collector.api_latency_ms).peak}
            unit="ms"
          />
          <Kpi
            label="Median book age"
            value={asRecord(collector.freshness_seconds).median}
            unit="s"
          />
          <Kpi
            label="Maximum book age"
            value={asRecord(collector.freshness_seconds).maximum}
            unit="s"
          />
        </div>
        <div className="terminal-telemetry">
          {Object.keys(telemetry).length ? (
            Object.entries(telemetry).map(([name, raw]) => {
              const row = asRecord(raw);
              const cgroup = asRecord(row.cgroup);
              return (
                <div key={name}>
                  <h3>{name.replaceAll("_", " ")}</h3>
                  <dl className="terminal-definition-list">
                    <div>
                      <dt>Current RSS bytes</dt>
                      <dd>
                        <Exact value={row.current_rss_bytes} places={0} />
                      </dd>
                    </div>
                    <div>
                      <dt>Peak RSS bytes</dt>
                      <dd>
                        <Exact value={row.peak_rss_bytes} places={0} />
                      </dd>
                    </div>
                    <div>
                      <dt>CPU utilization</dt>
                      <dd>
                        <Exact value={row.cpu_pct} unit="%" />
                      </dd>
                    </div>
                    <div>
                      <dt>CPU seconds</dt>
                      <dd>
                        <Exact value={row.cpu_seconds} />
                      </dd>
                    </div>
                    {cgroup.scope === "current_process_cgroup_v2" && (
                      <>
                        <div>
                          <dt>Cgroup memory charge / cap</dt>
                          <dd>
                            <Exact value={cgroup.memory_charged_bytes} places={0} unit="bytes" /> /{" "}
                            <Exact value={cgroup.memory_limit_bytes} places={0} unit="bytes" />
                          </dd>
                        </div>
                        <div>
                          <dt>Memory charge share / lifetime cap events</dt>
                          <dd>
                            <Exact value={cgroup.memory_pct_of_limit} unit="%" /> /{" "}
                            <Exact value={cgroup.memory_limit_events_total} places={0} />
                          </dd>
                        </div>
                        <div>
                          <dt>CPU cap usage / throttled scheduling periods</dt>
                          <dd>
                            <Exact value={cgroup.cpu_pct_of_limit} unit="%" /> /{" "}
                            <Exact value={cgroup.cpu_throttled_period_pct} unit="%" />
                          </dd>
                        </div>
                        <div>
                          <dt>Lifetime cgroup OOM kills</dt>
                          <dd>
                            <Exact value={cgroup.oom_kills_total} places={0} />
                          </dd>
                        </div>
                      </>
                    )}
                  </dl>
                </div>
              );
            })
          ) : (
            <Empty title="Process telemetry unavailable">
              No resource value is estimated. Container and VPS measurements remain distinct.
            </Empty>
          )}
        </div>
      </section>
      <section className="terminal-panel">
        <SectionTitle eyebrow="PUBLIC DATA NETWORK" title="Exchange health" />
        <div className="table-wrap">
          <table className="terminal-table" aria-label="Exchange operational health">
            <thead>
              <tr>
                {[
                  "Exchange",
                  "Status",
                  "Mode",
                  "Monitored",
                  "Freshness",
                  "API latency",
                  "Last success",
                ].map((x) => (
                  <th key={x}>{x}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {feeds.map((feed) => (
                <tr key={text(feed.slug)}>
                  <td>{text(feed.name)}</td>
                  <td>
                    <Tag value={feed.state} />
                  </td>
                  <td>{text(feed.connection_mode)}</td>
                  <td>{text(feed.monitored_count)}</td>
                  <td>{text(feed.freshness_seconds, "—")} s</td>
                  <td>{text(feed.latency_ms, "—")} ms</td>
                  <td>{utcLabel(amount(feed.last_success_at))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!feeds.length && (
          <Empty title="Exchange telemetry unavailable">
            Collector health cannot be inferred from earlier successful requests.
          </Empty>
        )}
        <div className="terminal-quality-strip">
          {Object.entries(quality).map(([key, count]) => (
            <span key={key}>
              <Tag value={key} />
              <strong>{text(count)}</strong>
            </span>
          ))}
        </div>
      </section>
      <section className="terminal-panel">
        <SectionTitle eyebrow="AUDIT HISTORY" title="Recent operational events" />
        {events.length ? (
          <div className="terminal-recent-list">
            {events.slice(0, 20).map((e, i) => (
              <div className="terminal-recent-row" key={text(e.id, String(i))}>
                <div>
                  <Tag value={e.severity} />
                  <strong>{text(e.message)}</strong>
                  <span>{text(e.event_type)}</span>
                </div>
                <time>{utcLabel(amount(e.created_at))}</time>
              </div>
            ))}
          </div>
        ) : (
          <Empty title="No recent warning records">
            An empty list is not proof of uninterrupted operation.
          </Empty>
        )}
      </section>
    </div>
  );
}
