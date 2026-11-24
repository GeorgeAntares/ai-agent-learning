import { request } from "./http"; import type { Refund } from "../types";
export const getOrderRefunds = (orderNo: string) => request<Refund[]>(`/api/v1/orders/${orderNo}/refund-requests`);
export const getTicketRefunds = (id: number) => request<Refund[]>(`/api/v1/tickets/${id}/refund-requests`);
export const getRefund = (id: number) => request<Refund>(`/api/v1/refund-requests/${id}`);
export const createRefund = (id: number, order_no: string, reason: string) => request<Refund>(`/api/v1/tickets/${id}/refund-requests`, { method: "POST", body: JSON.stringify({ order_no, reason }) });
