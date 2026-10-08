import { readFile } from "node:fs/promises";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
const origins = ["http://127.0.0.1:3000", "http://localhost:3000"];

async function relay(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const route = (await context.params).path.join("/");
  const project = "projects/[0-9a-f-]{36}";
  const readAllowed = new RegExp(`^(health|settings|model/status|projects|${project}|${project}/planning|${project}/planning/assets/[0-9a-f-]{36}|${project}/requirements|${project}/requirements/revisions/[0-9]+|${project}/requirements/baselines/[0-9a-f-]{36}|${project}/analysis/runs(/[0-9a-f-]{36})?)$`).test(route);
  const writeAllowed = new RegExp(`^(model/config|projects/(create-preview|import-preview|confirm)|${project}/planning/(environment|technical/(generate|revisions|confirm)|plans/(generate|revisions|confirm))|${project}/requirements/(revisions|check|answers|freeze)|${project}/analysis/(preview|authorize|runs)|${project}/analysis/runs/[0-9a-f-]{36}/adopt|${project}/analysis/authorizations/[0-9a-f-]{36}/revoke)$`).test(route);
  const error = (code: string, message: string, status: number) =>
    NextResponse.json({ error: { code, message } }, { status });
  if (!((request.method === "GET" && readAllowed) || (request.method === "POST" && writeAllowed))) {
    return error("NOT_FOUND", "请求地址不存在", 404);
  }
  const origin = request.headers.get("origin");
  if (request.method === "POST" && !origins.includes(origin ?? "")) {
    return error("FORBIDDEN", "写请求需要工作台来源", 403);
  }
  try {
    const dataDir = process.env.FACTORY_DATA_DIR ?? path.resolve(process.cwd(), "../data");
    const token = (await readFile(path.join(dataDir, "backend_token"), "utf8")).trim();
    const body = request.method === "POST" ? await request.text() : undefined;
    const bodyLimit = route.includes("/planning/") ? 1_048_576 : 131_072;
    if (body && Buffer.byteLength(body) > bodyLimit) return error("INPUT_INVALID", "请求内容过大", 413);
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    if (origin) headers.Origin = origin;
    if (body) headers["Content-Type"] = "application/json";
    const key = request.headers.get("idempotency-key");
    if (key) headers["Idempotency-Key"] = key;
    const response = await fetch(`http://127.0.0.1:8765/api/v1/factory/${route}${request.nextUrl.search}`, {
      method: request.method, headers, body, cache: "no-store", signal: AbortSignal.timeout(30_000),
    });
    return new NextResponse(await response.text(), {
      status: response.status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return error("SERVICE_UNAVAILABLE", "本机服务暂不可用，请确认服务已启动后重试", 503);
  }
}
export const GET = relay;
export const POST = relay;
