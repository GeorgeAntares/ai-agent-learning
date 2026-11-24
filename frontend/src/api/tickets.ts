import { request } from "./http"; import type { Ticket } from "../types";
export const getTicket = (id: number) => request<Ticket>(`/api/v1/tickets/${id}`);
export const createTicket = (message: string, order_no?: string) => request<{ id: number }>("/api/v1/tickets", { method: "POST", body: JSON.stringify({ message, ...(order_no ? { order_no } : {}) }) });
export const appendMessage = (id: number, message: string) => request(`/api/v1/tickets/${id}/messages`, { method: "POST", body: JSON.stringify({ message }) });
