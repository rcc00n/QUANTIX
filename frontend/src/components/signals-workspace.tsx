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