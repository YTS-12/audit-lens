"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, AuthError } from "@/lib/api";
import type { EvidenceItem, FactPreset, FinTable, Meta, Seed, StreamEvent, Understanding } from "@/lib/types";
import { AnswerBox, Empty, InterpPanel, Spinner } from "./bits";
import { EvidenceCard } from "./EvidenceCard";
import { FinTables } from "./FinTables";

interface Stage {
  understanding?: Understanding;
  answer?: string;
  analysis?: string;
  items: EvidenceItem[];
  tables: FinTable[];
  verified: number;
  insufficient?: boolean;
  progress?: string;
  error?: string;
  bridgeReady?: boolean;
}

// 감사인 실무 관점 예시 — 위험평가·감사계획·지정제·품질/독립성 지표를 한 번씩 체험
const EXAMPLES = [
  "계속기업 불확실성이 있는 기업 전부",
  "조선사들의 핵심감사사항 알려줘",
  "주기적 지정으로 감사인이 변경된 회사는?",
  "삼성전자 감사 투입시간과 보수 알려줘",
];
const GROUP_ORDER = ["의견·감사인", "위험 신호", "정책·추정", "거래·독립성"];

export function SearchView({ meta, onGen, onToast, onLock, onBridge }: {
  meta: Meta;
  onGen: (msg: string | null) => void;
  onToast: (html: React.ReactNode) => void;
  onLock: () => void;
  onBridge: (seed: Seed) => Promise<void>;
}) {
  const [q, setQ] = useState("");
  const [ind, setInd] = useState({ cls1: "", cls2: "" });
  const [showSec, setShowSec] = useState(false);   // 부수 사업 기업 섹션 펼침 여부
  const [years, setYears] = useState<string[]>([]);   // 복수선택: "2023"/"2024"/"2025"/"검토"
  const filtersOpen = true;                    // 상세 조건 상시 펼침(사용자 요청으로 접이식 해제)
  const [stage, setStage] = useState<Stage | null>(null);
  const [bridging, setBridging] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const seedRef = useRef<Seed | null>(null);
  const lastQRef = useRef("");
  const lastPathRef = useRef("");

  useEffect(() => () => abortRef.current?.abort(), []);

  const factYears = (meta.fact_years || ["2024", "2025"]).map(String);
  const numYears = years.filter((y) => y !== "검토");
  const noFact = numYears.length > 0 && !numYears.some((y) => factYears.includes(y));

  const run = useCallback(async (preset?: FactPreset) => {
    const question = (preset ? preset.question : q).trim();
    if (!question) return;
    lastQRef.current = question;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    seedRef.current = null;
    setShowSec(false);
    onGen("질문 이해하는 중…");
    setStage({ items: [], tables: [], verified: 0, progress: "질문 이해하는 중…" });

    const payload: Record<string, unknown> = { question };
    if (ind.cls1) payload.ind_cls1 = ind.cls1;
    if (ind.cls2) payload.ind_cls2 = ind.cls2;
    const ny = years.filter((y) => y !== "검토");
    if (ny.length) payload.years = ny;
    if (years.includes("검토")) payload.report = "검토";
    if (preset) { payload.fact_types = preset.fact_types; payload.exhaustive = true; }

    try {
      for await (const ev of api.stream("/api/query/stream", payload, ac.signal)) {
        if (ac.signal.aborted) return;
        handleEvent(ev);
      }
      if (abortRef.current === ac) { abortRef.current = null; onGen(null); }
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      if (abortRef.current === ac) abortRef.current = null;
      onGen(null);
      if (e instanceof AuthError) { onLock(); return; }
      setStage({ items: [], tables: [], verified: 0, error: (e as Error).message });
    }

    function handleEvent(ev: StreamEvent) {
      if (ev.type === "understanding") {
        onGen("근거 찾는 중…");
        setStage((s) => ({ ...(s || { items: [], tables: [], verified: 0 }),
          understanding: ev.understanding, progress: "정리 데이터 조회 중…" }));
      } else if (ev.type === "core") {
        if (ev.path) lastPathRef.current = ev.path;
        seedRef.current = {
          question, understanding: ev.understanding || {}, answer: ev.answer || "",
          analysis: ev.analysis || "", items: [...(ev.items || [])], tables: [...(ev.tables || [])],
          path: ev.path || "", verified_count: ev.verified_count || 0, insufficient: !!ev.insufficient,
        };
        onGen("본문에서 근거 보완 중…");
        setStage((s) => ({ ...(s || { items: [], tables: [], verified: 0 }),
          understanding: ev.understanding || s?.understanding,
          answer: ev.answer, analysis: ev.analysis,
          items: ev.items || [], tables: ev.tables || [],
          verified: ev.verified_count || 0, insufficient: !!ev.insufficient,
          progress: "본문에서 더 찾는 중…" }));
      } else if (ev.type === "progress") {
        onGen(ev.msg || "찾는 중…");
        setStage((s) => (s ? { ...s, progress: ev.msg || "찾는 중…" } : s));
      } else if (ev.type === "supplement") {
        if (seedRef.current) {
          seedRef.current.items = seedRef.current.items.concat(ev.items || []);
          seedRef.current.tables = seedRef.current.tables.concat(ev.tables || []);
          seedRef.current.verified_count += ev.verified_count || 0;
          if (ev.analysis && !seedRef.current.analysis) seedRef.current.analysis = ev.analysis;
        }
        setStage((s) => s ? {
          ...s,
          analysis: s.analysis || ev.analysis,
          tables: s.tables.length ? s.tables : (ev.tables || []),
          items: s.items.concat(ev.items || []),
          verified: s.verified + (ev.verified_count || 0),
        } : s);
      } else if (ev.type === "done") {
        onGen(null);
        setStage((s) => s ? { ...s, progress: undefined, bridgeReady: !!seedRef.current } : s);
      } else if (ev.type === "error") {
        onGen(null);
        setStage({ items: [], tables: [], verified: 0, error: ev.msg || "오류" });
      }
    }
  }, [q, ind, years, onGen, onLock]);

  const bridge = async () => {
    if (!seedRef.current || bridging) return;
    setBridging(true);
    try { await onBridge(seedRef.current); }
    finally { setBridging(false); }
  };

  const indList = meta.industry?.cls1 || [];
  const selCls = "rounded-lg border border-line bg-white px-2 py-1.5 text-[12.5px] " +
    "w-full sm:w-auto sm:max-w-48 min-w-0";

  // 활성 조건 라벨(요약 배지용)
  const indLabel = [
    indList.find((c) => c.code === ind.cls1)?.name,
    indList.flatMap((c) => c.sub).find((s) => s.code === ind.cls2)?.name,
  ].filter(Boolean).pop();

  // 프리셋 그룹핑(서버 group 필드, 없으면 기타)
  const presetGroups = GROUP_ORDER
    .map((g) => ({ g, items: (meta.fact_presets || []).filter((p) => p.group === g) }))
    .filter((x) => x.items.length);
  const ungrouped = (meta.fact_presets || []).filter((p) => !p.group || !GROUP_ORDER.includes(p.group));

  return (
    <div>
      {/* ① 검색창(히어로) — 자연어 질문이 주 동선 */}
      <div className="flex gap-2 mb-2">
        <input value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") run(); }}
          placeholder="자연어로 질문하세요 — 기업·업종·계정과목·리스크 무엇이든"
          className="flex-1 rounded-2xl border-2 border-line bg-white px-5 py-3.5 text-[15px] outline-none focus:border-green shadow-sm" />
        <button onClick={() => run()}
          className="rounded-2xl bg-green text-white font-bold px-7 hover:bg-green-deep transition-colors">
          검색
        </button>
      </div>

      {/* 실행형 예시 — 첫 방문자가 클릭 한 번으로 능력을 체험 */}
      {!stage && (
        <div className="flex flex-wrap items-center gap-1.5 mb-3 text-[12px]">
          <span className="text-ink-2">이런 질문을 해보세요 →</span>
          {EXAMPLES.map((ex) => (
            <button key={ex} onClick={() => setQ(ex)}
              className="rounded-full border border-line bg-white px-3 py-1 text-ink-2 hover:border-green hover:text-green-deep">
              {ex}
            </button>
          ))}
        </div>
      )}

      {/* ② 상세 조건(접이식) — 선택된 조건은 요약 배지로 항상 보임 */}
      <div className="rounded-xl border border-line bg-white mb-3">
        <div className="w-full flex flex-wrap items-center gap-2 px-4 py-2.5 text-[12.5px]">
          <span className="font-semibold text-ink">상세 조건</span>
          <span className="text-ink-2 hidden sm:inline">업종·연도로 좁히기</span>
          {indLabel && (
            <span className="rounded-full bg-green-soft text-green-deep px-2.5 py-0.5 inline-flex items-center gap-1.5">
              업종: {indLabel}
              <i role="button" aria-label="업종 조건 해제" className="not-italic font-bold hover:text-danger"
                onClick={(e) => { e.stopPropagation(); setInd({ cls1: "", cls2: "" }); }}>×</i>
            </span>
          )}
          {years.length > 0 && (
            <span className="rounded-full bg-green-soft text-green-deep px-2.5 py-0.5 inline-flex items-center gap-1.5">
              {years.map((y) => (y === "검토" ? "분·반기" : `${y}년`)).join(" · ")}
              <i role="button" aria-label="연도 조건 해제" className="not-italic font-bold hover:text-danger"
                onClick={(e) => { e.stopPropagation(); setYears([]); }}>×</i>
            </span>
          )}
          {!indLabel && years.length === 0 && <span className="text-ink-2/70">현재: 전체 업종 · 전체 연도</span>}
        </div>
        {filtersOpen && (
          <div className="px-4 pb-3.5 border-t border-line/60 pt-3 space-y-2.5">
            {/* 업종(감사렌즈 자체 2단: 분류1 11 · 분류2 59 — 감사쟁점 유사성 기준) */}
            <div className="flex flex-wrap items-center gap-2 text-[12.5px]">
              <span className="text-ink-2 font-semibold w-full sm:w-auto">업종(자체 분류)</span>
              <select className={selCls} value={ind.cls1}
                onChange={(e) => setInd({ cls1: e.target.value, cls2: "" })}
                title={indList.find((c) => c.code === ind.cls1)?.audit_focus || ""}>
                <option value="">분류1 전체</option>
                {indList.map((c) => <option key={c.code} value={c.code} title={c.audit_focus}>{c.name}</option>)}
              </select>
              <select className={selCls} value={ind.cls2}
                onChange={(e) => {
                  const v = e.target.value;
                  const c1 = indList.find((c) => c.sub.some((s) => s.code === v))?.code || "";
                  setInd(v ? { cls1: c1, cls2: v } : { ...ind, cls2: "" });
                }}>
                <option value="">분류2 전체</option>
                {indList.map((c) => (
                  <optgroup key={c.code} label={c.name}>
                    {c.sub.map((s) => <option key={s.code} value={s.code}>{s.name}</option>)}
                  </optgroup>
                ))}
              </select>
              <span className="text-ink-2 text-[11px]">
                {meta.industry?.source || "감사렌즈 자체 산업분류"}
                {meta.industry?.version ? ` ${meta.industry.version}` : ""}
              </span>
            </div>
            {/* 연도·보고서 */}
            <div className="flex flex-wrap items-center gap-2 text-[12.5px]">
              <span className="text-ink-2 font-semibold">연도·보고서</span>
              <span className="inline-flex rounded-lg border border-line overflow-hidden">
                {["", ...factYears, "2023", "검토"].filter((v, i, a) => a.indexOf(v) === i).map((y) => {
                  const active = y === "" ? years.length === 0 : years.includes(y);
                  return (
                    <button key={y || "all"}
                      onClick={() => setYears(y === "" ? []
                        : years.includes(y) ? years.filter((x) => x !== y) : [...years, y])}
                      className={`px-2.5 py-1 text-[12px] border-r border-line last:border-r-0
                        ${active ? "bg-green text-white" : "bg-white text-ink-2 hover:bg-bg"}`}>
                      {y === "" ? "전체" : y === "검토" ? "분·반기" : `${y}${factYears.includes(y) ? " ⚡" : ""}`}
                    </button>
                  );
                })}
              </span>
              <span className="text-[11px] text-ink-2">복수 선택 가능 · ⚡ = 사전 정리 데이터 보유(전수 조회 지원)</span>
            </div>
          </div>
        )}
      </div>

      {/* ③ 원클릭 전수 조회 — Fact Store 프리셋(그룹) */}
      <div className="rounded-xl border border-green/25 bg-green-soft/40 px-4 py-3 mb-4">
        <div className="flex flex-wrap items-baseline gap-2 mb-2">
          <span className="text-[13px] font-bold text-green-deep">⚡ 원클릭 전수 조회</span>
          <span className="text-[11.5px] text-ink-2">
            사전 정리 데이터로 전 종목을 즉시 훑습니다 (누락·환각 없음, {factYears.join("·")} 연차 기준)
          </span>
        </div>
        {noFact && (
          <div className="text-[11.5px] text-warn font-semibold mb-2">
            ⚠ 선택한 {numYears.join("·")}년은 정리 데이터가 없어 아래 키워드도 원문 검색으로 답변됩니다.
          </div>
        )}
        <div className="flex flex-wrap items-center gap-1.5">
          {[...presetGroups.flatMap((x) => x.items), ...ungrouped].map((p) => (
            <button key={p.label} title={`"${p.question}" 전수 조회`} onClick={() => run(p)}
              className="rounded-full border border-green/40 bg-white text-green-deep text-[12px] px-3 py-1 hover:bg-green-soft">
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* 결과 스테이지 */}
      {!stage && (
        <Empty big="코스피 전 종목 감사보고서를 근거와 함께 검색합니다"
          sub={<>위 예시를 눌러보거나, 자연어로 질문하세요.<br />모든 답변은 공시 원문 인용과 DART 딥링크를 함께 제공합니다.</>} />
      )}
      {stage?.error && <Empty big="오류" sub={stage.error} />}
      {stage && !stage.error && (
        <div>
          {stage.understanding && <InterpPanel u={stage.understanding} />}
          {stage.bridgeReady && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border-2 border-green bg-green-soft/50 px-4 py-2.5 mb-3">
              <span className="text-[12.5px] text-ink-2">
                이 결과를 기억한 채 후속 질문을 이어갈 수 있어요 — 회사·계정을 다시 쓰지 않아도 됩니다
              </span>
              <button onClick={bridge} disabled={bridging}
                className="shrink-0 rounded-lg bg-green text-white font-bold text-[13px] px-4 py-2 hover:bg-green-deep disabled:opacity-50">
                {bridging ? "대화 준비 중…" : "💬 이 결과로 대화 시작"}
              </button>
            </div>
          )}
          {stage.answer !== undefined && (
            <AnswerBox answer={stage.answer} analysis={stage.analysis}
              verified={stage.verified} total={stage.items.length} insufficient={stage.insufficient} />
          )}
          <FinTables tables={stage.tables} />
          {(() => {
            // ── 업종 필터 활성 시 결과를 '핵심 사업'과 '부수 사업'으로 계층화 ──
            // 다중 라벨 기업(예: 포장재를 부수 사업으로 하는 풍산홀딩스)이 첫 화면에
            // 섞이면 분류가 틀린 것처럼 보인다. 핵심만 기본 노출하고 나머지는 펼침으로.
            const selCode = ind.cls2 || ind.cls1 || (stage.understanding?._ind_auto?.code ?? "");
            const selKeys = selCode.split("+").filter(Boolean);
            const tierOf = (it: EvidenceItem) => {
              const m = (it.industry?.labels || []).find((l) =>
                selKeys.some((k) => (k.length === 1 ? l.code?.[0] === k : l.code === k)));
              return m?.materiality;
            };
            const grouping = selKeys.length > 0
              && stage.items.some((it) => (it.industry?.labels || []).length > 0);
            const coreItems = grouping ? stage.items.filter((it) => tierOf(it) === "핵심") : stage.items;
            const secItems = grouping ? stage.items.filter((it) => tierOf(it) !== "핵심") : [];
            const secOpen = showSec || coreItems.length === 0;   // 핵심이 없으면 자동 펼침
            const indName = indLabel || stage.understanding?._ind_auto?.label || "선택 업종";
            // 카드는 기업×연도 단위이므로, 요약 카운트는 기업 수로 센다
            const nCore = new Set(coreItems.map((x) => x.corp_name)).size;
            const nSec = new Set(secItems.map((x) => x.corp_name)).size;
            const card = (it: EvidenceItem, i: number) => (
              <EvidenceCard key={`${it.corp_code}-${i}`} it={it}
                lastQ={lastQRef.current} lastPath={lastPathRef.current} onToast={onToast}
                selCode={selCode} />
            );
            return (
              <>
                {grouping && secItems.length > 0 && (
                  <div className="text-[12.5px] text-ink-2 mb-2">
                    <b className="text-green-deep">&lsquo;{indName}&rsquo;</b> 핵심 사업 기업{" "}
                    <b className="text-ink">{nCore}개사</b>
                    <span className="mx-1.5 text-line">|</span>
                    부수 사업 영위 기업 {nSec}개사
                  </div>
                )}
                <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3 items-start">
                  {coreItems.map(card)}
                  {!stage.items.length && !stage.progress && (
                    <Empty big="조건에 맞는 결과를 찾지 못했습니다" />
                  )}
                </div>
                {grouping && secItems.length > 0 && !secOpen && (
                  <button onClick={() => setShowSec(true)}
                    className="w-full mt-3 rounded-xl border border-dashed border-line bg-white py-2.5
                               text-[12.5px] text-ink-2 hover:border-green hover:text-green-deep">
                    ▼ &lsquo;{indName}&rsquo;를 부수 사업으로 영위하는 기업 {nSec}개사 보기
                  </button>
                )}
                {grouping && secItems.length > 0 && secOpen && (
                  <>
                    <div className="flex items-center gap-2.5 mt-4 mb-2 text-[12px] text-ink-2">
                      <span className="h-px flex-1 bg-line" />
                      &lsquo;{indName}&rsquo; 부수 사업 영위 기업 · {nSec}개사
                      <span className="h-px flex-1 bg-line" />
                    </div>
                    <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3 items-start">
                      {secItems.map(card)}
                    </div>
                  </>
                )}
              </>
            );
          })()}
          {stage.progress && (
            <div className="py-4 px-2"><Spinner label={stage.progress} /></div>
          )}
        </div>
      )}
    </div>
  );
}
