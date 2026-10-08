import { NextRequest, NextResponse } from "next/server";

export function proxy(request: NextRequest) {
  const hosts = ["127.0.0.1:3000", "localhost:3000"];
  const origin = request.headers.get("origin");
  const allowed = ["http://127.0.0.1:3000", "http://localhost:3000"];
  if (!hosts.includes(request.headers.get("host") ?? "") || (origin && !allowed.includes(origin))) {
    return NextResponse.json({ error: { code: "FORBIDDEN", message: "请通过本机地址访问" } }, { status: 403 });
  }
  return NextResponse.next();
}
