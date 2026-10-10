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
export type NotificationHistory = {
  items: {
    id: string;
    kind: string;
    status: string;
    created_at: string;
    expires_at: string | null;
    delivered_at: string | null;
    error_code: string | null;
    attempts: number;
    message: string;
    alert_id: string | null;
  }[];
  limit: number;
};
export type NotificationPreview = {
  message: string | null;
  eligible?: boolean;
  reasons: string[];
  classification?: string;
  evaluation_id?: string;
};
