import { describe, expect, it, vi } from "vitest";

import { ApiError, request } from "./http";

describe("request", () => {
  it("returns parsed JSON for a successful response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 7 }), { status: 200 })));

    await expect(request<{ id: number }>("/api/v1/tickets/7")).resolves.toEqual({ id: 7 });
  });

  it("uses the backend message and request id for controlled errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "ticket_not_found", message: "Ticket not found", request_id: "req-42" }), { status: 404 })));

    await expect(request("/api/v1/tickets/7")).rejects.toMatchObject({
      status: 404,
      code: "ticket_not_found",
      message: "Ticket not found",
      requestId: "req-42",
    });
  });

  it("maps validation and network failures to user-facing messages", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response("{}", { status: 422 })).mockRejectedValueOnce(new TypeError("offline")));

    await expect(request("/api/v1/tickets")).rejects.toMatchObject({ message: "输入内容不符合要求，请检查后重试。" });
    await expect(request("/api/v1/tickets")).rejects.toMatchObject({ message: "网络连接失败，请确认服务是否已启动。" });
  });
});
