export type TicketStatus = "OPEN" | "PROCESSING" | "PENDING_REVIEW" | "RESOLVED" | "CLOSED";
export type RefundStatus = "PENDING_REVIEW" | "APPROVED" | "REFUND_PROCESSING" | "REFUNDED" | "REJECTED";
export interface Order { order_no: string; product_name: string; order_status: string; amount: string; logistics_status: string; receiver_phone: string; receiver_address: string; }
export interface LogisticsEvent { occurred_at: string; status: string; description: string; }
export interface Logistics { order_no: string; events: LogisticsEvent[]; latest_event: LogisticsEvent; }
export interface TicketSummary { id: number; status: TicketStatus; created_at: string; }
export interface Ticket { id: number; status: TicketStatus; created_at: string; messages: TicketMessage[]; }
export interface TicketMessage { id: number; role: "USER" | "ASSISTANT"; content: string; created_at: string; }
export interface Refund { id: number; ticket_id: number; order_no: string; reason: string; status: RefundStatus; reviewer_note: string | null; reviewed_at: string | null; refunded_at: string | null; created_at: string; updated_at: string; }
export interface Policy { policy_id: string; title: string; snippet: string; }
