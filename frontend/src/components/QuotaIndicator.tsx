import type { QuotaStatus } from "../api/types";

interface Props {
  status: QuotaStatus | null;
  /** Short label, e.g. "tests/hr" or "submits/hr". */
  label: string;
}

/**
 * Compact "used/limit" badge. Tone shifts to warn when remaining < 10% and
 * danger at 0. Hover tooltip shows when the next slot opens. Renders nothing
 * when status is null (still loading).
 */
export function QuotaIndicator({ status, label }: Props) {
  if (status == null) return null;
  const { limit, remaining, next_slot_at } = status;
  const used = limit - remaining;
  const tone =
    remaining === 0 ? "danger" :
    remaining <= Math.max(1, Math.floor(limit * 0.1)) ? "warn" :
    "ok";
  const title = formatTitle(remaining, next_slot_at);
  return (
    <span className={`quota ${tone}`} title={title}>
      {used}/{limit} {label}
    </span>
  );
}

interface SubmitProps {
  hourly: QuotaStatus | null;
  daily: QuotaStatus | null;
}

/** Two-window display for the submit endpoint. */
export function SubmitQuotaIndicator({ hourly, daily }: SubmitProps) {
  if (hourly == null || daily == null) return null;
  return (
    <span className="quota-group">
      <QuotaIndicator status={hourly} label="hr" />
      <QuotaIndicator status={daily} label="day" />
    </span>
  );
}

function formatTitle(remaining: number, nextSlotAt: number | null): string {
  if (nextSlotAt == null) return "Full quota available";
  const when = formatClock(nextSlotAt);
  return remaining === 0
    ? `Limit reached — next slot opens at ${when}`
    : `Next slot opens at ${when}`;
}

function formatClock(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  const today = new Date();
  const hhmm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = d.getDate() === today.getDate() && d.getMonth() === today.getMonth();
  return sameDay ? hhmm : `${d.toLocaleDateString()} ${hhmm}`;
}
