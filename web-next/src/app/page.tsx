"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, setPw } from "@/lib/api";
import type { Meta, Seed, Thread } from "@/lib/types";
import { SearchView } from "@/components/SearchView";
import { ChatView } from "@/components/ChatView";

export default function Home() {
  const [view, setView] = useState<"search" | "chat">("search");
  const [meta, setMeta] = useState<Meta>({});
  const [locked, setLocked] = useState(false);
  const [pwInput, setPwInput] = useState("");
  const [pwErr, setPwErr] = useState(false);
  const [gen, setGen] = useState<string | null>(null);
  const [toast, setToast] = useState<React.ReactNode>(null);
  const [fbOpen, setFbOpen] = useState(false);
  const [fbText, setFbText] = useState("");
  const [fbContact, setFbContact] = useState("");
  const [fbErr, setFbErr] = useState("");
  const [fbSending, setFbSending] = useState(false);
  const [bootThread, setBootThread] = useState<Thread | null>(null);
  const toastT = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((html: React.ReactNode) => {
    setToast(html);
    if (toastT.current) clearTimeout(toastT.current);
    toastT.current = setTimeout(() => setToast(null), 5000);
  }, []);

  // 초기화: 메타 로드 + 인증 게이트(기존 initAuth와 동일 규칙)
  useEffect(() => {
    api.meta().then(setMeta);
    (async () => {
      const req = await api.authRequired();
      if (req === false) return;                        // 서버가 '비번 미설정' 명시 → 개방
      const local = ["localhost", "127.0.0.1"].includes(location.hostname);
      if (req === undefined && local) return;           // 판단 불가 + 로컬 → 개방
      const saved = sessionStorage.getItem("al_pw") || "";
      if (saved && (await api.login(saved))) return;
      setLocked(true);                                  // 그 외 → 잠금(안전)
    })();
  }, []);

  const tryLogin = async () => {
    setPwErr(false);
    if (await api.login(pwInput)) {
      setPw(pwInput); setLocked(false); setPwInput("");
    } else setPwErr(true);
  };

  const sendFeedback = async () => {
    setFbErr("");
    if (fbText.trim().length < 5) { setFbErr("내용을 5자 이상 입력해 주세요."); return; }
    setFbSending(true);
    try {
      await api.feedback(fbText.trim(), fbContact.trim());
      setFbOpen(false); setFbText(""); setFbContact("");
      showToast("🙏 소중한 의견 감사합니다! 서비스 개선에 반영하겠습니다.");
    } catch (e) { setFbErr("전송 실패: " + (e as Error).message); }
    setFbSending(false);
  };

  // 브리지: 검색 결과 시드 → 새 스레드 생성 → 대화 탭 전환
  const onBridge = async (seed: Seed) => {
    try {
      const j = await api.createThread(seed);
      setBootThread(j);
      setView("chat");
      showToast(<>검색 결과의 맥락을 물려받았습니다 — <b>기억 중</b> 칩을 확인하고 이어서 질문하세요</>);
    } catch (e) { showToast("대화 시작에 실패했습니다: " + (e as Error).message); }
  };

  const tab = (v: "search" | "chat", label: string) => (
    <button onClick={() => setView(v)}
      className={`px-4 py-1.5 rounded-lg text-[13.5px] font-semibold transition-colors
        ${view === v ? "bg-green text-white" : "text-ink-2 hover:bg-bg"}`}>
      {label}
    </button>
  );

  return (
    <div className="min-h-screen flex flex-col">
      {/* 헤더 */}
      <header className="sticky top-0 z-30 bg-white/95 backdrop-blur border-b border-line">
        <div className="w-full px-4 sm:px-6 lg:px-10 2xl:px-16 mx-auto px-4 h-14 flex items-center gap-3">
          <a href="/" title="감사렌즈 홈으로" className="flex items-center gap-2 text-ink no-underline">
            <span className="w-7 h-7 rounded-lg bg-green text-white grid place-items-center font-black text-sm">監</span>
            <span className="font-extrabold text-[17px]">감사렌즈</span>
            <span className="text-[11px] text-ink-2 hidden sm:inline">코스피 감사보고서 AI 분석</span>
          </a>
          <nav className="ml-auto flex gap-1 bg-bg rounded-xl p-1 border border-line">
            {tab("search", "분석 검색")}
            {tab("chat", "AI 대화")}
          </nav>
        </div>
        {/* 생성 진행 배너 */}
        {gen && (
          <div className="bg-green text-white text-[12.5px] px-4 py-1.5 flex items-center gap-2">
            <span className="spin !border-white/40 !border-t-white !w-3.5 !h-3.5" />
            <span className="w-full px-4 sm:px-6 lg:px-10 2xl:px-16 mx-auto w-full">{gen}</span>
          </div>
        )}
      </header>

      {/* 본문 */}
      <main className="flex-1 w-full w-full px-4 sm:px-6 lg:px-10 2xl:px-16 mx-auto px-4 py-5">
        <div hidden={view !== "search"}>
          <SearchView meta={meta} onGen={setGen} onToast={showToast}
            onLock={() => setLocked(true)} onBridge={onBridge} />
        </div>
        <div hidden={view !== "chat"}>
          <ChatView boot={bootThread} onGen={setGen} onToast={showToast}
            onLock={() => setLocked(true)} />
        </div>
      </main>

      <footer className="border-t border-line text-center text-[11.5px] text-ink-2 py-4">
        감사렌즈 · 출처: OpenDART · KRX · 분석: Claude · 본 도구는 회계사의 판단을 보조하며 최종 확인 책임은 사용자에게 있습니다.
      </footer>

      {/* 개선 요청 버튼 + 모달 */}
      <button onClick={() => setFbOpen(true)} aria-label="개선 요청 보내기"
        className="fixed bottom-5 right-5 z-30 rounded-full bg-green text-white shadow-lg px-4 py-2.5 text-[13px] font-bold hover:bg-green-deep">
        💡 개선 요청
      </button>
      {fbOpen && (
        <div role="dialog" aria-modal="true" onClick={(e) => { if (e.target === e.currentTarget) setFbOpen(false); }}
          className="fixed inset-0 z-40 bg-ink/40 grid place-items-center p-4">
          <div className="w-full max-w-md rounded-2xl bg-white p-5 shadow-2xl">
            <h2 className="font-bold text-[16px] mb-1">💡 서비스 개선 요청</h2>
            <p className="text-xs text-ink-2 mb-3">
              불편했던 점, 있었으면 하는 기능, 잘못된 답변 사례 등 무엇이든 적어주세요.<br />
              보내주신 의견은 저장되어 서비스 개선에 직접 반영됩니다.
            </p>
            <textarea value={fbText} onChange={(e) => setFbText(e.target.value)} maxLength={2000} rows={4}
              placeholder="예) 특정 기업 검색이 잘 안 돼요 / 표를 엑셀로 내려받고 싶어요 / ○○ 질문에 잘못된 답이 나왔어요"
              className="w-full rounded-lg border border-line p-2.5 text-[13px] outline-none focus:border-green" />
            <input value={fbContact} onChange={(e) => setFbContact(e.target.value)} maxLength={200}
              placeholder="(선택) 회신 받을 이메일·연락처"
              className="w-full mt-2 rounded-lg border border-line p-2.5 text-[13px] outline-none focus:border-green" />
            {fbErr && <div className="text-danger text-xs mt-2">{fbErr}</div>}
            <div className="flex justify-end gap-2 mt-3">
              <button onClick={() => setFbOpen(false)}
                className="rounded-lg border border-line px-4 py-2 text-[13px] hover:bg-bg">닫기</button>
              <button onClick={sendFeedback} disabled={fbSending}
                className="rounded-lg bg-green text-white px-4 py-2 text-[13px] font-bold hover:bg-green-deep disabled:opacity-50">
                {fbSending ? "보내는 중…" : "의견 보내기"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 로그인 게이트 */}
      {locked && (
        <div className="fixed inset-0 z-50 bg-ink/60 backdrop-blur-sm grid place-items-center p-4">
          <div className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-2xl text-center">
            <div className="w-11 h-11 mx-auto rounded-xl bg-green text-white grid place-items-center font-black text-lg mb-2">監</div>
            <h1 className="font-extrabold text-[17px]">감사렌즈</h1>
            <p className="text-xs text-ink-2 mt-1 mb-4">접근 비밀번호를 입력해 주세요</p>
            <input type="password" value={pwInput} onChange={(e) => setPwInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") tryLogin(); }} autoFocus
              className="w-full rounded-lg border border-line p-2.5 text-center outline-none focus:border-green" />
            {pwErr && <div className="text-danger text-xs mt-2">비밀번호가 올바르지 않습니다</div>}
            <button onClick={tryLogin}
              className="w-full mt-3 rounded-lg bg-green text-white py-2.5 font-bold hover:bg-green-deep">입장</button>
          </div>
        </div>
      )}

      {/* 토스트 */}
      {toast && (
        <div className="fixed bottom-20 left-1/2 -translate-x-1/2 z-50 rounded-xl bg-ink text-white text-[13px] px-4 py-2.5 shadow-xl max-w-[90vw]">
          {toast}
        </div>
      )}
    </div>
  );
}
