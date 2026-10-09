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