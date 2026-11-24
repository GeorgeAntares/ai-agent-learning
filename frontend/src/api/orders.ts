import { request } from "./http"; import type { Logistics, Order, TicketSummary } from "../types";
export const getOrder = (orderNo: string) => request<Order>(`/api/v1/orders/${orderNo}`);
export const getLogistics = (orderNo: string) => request<Logistics>(`/api/v1/orders/${orderNo}/logistics`);
export const getOrderTickets = async (orderNo: string) => (await request<{ tickets: TicketSummary[] }>(`/api/v1/orders/${orderNo}/tickets`)).tickets;
