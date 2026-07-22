import type { Meta, Seed, StreamEvent, Thread } from "./types";

/** 같은 출처의 /api → next.config rewrites가 백엔드로 프록시 */
const PW_KEY = "al_pw";

export const getPw = () =>
  typeof window === "undefined" ? "" : sessionStorage.getItem(PW_KEY) || "";
export const setPw = (pw: string) => sessionStorage.setItem(PW_KEY, pw);

export class AuthError extends Error {
  constructor() { super("인증이 필요합니다"); }
}

function headers(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  const p = getPw();
  if (p) h["X-Auth-Password"] = p;
  return h;
}

async function ok(r: Response): Promise<Response> {
  if (r.status === 401) throw new AuthError();
  if (!r.ok) {
    const d = await r.json().catch(() => ({} as { detail?: string }));
    throw new Error(d.detail || r.statusText);
  }
  return r;
}

export const api = {
  async authRequired(): Promise<boolean | undefined> {
    try {
      const j = await (await fetch("/api/auth-required")).json();
      return j.required as boolean;
    } catch { return undefined; }
  },

  async login(pw: string): Promise<boolean> {
    try {
      return (await fetch("/api/login", { method: "POST", headers: { "X-Auth-Password": pw } })).ok;
    } catch { return false; }
  },

  async meta(): Promise<Meta> {
    try { return await (await fetch("/api/meta")).json(); } catch { return {}; }
  },

  /** NDJSON 스트림 → 이벤트 async generator (검색·대화 공용) */
  async *stream(path: string, body: unknown, signal: AbortSignal): AsyncGenerator<StreamEvent> {
    const r = await ok(await fetch(path, {
      method: "POST", headers: headers(), body: JSON.stringify(body), signal,
    }));
    const reader = r.body!.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i: number;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (line) yield JSON.parse(line) as StreamEvent;
      }
    }
  },

  async evidenceFeedback(body: unknown): Promise<void> {
    await fetch("/api/evidence-feedback", { method: "POST", headers: headers(), body: JSON.stringify(body) });
  },

  async feedback(message: string, contact: string): Promise<void> {
    await ok(await fetch("/api/feedback", {
      method: "POST", headers: headers(), body: JSON.stringify({ message, contact }),
    }));
  },

  async threads(): Promise<{ id: number; title?: string; n?: number }[]> {
    const r = await ok(await fetch("/api/chat/threads", { headers: headers() }));
    return (await r.json()).threads || [];
  },

  async thread(tid: number): Promise<Thread> {
    return await (await ok(await fetch(`/api/chat/threads/${tid}`, { headers: headers() }))).json();
  },

  async createThread(seed: Seed): Promise<Thread> {
    return await (await ok(await fetch("/api/chat/threads", {
      method: "POST", headers: headers(), body: JSON.stringify({ seed }),
    }))).json();
  },

  async postState(tid: number, state: Record<string, unknown>): Promise<Thread> {
    return await (await ok(await fetch(`/api/chat/threads/${tid}/state`, {
      method: "POST", headers: headers(), body: JSON.stringify({ state }),
    }))).json();
  },
};

/** 인용문에서 Ctrl+F용 구절 추출(DART 원문 탐색 보조) */
export function searchSnippet(q: string): string {
  q = (q || "").split("|")[0].replace(/\s+/g, " ").trim();
  if (q.length < 4) return "";
  if (q.length > 24) q = q.slice(0, 24).replace(/\S*$/, "").trim() || q.slice(0, 24);
  return q;
}
