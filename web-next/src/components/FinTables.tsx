"use client";

import type { FinTable } from "@/lib/types";

function isNumCell(c?: string): boolean {
  c = (c || "").trim();
  if (["-", "–", "—", "(+)", "(-)"].includes(c)) return true;
  return /\d/.test(c) && /^\(?[-+]?[\d,]+(\.\d+)?\)?%?p?$/.test(c.replace("(-)", "-"));
}

/** 해당 열이 '수치 열'인가 — 값이 있는 셀의 과반이 숫자면 참(머리글 정렬·빈칸 대시 판단용). */
function numCol(grid: NonNullable<FinTable["grid"]>, ci: number): boolean {
  if (ci === 0) return false;                       // 첫 열은 행 라벨
  let filled = 0, nums = 0;
  for (const r of grid.rows || []) {
    const v = (r.cells?.[ci] || "").trim();
    if (!v) continue;
    filled++;
    if (isNumCell(v)) nums++;
  }
  return filled > 0 && nums * 2 >= filled;
}

function Badge({ t }: { t: FinTable }) {
  if (t.grid) {
    const [label, title] =
      t.src === "store"
        ? ["✓ 공시 원본 표", "공시 원본 문서(XML)의 표 구조를 변형 없이 그대로 가져왔습니다"]
        : t.src === "xbrl"
        ? ["✓ XBRL 정형수치", "OpenDART 전체 재무제표(XBRL) 정형 수치 — 공시 데이터 그대로"]
        : ["✓ 숫자 원문검증", "AI가 표 구조를 정리했고, 모든 숫자는 원문과 기계 대조로 검증되었습니다"];
    return <span title={title} className="rounded bg-green text-white text-[10.5px] px-1.5 py-0.5">{label}</span>;
  }
  const [label, title] = t.lines?.length
    ? ["항목별 정리", "복잡한 표라 항목별로 정리해 보여드립니다(값은 원문 그대로)"]
    : ["원문 그대로", "자동 정리가 어려운 표라 원문 발췌를 그대로 보여드립니다"];
  return <span title={title} className="rounded border border-line text-ink-2 text-[10.5px] px-1.5 py-0.5">{label}</span>;
}

function OneTable({ t }: { t: FinTable }) {
  const src = [t.corp_name, t.report || (t.fiscal_year ? `${t.fiscal_year}년` : "")]
    .filter(Boolean).join(" · ");
  return (
    <div className="rounded-xl border border-line bg-card p-3.5 mb-2.5">
      <div className="flex flex-wrap items-center gap-1.5 mb-1.5 text-[13px]">
        <b>{t.title || "재무 수치"}</b>
        {t.unit && <span className="text-ink-2 text-xs">(단위: {t.unit})</span>}
        <Badge t={t} />
      </div>
      {src && <div className="text-xs text-ink-2 mb-2">📄 출처: <b>{src}</b></div>}
      {t.grid ? (
        <div className="overflow-x-auto">
          <table className="w-full text-[12.5px] border-collapse">
            {!!t.grid.columns?.length && (
              <thead>
                <tr>
                  {t.grid.columns.map((c, i) => (
                    // 숫자 열은 머리글도 우측 정렬 — 좌측 머리글 + 우측 숫자 조합이
                    // 넓은 열에서 값을 옆 열로 오독하게 만들던 문제(2026-07) 해소
                    <th key={i} className={`border-b-2 border-line px-2 py-1.5 bg-bg font-semibold align-bottom
                      ${i ? "border-l border-line/50" : ""}
                      ${numCol(t.grid!, i) ? "text-right" : "text-left"}
                      ${(c || "").length > 22 ? "whitespace-normal max-w-[15rem]" : "whitespace-nowrap"}`}>{c}</th>
                  ))}
                </tr>
              </thead>
            )}
            <tbody>
              {(t.grid.rows || []).map((r, ri) => (
                <tr key={ri} className={r.hl ? "bg-amber-50" : ""}>
                  {(r.cells || []).map((c, ci) => (
                    <td key={ci} className={`border-b border-line/60 px-2 py-1 ${ci ? "border-l border-line/50" : ""}
                      ${isNumCell(c) || (!c.trim() && numCol(t.grid!, ci)) ? "text-right tabular-nums" : ""}`}>
                      {c.trim() || (numCol(t.grid!, ci) ? <span className="text-ink-2/40">–</span> : "")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : t.lines?.length ? (
        <div className="space-y-1">
          {t.lines.map((l, li) => (
            <div key={li} className={`flex flex-wrap gap-x-3 gap-y-0.5 text-[12.5px] rounded px-2 py-1 ${l.hl ? "bg-amber-50" : ""}`}>
              <span className="font-semibold min-w-32">{l.label}</span>
              <span className="flex flex-wrap gap-x-3">
                {(l.pairs || []).map((p, pi) => (
                  <span key={pi}>
                    {p.col && <span className="text-ink-2 mr-1">{p.col}</span>}
                    <span className={isNumCell(p.val) ? "tabular-nums" : ""}>{p.val}</span>
                  </span>
                ))}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <pre className="text-[12px] whitespace-pre-wrap bg-bg rounded p-2 overflow-x-auto">{t.raw || ""}</pre>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-ink-2">
        <span className="truncate max-w-[60%]">{t.section_path || ""}</span>
        {t.dart_url && (
          <a href={t.dart_url} target="_blank" rel="noopener noreferrer"
            className="text-green-deep font-semibold hover:underline">DART 원문 열기 ↗</a>
        )}
      </div>
    </div>
  );
}

export function FinTables({ tables }: { tables: FinTable[] }) {
  if (!tables?.length) return null;
  return (
    <div className="mb-3">
      <div className="text-[13px] font-bold text-green-deep mb-1.5">
        📊 근거 재무 표 <span className="font-normal text-ink-2 text-xs">— 노란 줄이 질문과 관련된 항목입니다</span>
      </div>
      {tables.map((t, i) => <OneTable key={i} t={t} />)}
    </div>
  );
}
