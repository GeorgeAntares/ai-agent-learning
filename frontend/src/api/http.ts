const baseUrl = import.meta.env.VITE_API_BASE_URL || "";

export class ApiError extends Error {
  constructor(public status: number, public code?: string, message = "请求失败，请稍后重试。", public requestId?: string) { super(message); }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    const response = await fetch(`${baseUrl}${path}`, { headers: { "Content-Type": "application/json", ...init?.headers }, ...init });
    const payload: unknown = await response.json().catch(() => ({}));
    if (!response.ok) {
      const body = payload as { code?: string; message?: string; request_id?: string };
      const message = response.status === 422 ? "输入内容不符合要求，请检查后重试。" : body.message || (response.status === 404 ? "未找到对应资源，请检查编号后重试。" : "请求失败，请稍后重试。");
      if (body.request_id) console.info("AfterFlow request_id:", body.request_id);
      throw new ApiError(response.status, body.code, message, body.request_id);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, "network_error", "网络连接失败，请确认服务是否已启动。");
  }
}
