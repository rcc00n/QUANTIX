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