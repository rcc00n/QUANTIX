"use client";
import { MarketAge } from "@/components/market-age";
import { useLive } from "@/lib/terminal";
import { useEffect, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownUp,
  Check,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldQuestion,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  age,
  decimal,
  utc,
  type Book,
  type DataState,
  type DiscoveredMarket,
  type ExchangeFeed,
  type MarketPage,
  type MarketTier,
  type NativeStreamStatus,
} from "@/lib/market-data";

const marketTierLabels: Record<MarketTier, string> = {
  FAST_LANE: "Fast lane",
  STANDARD: "Standard",
  RESEARCH: "Research",
};

function State({ state }: { state: DataState }) {
  return (
    <span className={`data-state state-${state.toLowerCase()}`}>
      <i />
      {state}
    </span>
  );
}

function NativeStreamMetrics({
  stream,
  trusted,
}: {
  stream: NativeStreamStatus;
  trusted: boolean;
}) {
  const receiptAge = stream.book_age_seconds;
  return (
    <>
      <div>
        <dt>Valid / total cached WebSocket books</dt>
        <dd>
          {stream.valid_cached_stream_books} / {stream.cached_stream_books}
        </dd>
      </div>
      <div>
        <dt>WebSocket book receipt age · median / oldest</dt>
        <dd>
          {trusted
            ? `${age(receiptAge.p50)} / ${age(receiptAge.maximum)} (n=${receiptAge.sample_count})`
            : "Clock untrusted"}
        </dd>
      </div>
      <div>
        <dt>REST requests on stream markets / resynchronizations</dt>
        <dd>
          {stream.counters.rest_on_stream_market ?? 0} / {stream.counters.resynchronizations ?? 0}
        </dd>
      </div>
      {stream.last_error && (
        <div>
          <dt>Last native stream error</dt>
          <dd>{stream.last_error.replaceAll("_", " ")}</dd>
        </div>
      )}
    </>
  );
}

