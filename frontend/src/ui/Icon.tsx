import { ICONS, type IconName } from "../twin/icons";
import { STATUS } from "../twin/format";
import type { Status } from "../twin/types";

export function Icon({ name, size = 16, color = "currentColor", title }: {
  name: string; size?: number; color?: string; title?: string;
}) {
  return (
    <svg
      className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2}
      strokeLinecap="round" strokeLinejoin="round" role={title ? "img" : undefined} aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      <path d={ICONS[name as IconName]} />
    </svg>
  );
}

/** A status is always a symbol and a word, never only a colour. */
export function StatusChip({ status, small }: { status: Status; small?: boolean }) {
  const s = STATUS[status];
  return (
    <span className={`chip ${small ? "small" : ""}`} data-status={status} style={{ "--c": s.color } as React.CSSProperties}>
      <Icon name={s.icon} size={small ? 11 : 13} color={s.color} />
      {s.label}
    </span>
  );
}

export function StatusDot({ status }: { status: Status }) {
  const s = STATUS[status];
  return <Icon name={s.icon} size={13} color={s.color} title={s.label} />;
}

export function Button({ icon, label, onClick, active, text, disabled, kbd }: {
  icon: string; label: string; onClick: () => void; active?: boolean; text?: boolean; disabled?: boolean; kbd?: string;
}) {
  return (
    <button
      type="button" className={`btn ${active ? "active" : ""}`} onClick={onClick} disabled={disabled}
      title={kbd ? `${label} (${kbd})` : label} aria-label={label} aria-pressed={active}
    >
      <Icon name={icon} size={15} />
      {text && <span>{label}</span>}
    </button>
  );
}
