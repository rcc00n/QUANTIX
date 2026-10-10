export type NotificationStatus = {
  paired: boolean;
  pairing: { id: string; username: string | null; approved_at: string; paused: boolean } | null;
  pending_challenges: {
    id: string;
    expires_at: string;
    requested: boolean;
    telegram_username: string | null;
    requested_at: string | null;
  }[];
  worker: {
    heartbeat_at: string | null;
    healthy: boolean;
    bot_username: string | null;
    error_code: string | null;
    webhook_conflict: boolean;
  } | null;
  telegram_enabled: boolean;
  delivery_active: boolean;
  providers: {
    telegram: { available: boolean };
    push: { enabled: false; reason: string };
    voice: { enabled: false; reason: string };
  };
  queue_counts: Record<string, number>;
};