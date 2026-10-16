"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Bell,
  CirclePause,
  ExternalLink,
  Link2,
  Play,
  Send,
  ShieldCheck,
  Unlink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Empty, LiveStatus, SectionTitle, Tag, Warning } from "@/components/terminal-ui";
import { terminalFetch, useLive } from "@/lib/terminal";
import { utcLabel } from "@/lib/paper";
import type {
  NotificationHistory,
  NotificationPreview,
  NotificationStatus,
} from "@/lib/notifications";

type Challenge = {
  challenge_id: string;
  challenge: string;
  start_url: string | null;
  expires_at: string;
};

export function TelegramConnection({
  compact = false,
  onStatus,
  refreshKey = 0,
}: {
  compact?: boolean;
  onStatus?: (value: NotificationStatus | null) => void;
  refreshKey?: number;
}) {
  const status = useLive<NotificationStatus>("/notifications/status", 30000);
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    onStatus?.(status.data);
  }, [status.data, onStatus]);
  useEffect(() => {
    if (refreshKey) status.refresh();
  }, [refreshKey, status.refresh]);
  async function action(path: string, body?: unknown) {
    setBusy(path);
    setError("");
    setMessage("");
    try {
      const response = await terminalFetch<Challenge | NotificationStatus>(
        `/notifications/${path}`,
        undefined,
        {
          method: "POST",
          body: JSON.stringify(body || {}),
        },
      );
      if (path === "pairing/challenge") setChallenge(response as Challenge);
      else {
        setChallenge(null);
        setMessage(
          path === "pairing/approve"
            ? "Telegram pairing approved."
            : path === "pairing/revoke"
              ? "Telegram pairing revoked."
              : path === "pause"
                ? "Notifications paused."
                : "Notifications resumed subject to your alert policy.",
        );
      }
      status.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Telegram action failed");
    } finally {
      setBusy("");
    }
  }
  const data = status.data;
  const botUrl =
    data?.worker?.bot_username && /^[a-zA-Z0-9_]+$/.test(data.worker.bot_username)
      ? `https://t.me/${data.worker.bot_username}`
      : null;
  const safeStart =
    challenge?.start_url &&
    /^https:\/\/t\.me\/[a-zA-Z0-9_]+\?start=[a-zA-Z0-9_-]+$/.test(challenge.start_url)
      ? challenge.start_url
      : null;
  return (
    <section className="terminal-panel telegram-connection">
      <SectionTitle eyebrow="PRIVATE OWNER CHANNEL" title="Telegram connection">
        <LiveStatus
          error={status.error}
          loading={status.loading}
          received={status.received}
          refresh={status.refresh}
          interval={30}
        />
      </SectionTitle>
      {(status.error || error) && <Warning>{error || status.error}</Warning>}
      {message && (
        <p className="terminal-success" role="status">
          {message}
        </p>
      )}
      <div className="notification-connection-line">
        <div className="notification-identity">
          <Bell size={23} />
          <div>
            <strong>
              {data?.paired
                ? data.pairing?.username
                  ? `@${data.pairing.username.replace(/^@/, "")}`
                  : "Verified owner account"
                : data
                  ? "No Telegram account paired"
                  : status.error
                    ? "Telegram connection unavailable"
                    : "Checking Telegram connection…"}
            </strong>
            <span>
              {data?.worker?.bot_username
                ? `Bot @${data.worker.bot_username}`
                : "Bot identity unavailable"}
            </span>
          </div>
        </div>
        <div className="notification-status-tags">
          <Tag value={data ? (data.paired ? "Paired" : "Unpaired") : "Unknown"} />
          <Tag
            value={
              !data
                ? "Delivery unavailable"
                : data.delivery_active
                  ? "Delivery active"
                  : data?.pairing?.paused
                    ? "Paused"
                    : "Delivery inactive"
            }
          />
        </div>
      </div>
      <dl className="terminal-definition-list notification-connection-details">
        <div>
          <dt>Delivery worker</dt>
          <dd>{!data?.worker ? "Unavailable" : data.worker.healthy ? "Healthy" : "Unhealthy"}</dd>
        </div>
        <div>
          <dt>Last heartbeat · UTC</dt>
          <dd>{utcLabel(data?.worker?.heartbeat_at)}</dd>
        </div>
        {data?.worker?.error_code && (
          <div>
            <dt>Connection issue</dt>
            <dd>{data.worker.error_code.replaceAll("_", " ")}</dd>
          </div>
        )}
        {data?.worker?.webhook_conflict && (
          <div>
            <dt>Update consumer</dt>
            <dd>Webhook conflict · polling blocked</dd>
          </div>
        )}
      </dl>
      <p className="terminal-description">
        Create a pairing link here, open it in Telegram and send its /start challenge, then approve
        your pending request while signed in. Only the paired owner can access private commands.
      </p>
      <div className="paper-actions">
        {botUrl && (
          <a href={botUrl} target="_blank" rel="noopener noreferrer" className="text-link">
            Open Telegram bot <ExternalLink size={13} />
          </a>
        )}
        {!data?.paired && (
          <Button
            variant="outline"
            size="sm"
            disabled={!!busy || !data}
            onClick={() => void action("pairing/challenge")}
          >
            <Link2 size={14} />
            Create pairing link
          </Button>
        )}
        {data?.paired && (
          <>
            <Button
              variant="outline"
              size="sm"
              disabled={!!busy}
              onClick={() => void action(data.pairing?.paused ? "resume" : "pause")}
            >
              {data.pairing?.paused ? <Play size={14} /> : <CirclePause size={14} />}
              {data.pairing?.paused ? "Resume notifications" : "Pause notifications"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={!!busy}
              onClick={() => void action("pairing/revoke")}
            >
              <Unlink size={14} />
              Revoke Telegram pairing
            </Button>
          </>
        )}
      </div>
      {challenge && (
        <div className="notification-pairing-instructions">
          <strong>Pairing link expires {utcLabel(challenge.expires_at)}</strong>
          {safeStart ? (
            <a href={safeStart} target="_blank" rel="noopener noreferrer" className="text-link">
              Continue in Telegram <ExternalLink size={13} />
            </a>
          ) : (
            <p>
              Send <code>/start {challenge.challenge}</code> to the bot.
            </p>
          )}
          <p>Return here after Telegram receives the challenge and approve your account.</p>
        </div>
      )}
      {data?.pending_challenges
        .filter((item) => item.requested)
        .map((item) => (
          <div className="notification-pending" key={item.id}>
            <div>
              <strong>
                {item.telegram_username
                  ? `@${item.telegram_username.replace(/^@/, "")}`
                  : "Telegram account awaiting approval"}
              </strong>
              <span>
                Requested {utcLabel(item.requested_at)} · expires {utcLabel(item.expires_at)}
              </span>
              <small>Approve only the request you initiated in Telegram.</small>
            </div>
            <Button
              size="sm"
              disabled={!!busy}
              onClick={() => void action("pairing/approve", { challenge_id: item.id })}
            >
              <ShieldCheck size={14} />
              Approve pairing
            </Button>
          </div>
        ))}
      {compact && (
        <Link href="/signals" className="text-link">
          Open notification history and previews <ExternalLink size={13} />
        </Link>
      )}
    </section>
  );
}

