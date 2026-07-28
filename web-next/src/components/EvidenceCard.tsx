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

/** 표시용 텍스트(quote_display·context_display)의 구조화 렌더 — B안.
    〔표: …〕 캡션·(단위: …)는 소제목으로, ▸ 행은 행 스타일로, 메타 헤더는 흐리게.
    hl(인용 표시용)이 주어지면 인용에 포함된 행을 행 단위로 하이라이트한다. */
function DisplayBlock({ text, hl }: { text: string; hl?: string }) {
  const hlSet = new Set(
    (hl || "").split("\n").map((l) => l.replace(/\s/g, "")).filter((l) => l.length > 3),
  );
  return (
    <>
      {text.split("\n").map((line, i) => {
        const s = line.trim();
        if (!s) return <div key={i} className="h-1.5" />;
        if (s.startsWith("〔표:") || /^\(단위:.*\)$/.test(s)) {
          return (
            <div key={i} className="mt-2 first:mt-0 text-[11.5px] font-bold text-green-deep/90
                                     border-l-2 border-green/40 pl-1.5">
              {s.replace(/^〔표:\s*/, "").replace(/〕/, " ")}
            </div>
          );
        }
        if (i === 0 && s.startsWith("[") && s.endsWith("]")) {
          return <div key={i} className="text-[11px] text-ink-2/70 mb-1">{s}</div>;
        }
        if (s.startsWith("▸")) {
          const on = hlSet.has(line.replace(/\s/g, ""));
          return (
            <div key={i}
              className={`pl-2 py-[1px] ${on ? "bg-yellow-100 rounded" : ""}`}>
              {s}
            </div>
          );
        }
        return <div key={i}>{line}</div>;
      })}
    </>
  );
}

export function EvidenceCard({ it, lastQ, lastPath, onToast, selCode }: {
  it: EvidenceItem; lastQ: string; lastPath: string; onToast: (html: React.ReactNode) => void;
  selCode?: string;   // 활성 업종 필터 코드(분류1 1자 또는 분류2 3자) — 매칭 라벨을 배지로 노출
}) {
  const [ctxOpen, setCtxOpen] = useState(false);
  const [fbSent, setFbSent] = useState<"" | "up" | "down">("");
  const ok = !!it.verified;
  const basis = it.is_consolidated ? "연결" : "별도";
  const hasCtx = !!it.context && it.context.length > (it.quote || "").length + 20;
  const ind = it.industry;
  // 배지는 핵심 라벨만(최대 2개) — 보조·노출까지 붙이면 카드가 소란해진다. 전체는 툴팁으로.
  // 단, 업종 필터로 검색된 경우 그 필터와 일치하는 라벨은 반드시 맨 앞에 노출한다.
  // (보조 라벨로 매칭된 다중 라벨 기업이 '다른 업종'처럼 보이는 착시 방지 — 예: 포장재 필터에
  //  잡힌 풍산홀딩스는 핵심이 비철금속이라 포장재 배지가 없으면 무관해 보인다)
  const allLabels = ind?.labels || [];
  // selCode: 분류1 1자("B") · 분류2("B05") · 자동 인식 복합형("C01+C02") 모두 지원
  const selKeys = (selCode || "").split("+").filter(Boolean);
  const selMatch = selKeys.length
    ? allLabels.find((l) => selKeys.some((k) => (k.length === 1 ? l.code?.[0] === k : l.code === k)))
    : undefined;
  const coreLabels = allLabels.filter((l) => l.materiality === "핵심" && l !== selMatch).slice(0, selMatch ? 1 : 2);
  const shown = selMatch ? [selMatch, ...coreLabels] : coreLabels;
  const indTip = allLabels
    .map((l) => `${l.cls2}[${l.materiality}${l.rel && l.rel !== "직접영위" ? "·" + l.rel : ""}]`)
    .join(" · ");

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
        {shown.map((l) => (
          <span key={l.code}
            className={`rounded text-[11px] px-1.5 py-0.5 ${l === selMatch
              ? "bg-green text-white"
              : "bg-green-soft text-green-deep"}`}
            title={(l === selMatch ? `선택한 업종과 일치하는 라벨 (중요도: ${l.materiality})\n` : "")
              + `${l.cls1} › ${l.cls2} (${l.code}) — ${indTip}${l.basis ? `\n근거: ${l.basis}` : ""}`}>
            {l.cls1} › {l.cls2}{l === selMatch && l.materiality !== "핵심" ? ` · ${l.materiality}` : ""}
          </span>
        ))}
        {shown.length > 0 && (
          <span className="rounded text-[11px] px-1.5 py-0.5 border border-line text-ink-2"
            title={`감사쟁점 유사성 기준 자체 분류 — 전체 라벨: ${indTip}`}>
            {ind?.source || "자체 분류"}
          </span>
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
          {/* 표 청크 인용은 서버가 만든 표시용 텍스트(quote_display)를 구조화 렌더 —
              검증·복사·DART 검색은 원본 quote 기준 그대로 유지 */}
          {it.quote_display ? <DisplayBlock text={it.quote_display} /> : it.quote}
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
              {/* 표시용이 있으면 구조화 렌더(행 단위 하이라이트), 없으면 원본 하이라이트 */}
              {it.context_display
                ? <DisplayBlock text={it.context_display} hl={it.quote_display || ""} />
                : <HighlightedCtx ctx={it.context || ""} quote={it.quote || ""} />}
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
