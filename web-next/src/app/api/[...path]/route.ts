import type { NextRequest } from "next/server";

// 백엔드(FastAPI) 프록시 — rewrites 대신 Route Handler를 쓰는 이유:
// dev 서버의 rewrites 프록시는 장시간 무데이터 스트림(NDJSON 보완 구간 ~80초)의
// 꼬리를 전달하지 못하는 문제가 있어, fetch 스트림을 그대로 통과시키는 핸들러로 대체.
const BACKEND = process.env.BACKEND_URL || "http://127.0.0.1:8200";

export const dynamic = "force-dynamic";

async function proxy(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const url = `${BACKEND}/api/${path.join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const k of ["content-type", "x-auth-password", "accept"]) {
    const v = req.headers.get(k);
    if (v) headers.set(k, v);
  }
  const init: RequestInit & { duplex?: "half" } = {
    method: req.method,
    headers,
    cache: "no-store",
  };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = req.body;
    init.duplex = "half";                 // 요청 본문 스트리밍에 필요(undici)
  }
  const res = await fetch(url, init);
  return new Response(res.body, {
    status: res.status,
    headers: {
      "content-type": res.headers.get("content-type") || "application/json",
      "cache-control": "no-cache",
    },
  });
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as DELETE };