export function SignalsWorkspace() {
  const [status, setStatus] = useState<NotificationStatus | null>(null);
  const [statusRevision, setStatusRevision] = useState(0);
  const history = useLive<NotificationHistory>("/notifications/history?limit=30", 30000);
  const preview = useLive<NotificationPreview>("/notifications/preview", 60000);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function action(path: string) {
    setBusy(path);
    setMessage("");
    setError("");
    try {
      const result = await terminalFetch<{ id?: string; status?: string }>(
        `/notifications/${path}`,
        undefined,
        { method: "POST", body: "{}" },
      );
      setMessage(
        path === "test"
          ? `Test notification ${result.status?.toLowerCase() || "queued"}. Delivery is confirmed only by the recorded outcome.`
          : path.endsWith("acknowledge")
            ? "Alert acknowledged. Pending repeats are cancelled."
            : "Alert cancelled.",
      );
      history.refresh();
      setStatusRevision((value) => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Notification action failed");
    } finally {
      setBusy("");
    }
  }
  return (
    <>
      <TelegramConnection onStatus={setStatus} refreshKey={statusRevision} />
      <div className="terminal-split notification-context">
        <section className="terminal-panel">
          <SectionTitle eyebrow="COMPUTED FROM RETAINED EVIDENCE" title="Message preview">
            <Tag
              value={
                preview.data?.classification || (preview.data ? "No candidate" : "Unavailable")
              }
            />
          </SectionTitle>
          {preview.error && <Warning>{preview.error}</Warning>}
          {preview.data?.message ? (
            <pre className="notification-preview">{preview.data.message}</pre>
          ) : (
            <Empty title="No eligible preview">
              A preview requires a retained quantitative evaluation. No example prices are
              substituted.
            </Empty>
          )}
          {!!preview.data?.reasons?.length && (
            <div className="notification-reasons">
              {preview.data.reasons.map((reason) => (
                <span key={reason}>{reason.replaceAll("_", " ")}</span>
              ))}
            </div>
          )}
          <p className="terminal-footnote">
            Preview is not delivery. Historical quotes may have expired. Research remains separate
            from actionable signals and simulated executions.
          </p>
        </section>
        <section className="terminal-panel">
          <SectionTitle eyebrow="DELIVERY CONTROLS" title="Queue and providers" />
          <dl className="terminal-definition-list">
            {Object.entries(status?.queue_counts || {}).map(([key, value]) => (
              <div key={key}>
                <dt>{key.replaceAll("_", " ")}</dt>
                <dd>{value}</dd>
              </div>
            ))}
            <div>
              <dt>Telegram</dt>
              <dd>{status?.providers.telegram.available ? "Available" : "Unavailable"}</dd>
            </div>
            <div>
              <dt>Paid push</dt>
              <dd>Disabled · separate authorization required</dd>
            </div>
            <div>
              <dt>Voice calls</dt>
              <dd>Disabled · separate authorization required</dd>
            </div>
          </dl>
          <p className="terminal-description">
            Telegram cannot make ordinary phone calls. Delivery cannot guarantee an iPhone Focus
            override or wake-up.
          </p>
          <div className="paper-actions">
            <Button
              variant="outline"
              disabled={!!busy || !status?.paired || !status?.worker?.healthy}
              onClick={() => void action("test")}
            >
              <Send size={14} />
              Send owner test notification
            </Button>
            <Link href="/settings#notification-policy" className="text-link">
              Edit alert policy <ExternalLink size={13} />
            </Link>
          </div>
          <p className="terminal-footnote">
            The test button explicitly queues one message to the paired owner.
          </p>
        </section>
      </div>
      {error && <Warning>{error}</Warning>}
      {message && (
        <p className="terminal-success" role="status">
          {message}
        </p>
      )}
      <section className="terminal-panel">
        <SectionTitle eyebrow="DURABLE DELIVERY AUDIT" title="Notification history">
          <LiveStatus
            error={history.error}
            loading={history.loading}
            received={history.received}
            refresh={history.refresh}
            interval={30}
          />
        </SectionTitle>
        {history.error && <Warning>{history.error}</Warning>}
        {history.data?.items.length ? (
          <div className="notification-history">
            {history.data.items.map((item) => (
              <article key={item.id} className="notification-history-row">
                <div className="notification-history-summary">
                  <div>
                    <Tag value={item.kind} />
                    <Tag value={item.status} />
                    <time>{utcLabel(item.created_at)}</time>
                  </div>
                  <span>
                    {item.attempts} attempt{item.attempts === 1 ? "" : "s"}
                    {item.delivered_at ? ` · delivered ${utcLabel(item.delivered_at)}` : ""}
                  </span>
                </div>
                {item.error_code && (
                  <p className="error-text">{item.error_code.replaceAll("_", " ")}</p>
                )}
                <details className="evidence-details">
                  <summary>Message and expiry</summary>
                  <pre>{item.message}</pre>
                  <p className="terminal-footnote">Expires {utcLabel(item.expires_at)}</p>
                </details>
                {item.alert_id &&
                  !["ACKNOWLEDGED", "CANCELLED", "EXPIRED"].includes(item.status) && (
                    <div className="paper-actions">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!!busy}
                        onClick={() => void action(`${item.alert_id}/acknowledge`)}
                      >
                        Acknowledge alert
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={!!busy}
                        onClick={() => void action(`${item.alert_id}/cancel`)}
                      >
                        Cancel alert
                      </Button>
                    </div>
                  )}
              </article>
            ))}
          </div>
        ) : (
          <Empty title="No recorded notifications">
            Messages appear here only after they enter the durable queue. No delivery is invented.
          </Empty>
        )}
        <p className="terminal-footnote">
          Latest {history.data?.limit || 30} queue records. Delivered messages are not simulated or
          real trades.
        </p>
      </section>
    </>
  );
}
