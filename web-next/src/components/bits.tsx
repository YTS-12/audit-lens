"use client";

import type { Understanding } from "@/lib/types";

/** 내부값 → 회계사 친화 표현 (기존 UI와 동일) */
export const INTENT: Record<string, string> = {
  스크리닝: "여러 기업 거르기", 단건: "특정 기업 조회", 요약: "요약·사례 찾기", 수치: "수치 비교",
};
export const DOCT: Record<string, string> = {
  audit: "감사보고서", financial_note: "재무제표 주석", financial_stmt: "재무제표",
};

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-ink-2 text-sm">
      <span className="spin" />{label}
    </span>
  );
}

export function Pill({ children, dim }: { children: React.ReactNode; dim?: boolean }) {
  return (
    <span className={`inline-block rounded-full border border-line px-2.5 py-0.5 text-xs
      ${dim ? "text-ink-2 bg-white" : "bg-green-soft text-green-deep border-green/30"}`}>
      {children}
    </span>
  );
}

export function InterpPanel({ u }: { u: Understanding }) {
  const filters: string[] = [];
  if (u.industry) filters.push("산업: " + u.industry);
  if (u._wics_auto) filters.push(`업종 해석: ${u._wics_auto.label} (${u._wics_auto.n}사)`);
  if (u.is_consolidated === true) filters.push("연결");
  else if (u.is_consolidated === false) filters.push("별도");
  if (u.fiscal_years?.length) filters.push("연도: " + u.fiscal_years.join(","));
  if (u.doc_types?.length) filters.push(u.doc_types.map((d) => DOCT[d] || d).join("/"));
  return (
    <div className="rounded-xl border border-line bg-white p-3.5 mb-3">
      <div className="text-[11px] font-bold text-green-deep mb-2">이렇게 이해했어요</div>
      <div className="flex flex-wrap gap-1.5 mb-1.5">
        <Pill>질문 유형: {INTENT[u.intent || ""] || u.intent || "-"}</Pill>
        {filters.map((f) => <Pill key={f}>{f}</Pill>)}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {(u.expanded_queries || []).map((q) => <Pill key={q} dim>› {q}</Pill>)}
      </div>
    </div>
  );
}

export function AnswerBox({ answer, analysis, verified, total, insufficient }: {
  answer?: string; analysis?: string; verified: number; total: number; insufficient?: boolean;
}) {
  return (
    <div className="rounded-xl border border-green/25 bg-green-soft/60 p-4 mb-3">
      <div className="text-xs text-ink-2 mb-1.5">
        <b className="text-green-deep">{total}건</b> · 원문 확인 {verified}/{total}
      </div>
      {answer && <div className="font-semibold leading-relaxed max-w-[90ch]">{answer}</div>}
      {analysis && (
        <div className="mt-2 text-[13.5px] leading-relaxed text-ink whitespace-pre-line max-w-[90ch]">{analysis}</div>
      )}
      {insufficient && (
        <div className="mt-2 text-xs text-warn">근거가 불충분합니다 — 회계사 확인 권장.</div>
      )}
      <div className="mt-2 text-[11px] text-ink-2">
        적재된 데이터 기준이며 누락 가능성이 있어 회계사의 최종 확인이 필요합니다.
      </div>
    </div>
  );
}

export function Empty({ big, sub }: { big: string; sub?: React.ReactNode }) {
  return (
    <div className="text-center py-12 text-ink-2">
      <div className="text-lg font-semibold text-ink mb-1.5">{big}</div>
      {sub && <div className="text-sm leading-relaxed">{sub}</div>}
    </div>
  );
}
