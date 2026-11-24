import { request } from "./http"; import type { Policy } from "../types";
export const searchPolicies = async (query: string) => (await request<{ results: Policy[] }>(`/api/v1/policies/search?query=${encodeURIComponent(query)}`)).results;
