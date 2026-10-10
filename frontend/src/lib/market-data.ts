export type DataState = "HEALTHY" | "DEGRADED" | "STALE" | "SUSPICIOUS" | "UNAVAILABLE";
export type MarketTier = "FAST_LANE" | "STANDARD" | "RESEARCH";
export type NativeStreamStatus = {
  subscribed: string[];
  synchronized: number;
  cached_stream_books: number;
  valid_cached_stream_books: number;
  book_age_seconds: {
    sample_count: number;
    p50: number | null;
    p95: number | null;
    p99: number | null;
    maximum: number | null;
  };
  counters: Record<string, number>;
  market_tiers: Record<string, MarketTier>;
  last_error?: string | null;
};
export type ExchangeFeed = {
  id: string;
  slug: string;
  name: string;
  enabled: boolean;
  state: DataState;
  collector_healthy: boolean;
  public_data_verified: boolean;
  market_count: number;
  monitored_count: number;
  cad_market_count: number;
  connection_mode: string;
  latency_ms: number | null;
  last_success_at: string | null;
  freshness_seconds: number | null;
  error_code: string | null;
  failed_requests: number;
  successful_requests: number;
  reconnects: number;
  status_generated_at?: string;
  last_status_check_at?: string | null;
  last_collector_received_at?: string | null;
  last_source_update_at?: string | null;
  clock_trusted?: boolean;
  source?: string;
  freshness_counts?: Record<string, number>;
  book_age_seconds?: { p50: number | null; p95: number | null; maximum: number | null };
  timings?: Record<string, { p50: number | null; p95: number | null; sample_count: number }>;
  quality_reasons?: Record<string, number>;
  rate_limit_state?: string;
  queue_backlog?: number;
  active_monitored_markets?: string[];
  ws_last_message_at?: string | null;
  ws_synchronized?: boolean;
  native_stream?: NativeStreamStatus;
  capabilities: {
    websocket_advertised?: boolean;
    websocket_implemented?: boolean;
    canadian_eligibility?: {
      classification: string;
      entity: string | null;
      source: string | null;
      scope: string;
      manual_trading_eligible: false;
      reviewed_at: string | null;
      venue_group: string;
    };
  };
};
export type DiscoveredMarket = {
  id: string;
  symbol: string;
  base_symbol: string;
  quote_symbol: string;
  active_status: string;
  monitored: boolean;
  base_identity_verified: boolean;
  quote_identity_verified: boolean;
  quote_volume: string | null;
  quote_volume_unit: string;
};
export type MarketPage = { total: number; markets: DiscoveredMarket[] };
export type Book = {
  market_id: string;
  exchange: string;
  symbol: string;
  base_key: string;
  quote_key: string;
  normalized: {
    bids: { price: string; quantity: string }[];
    asks: { price: string; quantity: string }[];
  };
  received_at: string;
  status_generated_at?: string;
  freshness?: { classification: string; source_age_seconds: number | null; reason?: string | null };
  exchange_timestamp: string | null;
  latency_ms: number | null;
  state: DataState;
  reasons: string[];
  source: string;
  sequence: string | null;
  verification: {
    status?: string;
    independent_references?: {
      exchange: string;
      price: string;
      quote_key: string;
      received_at: string;
    }[];
  };
  storage_origin: string;
  collector_healthy: boolean;
  age_ms: number | null;
  content_sha256: string;
  raw_evidence: unknown;
  actionable: false;
};
export function age(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "Unavailable";
  return seconds < 60
    ? `${seconds.toFixed(1)}s`
    : `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}
export function utc(value: string | null): string {
  return value
    ? new Date(value).toISOString().replace("T", " ").slice(0, 19) + " UTC"
    : "Not received";
}