export function ExchangeExplorer({ initialFeeds }: { initialFeeds: ExchangeFeed[] }) {
  const [feeds, setFeeds] = useState(initialFeeds);
  const [feedError, setFeedError] = useState("");
  const [selected, setSelected] = useState(
    initialFeeds.find((feed) => feed.slug === "kraken")?.slug ||
      initialFeeds.find((feed) => feed.enabled)?.slug ||
      "",
  );
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [monitoredOnly, setMonitoredOnly] = useState(true);
  const [markets, setMarkets] = useState<DiscoveredMarket[]>([]);
  const [marketTotal, setMarketTotal] = useState(0);
  const [marketId, setMarketId] = useState("");
  const [marketsLoading, setMarketsLoading] = useState(false);
  const [marketError, setMarketError] = useState("");
  const [book, setBook] = useState<Book | null>(null);
  const [bookError, setBookError] = useState("");
  const [bookLoading, setBookLoading] = useState(false);
  const [depth, setDepth] = useState(10);
  const [rechecking, setRechecking] = useState(false);
  const [recheckMessage, setRecheckMessage] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => setQuery(search), 350);
    return () => clearTimeout(timer);
  }, [search]);
  const feedLive = useLive<ExchangeFeed[]>("/exchanges", 15000);
  useEffect(() => {
    if (feedLive.data) {
      setFeeds(feedLive.data);
      setSelected((value) =>
        feedLive.data?.some((row) => row.slug === value)
          ? value
          : feedLive.data?.find((row) => row.enabled)?.slug || "",
      );
    }
    setFeedError(feedLive.error);
  }, [feedLive.data, feedLive.error]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setMarketsLoading(true);
    setMarketError("");
    setRecheckMessage("");
    async function update() {
      try {
        const response = await fetch(
          `/api/exchanges/${selected}/markets?monitored_only=${monitoredOnly}&limit=200&search=${encodeURIComponent(query)}`,
          { signal: controller.signal, cache: "no-store" },
        );
        if (!response.ok)
          throw new Error("Market catalog unavailable. Check the collector status.");
        const value: MarketPage = await response.json();
        setMarkets(value.markets);
        setMarketTotal(value.total);
        setMarketId((previous) =>
          value.markets.some((row) => row.id === previous) ? previous : value.markets[0]?.id || "",
        );
      } catch (error) {
        if (!controller.signal.aborted) {
          setMarkets([]);
          setMarketId("");
          setMarketError(error instanceof Error ? error.message : "Market catalog unavailable");
        }
      } finally {
        if (!controller.signal.aborted) setMarketsLoading(false);
      }
    }
    void update();
    return () => controller.abort();
  }, [selected, query, monitoredOnly, refresh]);
  const bookLive = useLive<Book>(marketId ? `/markets/${marketId}/book` : null, 5000);
  useEffect(() => {
    setBook(bookLive.data);
    setBookError(bookLive.error);
    setBookLoading(bookLive.loading);
  }, [bookLive.data, bookLive.error, bookLive.loading]);

  async function recheck() {
    setRechecking(true);
    setRecheckMessage("");
    try {
      const response = await fetch(`/api/markets/${marketId}/confirm`, { method: "POST" });
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail || "Fresh recheck unavailable");
      setRecheckMessage(
        "Fresh REST recheck queued. Confirmation requires independent same-quote references.",
      );
    } catch (error) {
      setRecheckMessage(error instanceof Error ? error.message : "Recheck unavailable");
    } finally {
      setRechecking(false);
    }
  }
  const activeFeed = feeds.find((feed) => feed.slug === selected);
  const activeMarket = markets.find((market) => market.id === marketId);
  const liveConnector = !feedError && activeFeed?.source === "live_collector";
  const nativeStream = liveConnector ? activeFeed?.native_stream : undefined;
  const streamSubscriptions = nativeStream?.subscribed.length ?? 0;
  const marketTier = activeMarket ? nativeStream?.market_tiers[activeMarket.symbol] : undefined;
  const webSocketBook = Boolean(book && book.source === `${book.exchange}_ws`);
  const bookExchange = book ? feeds.find((feed) => feed.slug === book.exchange)?.name : undefined;
  const enabled = feeds.filter((feed) => feed.enabled);
  const verified = enabled.filter((feed) => feed.public_data_verified);
  const working = enabled.filter(
    (feed) =>
      feed.collector_healthy &&
      feed.last_success_at &&
      feed.state !== "STALE" &&
      feed.state !== "UNAVAILABLE",
  );
  const visible = feeds.filter((feed) =>
    filter === "cad"
      ? feed.cad_market_count > 0
      : filter === "verified"
        ? feed.public_data_verified
        : feed.enabled,
  );
  const bookState: DataState = bookError ? "UNAVAILABLE" : book?.state || "UNAVAILABLE";
  const eligibility = activeFeed?.capabilities.canadian_eligibility;
  return (
    <div className="exchange-workspace">
      <div className="exchange-toolbar">
        <p>
          <Activity size={16} /> Status refresh · 15s; book refresh · 5s
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setRefresh((value) => value + 1);
            feedLive.refresh();
            bookLive.refresh();
          }}
        >
          <RefreshCw size={14} />
          Refresh feeds
        </Button>
      </div>
      <p className="feed-disclosure">
        Status generated:{" "}
        {feeds[0]?.status_generated_at ? utc(feeds[0].status_generated_at) : "Unavailable"}.
        Collector age measures the newest received book on a venue; individual books can be older.
        REST duration includes adapter throttling and parsing.
      </p>
      {feedError && (
        <div className="notice" role="alert">
          {feedError}
        </div>
      )}
      <div className="metrics-strip feed-metrics">
        {[
          [
            "Connected feeds",
            feedError ? "—" : working.length,
            `${verified.length} have supplied real books`,
          ],
          [
            "Discovered spot listings",
            enabled.reduce((sum, feed) => sum + feed.market_count, 0).toLocaleString(),
            "Includes separate adapter catalogs",
          ],
          [
            "Monitored pairs",
            enabled.reduce((sum, feed) => sum + feed.monitored_count, 0),
            "Bounded per-exchange budget",
          ],
          [
            "Exchanges with CAD",
            enabled.filter((feed) => feed.cad_market_count > 0).length,
            "CAD remains distinct from USD & stablecoins",
          ],
        ].map(([label, value, note]) => (
          <div className="metric" key={label}>
            <span>{label}</span>
            <div>{value}</div>
            <p>{note}</p>
          </div>
        ))}
      </div>
      <div className="section-heading">
        <div>
          <span className="small-label">EXCHANGE REGISTRY</span>
          <h2>Public data connections</h2>
        </div>
        <div className="feed-filters" role="group" aria-label="Exchange filters">
          {[
            ["all", "Configured"],
            ["cad", "CAD markets"],
            ["verified", "Verified feeds"],
          ].map(([value, label]) => (
            <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="table-wrap feeds-table">
        <table>
          <thead>
            <tr>
              {[
                "Exchange",
                "API status",
                "Spot markets",
                "Monitored",
                "CAD",
                "Mode",
                "Collector age / REST duration",
                "Canadian eligibility",
              ].map((label) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((feed) => (
              <tr key={feed.id} className={feed.slug === selected ? "selected-feed" : ""}>
                <td>
                  <button
                    className="exchange-select"
                    onClick={() => {
                      setSelected(feed.slug);
                      setQuery("");
                      setSearch("");
                    }}
                    aria-label={`Inspect ${feed.name}`}
                  >
                    <span>{feed.name}</span>
                    <small>
                      {feed.slug}
                      {feed.slug === "coinbaseexchange" ? " · same Coinbase venue" : ""}
                    </small>
                  </button>
                </td>
                <td>
                  <State state={feedError ? "UNAVAILABLE" : feed.state} />
                  {feed.error_code && (
                    <small className="feed-error-code">
                      {feed.error_code.replaceAll("_", " ")}
                    </small>
                  )}
                </td>
                <td>{feed.market_count.toLocaleString()}</td>
                <td>{feed.monitored_count}</td>
                <td>{feed.cad_market_count}</td>
                <td className="mode-cell">
                  {feed.connection_mode}
                  {!feedError &&
                    feed.source === "live_collector" &&
                    Boolean(feed.native_stream?.subscribed.length) && (
                      <small>
                        WebSocket sync {feed.native_stream?.synchronized} /{" "}
                        {feed.native_stream?.subscribed.length}
                      </small>
                    )}
                </td>
                <td className="timing-cell">
                  <MarketAge
                    seconds={feed.freshness_seconds}
                    generatedAt={feed.status_generated_at}
                    trusted={feed.clock_trusted !== false}
                  />
                  <small>
                    {feed.source === "live_collector"
                      ? "Live connector status"
                      : "Retained status snapshot"}
                  </small>
                  <small>
                    {feed.latency_ms === null
                      ? "No REST latency yet"
                      : `${feed.latency_ms} ms last REST call`}
                  </small>
                  <small>
                    REST p50 / p95: {feed.timings?.request_ms?.p50?.toFixed(0) ?? "—"} /{" "}
                    {feed.timings?.request_ms?.p95?.toFixed(0) ?? "—"} ms (n=
                    {feed.timings?.request_ms?.sample_count ?? 0})
                  </small>
                  <small title={utc(feed.last_collector_received_at ?? feed.last_success_at)}>
                    {(feed.last_collector_received_at ?? feed.last_success_at)
                      ? `Collector received ${new Date((feed.last_collector_received_at ?? feed.last_success_at)!).toISOString().slice(11, 19)} UTC`
                      : "No successful update"}
                  </small>
                </td>
                <td>
                  <span className="eligibility-tag">
                    {feed.capabilities.canadian_eligibility?.classification === "REGISTERED_ENTITY"
                      ? "Registered entity"
                      : "Unverified"}
                  </span>
                  <small className="eligibility-note">
                    {feed.capabilities.canadian_eligibility?.classification === "REGISTERED_ENTITY"
                      ? "Account / product review required"
                      : "Reference data only"}
                  </small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!visible.length && (
          <div className="table-empty">
            No exchanges match this filter. The collector may still be starting.
          </div>
        )}
      </div>
      <p className="feed-disclosure">
        <ShieldQuestion size={15} /> Public API access does not establish Canadian trading
        eligibility. No exchange is marked actionable for manual trading.
      </p>
      <section className="market-explorer" aria-labelledby="explorer-title">
        <div className="section-heading">
          <div>
            <span className="small-label">MARKET DATA EXPLORER</span>
            <h2 id="explorer-title">Inspect an order book</h2>
          </div>
          {bookLoading && (
            <span className="loading-label">
              <LoaderCircle size={14} className="spin" />
              Refreshing
            </span>
          )}
        </div>
        <div className="explorer-controls">
          <label>
            Exchange
            <select
              aria-label="Exchange"
              value={selected}
              onChange={(event) => {
                setSelected(event.target.value);
                setSearch("");
                setQuery("");
              }}
            >
              <option value="" disabled>
                Select exchange
              </option>
              {enabled.map((feed) => (
                <option value={feed.slug} key={feed.id}>
                  {feed.name}
                </option>
              ))}
            </select>
          </label>
          <label className="market-search">
            Find a market
            <span>
              <Search size={15} />
              <input
                aria-label="Find a market"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="BTC, ETH, SOL…"
              />
            </span>
          </label>
          <label>
            Market
            <select
              aria-label="Market"
              value={marketId}
              disabled={marketsLoading || !markets.length}
              onChange={(event) => setMarketId(event.target.value)}
            >
              <option value="" disabled>
                {marketsLoading ? "Loading markets…" : "Select market"}
              </option>
              {markets.map((market) => (
                <option key={market.id} value={market.id}>
                  {market.symbol}
                  {market.monitored ? " · monitored" : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={monitoredOnly}
              onChange={(event) => setMonitoredOnly(event.target.checked)}
            />
            Monitored only
          </label>
        </div>
        <p className="catalog-meta">
          {marketTotal.toLocaleString()} matching markets
          {marketTotal > 200 ? " · showing first 200; narrow your search" : ""}. Continuous books
          are collected only for monitored pairs.
        </p>
        {marketError && (
          <div className="notice" role="alert">
            {marketError}
          </div>
        )}
        {activeFeed && (
          <details className="evidence-details">
            <summary>Connector timing and freshness</summary>
            <dl className="book-metadata">
              <div>
                <dt>Latest supplied source time</dt>
                <dd>{utc(activeFeed.last_source_update_at ?? null)}</dd>
              </div>
              <div>
                <dt>Latest collector receipt</dt>
                <dd>{utc(activeFeed.last_collector_received_at ?? activeFeed.last_success_at)}</dd>
              </div>
              <div>
                <dt>Successful status check</dt>
                <dd>{utc(activeFeed.last_status_check_at ?? null)}</dd>
              </div>
              <div>
                <dt>Fresh / aging / stale books</dt>
                <dd>
                  {activeFeed.freshness_counts?.FRESH ?? "—"} /{" "}
                  {activeFeed.freshness_counts?.AGING ?? "—"} /{" "}
                  {activeFeed.freshness_counts?.STALE ?? "—"}
                </dd>
              </div>
              <div>
                <dt>Errors / rate limit / due queue</dt>
                <dd>
                  {activeFeed.failed_requests} / {activeFeed.rate_limit_state ?? "Unavailable"} /{" "}
                  {activeFeed.queue_backlog ?? "—"}
                </dd>
              </div>
              <div>
                <dt>WebSocket transport</dt>
                <dd>
                  {nativeStream && streamSubscriptions > 0
                    ? `${nativeStream.synchronized} of ${streamSubscriptions} subscriptions synchronized`
                    : liveConnector && activeFeed.ws_synchronized
                      ? "Synchronized"
                      : "No verified synchronized stream"}
                  {activeFeed.ws_last_message_at &&
                    ` · last message ${utc(activeFeed.ws_last_message_at)}`}
                </dd>
              </div>
              {nativeStream && streamSubscriptions > 0 && (
                <NativeStreamMetrics
                  stream={nativeStream}
                  trusted={activeFeed.clock_trusted === true}
                />
              )}
            </dl>
            <p>
              Quote age and transport heartbeat are independent. An old unchanged quote may coexist
              with a healthy synchronized connection.
            </p>
            <p>
              Monitored markets: {activeFeed.active_monitored_markets?.join(", ") || "Unavailable"}
            </p>
            <p>
              Confidence reasons:{" "}
              {Object.keys(activeFeed.quality_reasons || {}).join(", ") ||
                activeFeed.error_code ||
                "None reported"}
            </p>
            <pre>
              {JSON.stringify(
                activeFeed.timings ?? { status: "Timing samples unavailable" },
                null,
                2,
              )}
            </pre>
          </details>
        )}
        <div className="book-heading">
          <div>
            <h3>{activeMarket?.symbol || "No market selected"}</h3>
            <p>
              {activeFeed?.name || "Choose an exchange"} · Native quote currency · Public data
              {marketTier && ` · Collector tier ${marketTierLabels[marketTier]}`}
            </p>
          </div>
          <div className="book-actions">
            <State state={bookState} />
            <label className="sr-only" htmlFor="book-depth">
              Displayed depth
            </label>
            <select
              id="book-depth"
              value={depth}
              onChange={(event) => setDepth(Number(event.target.value))}
            >
              <option value={10}>10 levels</option>
              <option value={25}>25 levels</option>
            </select>
            <Button
              size="sm"
              variant="outline"
              disabled={!marketId || rechecking || !activeMarket?.monitored}
              onClick={recheck}
            >
              {rechecking ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}
              Fresh recheck
            </Button>
          </div>
        </div>
        {bookError && (
          <div className="notice" role="status">
            <AlertTriangle size={15} />
            {bookError}
            {book ? " Showing the last received record below." : ""}
          </div>
        )}
        {recheckMessage && (
          <div className="inline-note" role="status">
            {recheckMessage}
          </div>
        )}
        {book ? (
          <>
            <dl className="book-metadata">
              <div>
                <dt>Feed</dt>
                <dd>
                  {book.source === "rest"
                    ? "REST"
                    : webSocketBook
                      ? `${bookExchange ?? "Native"} WebSocket`
                      : "Transport unavailable"}{" "}
                  · {book.storage_origin === "live_memory" ? "live cache" : "persisted fallback"}
                </dd>
              </div>
              <div>
                <dt>Collector age / last checked class</dt>
                <dd>
                  <MarketAge
                    seconds={book.age_ms === null ? null : book.age_ms / 1000}
                    generatedAt={book.status_generated_at}
                    trusted={book.freshness?.classification !== "CLOCK_UNTRUSTED"}
                  />{" "}
                  · {book.freshness?.classification ?? "Unavailable"}
                </dd>
              </div>
              <div>
                <dt>Received</dt>
                <dd>{utc(book.received_at)}</dd>
              </div>
              <div>
                <dt>Exchange timestamp</dt>
                <dd>
                  {book.exchange_timestamp
                    ? utc(book.exchange_timestamp)
                    : "Not supplied · ingestion time only"}
                </dd>
              </div>
              <div>
                <dt>Fetch duration</dt>
                <dd>
                  {book.latency_ms !== null
                    ? `${book.latency_ms} ms`
                    : webSocketBook
                      ? "Stream · no REST timing"
                      : "Unavailable"}
                </dd>
              </div>
            </dl>
            <div className="quality-reasons">
              {book.reasons.length ? (
                book.reasons.map((reason) => (
                  <span key={reason}>
                    <AlertTriangle size={12} />
                    {reason.replaceAll("_", " ")}
                  </span>
                ))
              ) : (
                <span className="quality-clear">
                  <Check size={13} />
                  Structural and freshness checks passed
                </span>
              )}
            </div>
            <div className="book-sides">
              {(["bids", "asks"] as const).map((side) => (
                <div className={`book-side ${side}`} key={side}>
                  <div className="book-side-title">
                    <ArrowDownUp size={14} />
                    <strong>{side === "bids" ? "Bids" : "Asks"}</strong>
                    <span>{book.normalized[side].length} available levels</span>
                  </div>
                  <table aria-label={`${side} order book`}>
                    <thead>
                      <tr>
                        <th>Price · {activeMarket?.quote_symbol || book.quote_key}</th>
                        <th>Size · {activeMarket?.base_symbol || book.base_key}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {book.normalized[side].slice(0, depth).map((level, index) => (
                        <tr key={`${index}-${level.price}`}>
                          <td>{decimal(level.price)}</td>
                          <td>{decimal(level.quantity)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </div>
            <details className="evidence-details">
              <summary>Quality, confirmation & source evidence</summary>
              <p>
                Confirmation: <strong>{book.verification.status || "NOT_NEEDED"}</strong>.
                Cross-checks compare the same canonical asset and quote only; Coinbase adapters
                count as one venue.
              </p>
              <pre>
                {JSON.stringify(
                  {
                    reasons: book.reasons,
                    verification: book.verification,
                    sequence: book.sequence,
                    sha256: book.content_sha256,
                    raw: book.raw_evidence,
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
          </>
        ) : (
          <div className="explorer-empty">
            <Activity size={28} />
            <h3>{bookLoading ? "Loading the latest book…" : "No collected book available"}</h3>
            <p>
              {activeMarket?.monitored
                ? "The collector has not supplied a book for this market yet."
                : "Choose a monitored market to inspect continuously collected public data."}
            </p>
          </div>
        )}
        <div className="eligibility-detail">
          <ShieldQuestion size={18} />
          <div>
            <strong>{eligibility?.entity || "Canadian eligibility unverified"}</strong>
            <p>{eligibility?.scope || "No Canadian trading eligibility has been verified."}</p>
            {eligibility?.source && (
              <a href={eligibility.source} target="_blank" rel="noreferrer">
                Official registration source · reviewed {eligibility.reviewed_at}
              </a>
            )}
          </div>
        </div>
        <div className="inline-note">
          Market observations alone are not profit opportunities. Opportunity Radar evaluates depth,
          costs and confidence separately. USD, CAD, USDC and USDT remain separate quotes.
        </div>
      </section>
    </div>
  );
}
