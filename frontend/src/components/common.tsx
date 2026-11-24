import { useEffect, useRef } from "react";
import { AlertCircle, CheckCircle2, Inbox, X } from "lucide-react";
import type { ReactNode } from "react";

export function StatusBadge({ status }: { status: string }) { return <span className={`status status-${status}`}>{status.replaceAll("_", " ")}</span>; }
export function PageState({ type, title, description, onRetry }: { type: "loading" | "empty" | "error"; title?: string; description?: string; onRetry?: () => void }) {
  if (type === "loading") return <div className="skeleton-state"><i/><i/><i/></div>;
  return <div className={`page-state ${type}`}><Inbox size={28}/><strong>{title}</strong><span>{description}</span>{onRetry && <button className="button secondary" onClick={onRetry}>重试</button>}</div>;
}
export function Toast({ notice, onClose }: { notice: { type: "success" | "error"; text: string } | null; onClose: () => void }) { useEffect(() => { if (!notice) return; const id = setTimeout(onClose, 3500); return () => clearTimeout(id); }, [notice, onClose]); return notice ? <div className={`toast ${notice.type}`}>{notice.type === "success" ? <CheckCircle2 size={18}/> : <AlertCircle size={18}/>}<span>{notice.text}</span><button aria-label="关闭提示" onClick={onClose}><X size={16}/></button></div> : null; }
export function Dialog({ title, open, busy, onClose, children }: { title: string; open: boolean; busy?: boolean; onClose: () => void; children: ReactNode }) { const ref = useRef<HTMLDivElement>(null); useEffect(() => { if (open) ref.current?.focus(); }, [open]); if (!open) return null; return <div className="overlay" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}><section className="dialog" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref} onKeyDown={(e) => { if (e.key === "Escape" && !busy) onClose(); }}><header><h2>{title}</h2><button aria-label="关闭弹窗" title="关闭" disabled={busy} onClick={onClose}><X size={20}/></button></header>{children}</section></div>; }
export function IconButton({ label, children, onClick }: { label: string; children: ReactNode; onClick: () => void }) { return <button className="icon-button" aria-label={label} title={label} onClick={onClick}>{children}</button>; }
export const fmt = (value: string | null | undefined) => value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "暂无";
