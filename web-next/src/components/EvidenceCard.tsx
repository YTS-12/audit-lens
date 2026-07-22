"use client";

import { useState } from "react";
import { api, searchSnippet } from "@/lib/api";
import type { EvidenceItem } from "@/lib/types";

/** 섹션 원문에서 인용부를 <mark>로 하이라이트(공백·구분자 관대 매칭) */
function HighlightedCtx({ ctx, quote }: { ctx: string; quote: string }) {
  const q = (quote || "").trim();
  if (q) {
    const toks = q.slice(0, 140).split(/[\s|]+/).filter(Boolean)
      .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (toks.length) {
      try {
        const m = ctx.match(new RegExp(toks.join("[\\s|]*")));
        if (m && m.index !== undefined) {
          const i = m.index, len = m[0].length;
          return (
            <>
              {ctx.slice(0, i)}<mark>{ctx.slice(i, i + len)}</mark>{ctx.slice(i + len)}
            </>
          );
        }
      } catch { /* 정규식 실패 시 원문 그대로 */ }
    }
  }
  return <>{ctx}</>;
}

export function EvidenceCard({ it, lastQ, lastPath, onToast }: {
  it: EvidenceItem; lastQ: string; lastPath: string; onToast: (html: React.ReactNode) => void;
}) {
  const [ctxOpen, setCtxOpen] = useState(false);
  const [fbSent, setFbSent] = useState<"" | "up" | "down">("");
  const ok = !!it.verified;
  const basis = it.is_consolidated ? "연결" : "별도";
  const hasCtx = !!it.context && it.context.length > (it.quote || "").length + 20;
  const w = it.wics;
  const wOff = !!w && (w.source || "").includes("공식");

  const sendFb = async (verdict: "up" | "down") => {
    if (fbSent) return;
    setFbSent(verdict);
    try {
      await api.evidenceFeedback({
        verdict, question: lastQ, corp_code: it.corp_code || "",
        corp_name: it.corp_name || "", quote: (it.quote || "").slice(0, 300), path: lastPath,
      });
      onToast(verdict === "up" ? "👍 근거 평가 감사합니다" : "👎 반영하겠습니다 — 개선에 활용됩니다");
    } catch { /* 무해 */ }
  };

  const copySnippet = () => {
    const snip = searchSnippet(it.quote || "");
    if (snip) {
      try { navigator.clipboard.writeText(snip); } catch { /* 무해 */ }
      onToast(<>🔍 검색어 <b>&ldquo;{snip}&rdquo;</b> 복사됨 — DART 원문에서 <b>Ctrl+F → Ctrl+V</b>로 찾으세요</>);
    }
  };

  return (
    <div className="rounded-xl border border-line bg-card p-4 shadow-sm">
      <div className="flex flex-wrap items-center gap-1.5 mb-2">
        <span className="font-bold text-[15px]">{it.corp_name}</span>
        {w && w.dae && w.dae !== "미분류" && (
          <>
            <span className="rounded bg-green-soft text-green-deep text-[11px] px-1.5 py-0.5"
              title={`WICS 코드 ${w.code || ""} · ${w.jung || ""}`}>
              {w.dae}{w.so ? ` › ${w.so}` : ""}
            </span>
            <span
              className={`rounded text-[11px] px-1.5 py-0.5 ${wOff ? "bg-green text-white" : "bg-line/60 text-ink-2"}`}
              title={wOff ? undefined : `WICS 분류 근거: ${w.basis || ""} (확신도 ${w.confidence || ""})`}>
              {wOff ? "WICS 공식" : "WICS 추정"}
            </span>
          </>
        )}
        <span className="rounded bg-bg text-ink-2 text-[11px] px-1.5 py-0.5 border border-line">
          {it.fiscal_year || ""} · {basis}
        </span>
        <span className={`rounded text-[11px] px-1.5 py-0.5 ${ok ? "bg-green text-white" : "border border-line text-ink-2"}`}>
          {ok ? "● 원문 확인됨" : "○ 원문 미확인"}
        </span>
        {it.source === "ondemand" && (
          <span className="rounded bg-bg text-ink-2 text-[11px] px-1.5 py-0.5 border border-line">⊕ 본문에서 추가 검색</span>
        )}
      </div>
      <div className="font-semibold text-[14px] mb-1.5">{it.conclusion}</div>
      {it.quote && (
        <div className="text-[13px] text-ink-2 leading-relaxed bg-bg rounded-lg p-2.5 whitespace-pre-line">
          {it.quote}
        </div>
      )}
      {hasCtx && (
        <div className="mt-2">
          <button type="button" onClick={() => setCtxOpen(!ctxOpen)}
            className="text-xs text-green-deep font-semibold hover:underline">
            {ctxOpen ? "📄 원문 접기" : "📄 원문 보기"}
          </button>
          {ctxOpen && (
            <div className="mt-1.5 text-[12.5px] leading-relaxed border border-line rounded-lg p-3 max-h-72 overflow-y-auto whitespace-pre-line">
              <HighlightedCtx ctx={it.context || ""} quote={it.quote || ""} />
            </div>
          )}
        </div>
      )}
      <div className="mt-2.5 flex flex-wrap items-center gap-2 text-[11.5px] text-ink-2">
        <span className="truncate max-w-[55%]">{it.section_path || ""}</span>
        {it.dart_url && (
          <a href={it.dart_url} target="_blank" rel="noopener noreferrer" onClick={copySnippet}
            className="text-green-deep font-semibold hover:underline">
            DART 원문 열기 ↗
          </a>
        )}
        <span className="ml-auto inline-flex gap-1">
          <button type="button" disabled={!!fbSent} onClick={() => sendFb("up")}
            title="이 근거가 질문에 유효합니다"
            className={`rounded border border-line px-1.5 py-0.5 hover:bg-bg disabled:opacity-40 ${fbSent === "up" ? "bg-green-soft border-green" : ""}`}>
            👍
          </button>
          <button type="button" disabled={!!fbSent} onClick={() => sendFb("down")}
            title="이 근거는 틀리거나 무관합니다"
            className={`rounded border border-line px-1.5 py-0.5 hover:bg-bg disabled:opacity-40 ${fbSent === "down" ? "bg-green-soft border-green" : ""}`}>
            👎
          </button>
        </span>
      </div>
    </div>
  );
}
