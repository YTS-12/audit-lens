"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, AuthError } from "@/lib/api";
import type { EvidenceItem, FinTable, MemChip, StreamEvent, Thread } from "@/lib/types";
import { AnswerBox, Empty, Spinner } from "./bits";
import { EvidenceCard } from "./EvidenceCard";
import { FinTables } from "./FinTables";

interface Turn {
  q: string;
  resolved?: string;
  contextSummary?: string;
  usedContext?: boolean;
  summary?: string;          // 접힘 한 줄 요약
  answer?: string;
  analysis?: string;
  items: EvidenceItem[];
  tables: FinTable[];
  verified: number;
  insufficient?: boolean;
  progress?: string;
  error?: string;
  folded: boolean;
}

export function ChatView({ boot, onGen, onToast, onLock }: {
  boot: Thread | null;                    // 브리지로 생성된 스레드(검색→대화 인계)
  onGen: (msg: string | null) => void;
  onToast: (html: React.ReactNode) => void;
  onLock: () => void;
}) {
  const [cq, setCq] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [chips, setChips] = useState<MemChip[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [menu, setMenu] = useState<{ id: number; title?: string; n?: number }[] | null>(null);
  const tidRef = useRef<number | null>(null);
  const stateRef = useRef<Record<string, unknown>>({});
  const abortRef = useRef<AbortController | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const applyThread = useCallback((j: Thread) => {
    tidRef.current = j.id;
    stateRef.current = j.state || {};
    setChips(j.chips || []);
    setSuggestions(j.suggestions || []);
    const ms = j.messages || [];
    setTurns(ms.map((m, i) => ({
      q: m.question,
      resolved: m.resolved,
      usedContext: m.payload?.used_context,
      contextSummary: m.payload?.context_summary,
      summary: m.summary,
      answer: m.payload?.answer, analysis: m.payload?.analysis,
      items: m.payload?.items || [], tables: m.payload?.tables || [],
      verified: m.payload?.verified_count || 0, insufficient: m.payload?.insufficient,
      folded: i < ms.length - 1,
    })));
  }, []);

  useEffect(() => { if (boot) applyThread(boot); }, [boot, applyThread]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [turns.length]);

  const patchLast = (fn: (t: Turn) => Turn) =>
    setTurns((ts) => ts.length ? [...ts.slice(0, -1), fn(ts[ts.length - 1])] : ts);

  const chatRun = useCallback(async (qArg?: string) => {
    const q = (qArg || cq).trim();
    if (!q) return;
    setCq("");
    setSuggestions([]);
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    onGen("질문 이해하는 중…");
    setTurns((ts) => [...ts.map((t) => ({ ...t, folded: true })),
      { q, items: [], tables: [], verified: 0, progress: "질문 이해하는 중…", folded: false }]);

    try {
      for await (const ev of api.stream("/api/chat/stream", { question: q, thread_id: tidRef.current }, ac.signal)) {
        if (ac.signal.aborted) return;
        handle(ev);
      }
      if (abortRef.current === ac) { abortRef.current = null; onGen(null); }
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      if (abortRef.current === ac) abortRef.current = null;
      onGen(null);
      if (e instanceof AuthError) { onLock(); return; }
      patchLast((t) => ({ ...t, progress: undefined, error: (e as Error).message }));
    }

    function handle(ev: StreamEvent) {
      if (ev.type === "chat_meta") {
        if (ev.thread_id != null) tidRef.current = ev.thread_id;
        if (ev.used_context)
          patchLast((t) => ({ ...t, usedContext: true, contextSummary: ev.context_summary, resolved: ev.resolved }));
      } else if (ev.type === "understanding") {
        onGen("근거 찾는 중…");
        patchLast((t) => ({ ...t, progress: "정리 데이터 조회 중…" }));
      } else if (ev.type === "core") {
        onGen("본문에서 근거 보완 중…");
        patchLast((t) => ({ ...t, answer: ev.answer, analysis: ev.analysis,
          items: ev.items || [], tables: ev.tables || [],
          verified: ev.verified_count || 0, insufficient: !!ev.insufficient,
          progress: "본문에서 더 찾는 중…" }));
      } else if (ev.type === "progress") {
        onGen(ev.msg || "찾는 중…");
        patchLast((t) => ({ ...t, progress: ev.msg || "찾는 중…" }));
      } else if (ev.type === "supplement") {
        patchLast((t) => ({ ...t,
          analysis: t.analysis || ev.analysis,
          tables: t.tables.length ? t.tables : (ev.tables || []),
          items: t.items.concat(ev.items || []),
          verified: t.verified + (ev.verified_count || 0) }));
      } else if (ev.type === "done") {
        patchLast((t) => ({ ...t, progress: undefined }));
      } else if (ev.type === "chat_done") {
        onGen(null);
        if (ev.thread_id != null) tidRef.current = ev.thread_id;
        stateRef.current = ev.state || {};
        setChips(ev.chips || []);
        setSuggestions(ev.suggestions || []);
        patchLast((t) => ({ ...t, summary: ev.summary }));
      } else if (ev.type === "error") {
        onGen(null);
        patchLast((t) => ({ ...t, progress: undefined, error: ev.msg || "오류" }));
      }
    }
  }, [cq, onGen, onLock]);

  const postState = async (st: Record<string, unknown>) => {
    if (tidRef.current == null) return;
    try {
      const j = await api.postState(tidRef.current, st);
      stateRef.current = j.state || {};
      setChips(j.chips || []);
      onToast("기억을 갱신했습니다");
    } catch { onToast("기억 갱신에 실패했습니다"); }
  };

  const newChat = () => {
    abortRef.current?.abort(); abortRef.current = null; onGen(null);
    tidRef.current = null; stateRef.current = {};
    setChips([]); setSuggestions([]); setTurns([]);
  };

  const openMenu = async () => {
    if (menu) { setMenu(null); return; }
    setMenu([]);
    try { setMenu(await api.threads()); }
    catch (e) { if (e instanceof AuthError) onLock(); setMenu(null); }
  };

  return (
    <div>
      <div className="flex items-center gap-2 mb-3 relative">
        <div className="text-[15px] font-bold">
          AI 대화 <span className="text-[10px] align-top bg-green text-white rounded px-1 py-0.5">BETA</span>
          <span className="ml-2 text-xs font-normal text-ink-2">
            대화 맥락을 기억합니다 — 후속 질문은 회사·계정을 다시 쓰지 않아도 돼요
          </span>
        </div>
        <div className="ml-auto flex gap-1.5">
          <button onClick={openMenu}
            className="rounded-lg border border-line bg-white px-3 py-1.5 text-[12.5px] hover:bg-bg">이전 대화 ▾</button>
          <button onClick={newChat}
            className="rounded-lg bg-green text-white px-3 py-1.5 text-[12.5px] font-semibold hover:bg-green-deep">+ 새 대화</button>
        </div>
        {menu !== null && (
          <div className="absolute right-0 top-10 z-20 w-72 max-h-80 overflow-y-auto rounded-xl border border-line bg-white shadow-lg p-1">
            {menu.length === 0 && <div className="p-3 text-xs text-ink-2">불러오는 중…</div>}
            {menu.map((t) => (
              <button key={t.id}
                onClick={async () => {
                  setMenu(null);
                  try { applyThread(await api.thread(t.id)); }
                  catch { onToast("대화를 불러오지 못했습니다"); }
                }}
                className="w-full text-left rounded-lg px-3 py-2 hover:bg-bg flex items-center gap-2">
                <span className="truncate text-[13px]">{t.title || "(제목 없음)"}</span>
                <span className="ml-auto text-[11px] text-ink-2 shrink-0">{t.n}문답</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 기억 중 칩 */}
      {chips.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 mb-3 rounded-xl border border-line bg-white px-3 py-2">
          <span className="text-[11px] font-bold text-green-deep">기억 중</span>
          {chips.map((c) => (
            <span key={c.k} className="inline-flex items-center gap-1 rounded-full bg-green-soft text-green-deep text-[12px] px-2.5 py-0.5">
              {c.label}
              <button title="이 항목 잊기" className="hover:text-danger"
                onClick={() => { const st = { ...stateRef.current }; delete st[c.k]; postState(st); }}>×</button>
            </span>
          ))}
          <button onClick={() => postState({})}
            className="ml-auto text-[11px] text-ink-2 hover:underline">비우기</button>
        </div>
      )}

      {/* 대화 로그 */}
      <div ref={logRef} className="space-y-3 mb-4">
        {turns.length === 0 && (
          <Empty big="이어지는 질문이 가능한 대화형 분석"
            sub={<>예: &ldquo;삼성전자 매출채권 알려줘&rdquo; → &ldquo;그럼 작년이랑 비교하면?&rdquo; → &ldquo;SK하이닉스는?&rdquo;<br />이전 답변은 한 줄로 접혀 최신 답변만 크게 보입니다.</>} />
        )}
        {turns.map((t, i) => t.folded ? (
          <button key={i} onClick={() => setTurns((ts) => ts.map((x, xi) => xi === i ? { ...x, folded: false } : x))}
            className="w-full text-left rounded-xl border border-line bg-white px-4 py-2.5 text-[13px] hover:bg-bg flex items-center gap-2">
            <span className="font-semibold truncate">{t.q}</span>
            {t.summary && <span className="text-ink-2 truncate">→ {t.summary}</span>}
            <span className="ml-auto text-green-deep shrink-0 text-xs">펼치기 ▾</span>
          </button>
        ) : (
          <div key={i}>
            <div className="flex justify-end mb-2">
              <div className="rounded-2xl rounded-br-sm bg-green text-white px-4 py-2 text-[14px] max-w-[80%]">{t.q}</div>
            </div>
            {t.usedContext && t.resolved && t.resolved !== t.q && (
              <div className="text-[12px] text-ink-2 mb-2">
                <span className="rounded bg-green-soft text-green-deep font-semibold px-1.5 py-0.5 mr-1.5">맥락 해석</span>
                {t.contextSummary && <span>{t.contextSummary} </span>}
                <b className="text-ink">→ {t.resolved}</b>
              </div>
            )}
            {t.error && <Empty big="오류" sub={t.error} />}
            {!t.error && t.answer !== undefined && (
              <AnswerBox answer={t.answer} analysis={t.analysis}
                verified={t.verified} total={t.items.length} insufficient={t.insufficient} />
            )}
            <FinTables tables={t.tables} />
            <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3 items-start">
              {t.items.map((it, ii) => (
                <EvidenceCard key={ii} it={it} lastQ={t.q} lastPath="" onToast={onToast} />
              ))}
            </div>
            {t.progress && <div className="py-3 px-1"><Spinner label={t.progress} /></div>}
            {i === turns.length - 1 && suggestions.length > 0 && !t.progress && (
              <div className="flex flex-wrap items-center gap-1.5 mt-3">
                <span className="text-[11px] font-bold text-ink-2">이어서</span>
                {suggestions.map((s) => (
                  <button key={s} onClick={() => chatRun(s)}
                    className="rounded-full border border-green/40 bg-green-soft/70 text-green-deep text-[12px] px-3 py-1 hover:bg-green-soft">
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* 입력 */}
      <div className="flex gap-2 sticky bottom-4">
        <input value={cq} onChange={(e) => setCq(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") chatRun(); }}
          placeholder="질문을 입력하세요 — 후속 질문은 회사·계정을 다시 안 써도 됩니다"
          className="flex-1 rounded-xl border border-line bg-white px-4 py-3 text-[14px] outline-none focus:border-green shadow-sm" />
        <button onClick={() => chatRun()}
          className="rounded-xl bg-green text-white font-bold px-6 hover:bg-green-deep">질문</button>
      </div>
    </div>
  );
}
