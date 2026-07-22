import type { NextConfig } from "next";

// 로컬 FastAPI 백엔드(자기서명 https)로의 서버사이드 프록시.
// 브라우저는 같은 출처(/api)만 호출 → CORS·인증서 문제를 Next 서버가 흡수한다.
// 배포 시 BACKEND_URL 환경변수로 교체(예: https://api.auditlens.kr).
const BACKEND = process.env.BACKEND_URL || "https://127.0.0.1:8000";

if (process.env.NODE_ENV !== "production") {
  // dev 한정: 로컬 백엔드의 자기서명 인증서 허용(프록시 fetch용)
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
}

// dev: /api/* 프록시는 src/app/api/[...path]/route.ts(스트리밍 통과)가 담당.
// prod: BUILD_STATIC=1 로 정적 export → FastAPI가 같은 출처에서 서빙(프록시 불필요).
//       (route handler는 export와 비호환이라 빌드 스크립트가 api 디렉터리를 잠시 치웠다 복원)
void BACKEND;
const nextConfig: NextConfig = process.env.BUILD_STATIC
  ? { output: "export" }
  : {};

export default nextConfig;
