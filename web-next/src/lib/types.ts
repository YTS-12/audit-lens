/** 백엔드(FastAPI) 응답 계약 — web/index.html과 동일한 이벤트·필드 구조 */

export interface Understanding {
  intent?: string;
  industry?: string;
  company?: string;
  is_consolidated?: boolean | null;
  fiscal_years?: (string | number)[];
  doc_types?: string[];
  expanded_queries?: string[];
  _ind_auto?: { label: string; code: string; n: number };
  [k: string]: unknown;
}

/** 자체 산업분류 라벨(다중) — 중요도(핵심/보조/노출)·산업관계·수행법인·근거 포함 */
export interface IndustryLabel {
  code?: string; cls1?: string; cls2?: string;
  materiality?: string; rel?: string; entity?: string; basis?: string;
}
export interface IndustryBrief {
  labels?: IndustryLabel[];
  core?: string;            // 핵심 분류2 이름 나열("반도체 · 완제 전자·가전")
  source?: string;          // "감사렌즈 자체 분류 v11"
}

export interface EvidenceItem {
  corp_code?: string;
  corp_name?: string;
  fiscal_year?: string | number;
  is_consolidated?: boolean | null;
  conclusion?: string;
  quote?: string;
  context?: string;
  section_path?: string;
  dart_url?: string;
  doc_type?: string;
  verified?: boolean;
  source?: string;          // 'ondemand' = 본문 추가 검색 배지
  industry?: IndustryBrief;
  /** 표 청크 인용의 표시용 텍스트(서버가 직렬화 메타로 결정적 생성).
      검증·복사·DART 검색은 원본 quote/context 기준을 유지한다. */
  quote_display?: string;
  context_display?: string;
  [k: string]: unknown;
}

export interface FinTableLinePair { col?: string; val?: string }
export interface FinTableLine { label?: string; hl?: boolean; pairs?: FinTableLinePair[] }
export interface FinTable {
  title?: string; unit?: string; src?: string;
  corp_name?: string; fiscal_year?: string | number; report?: string;
  section_path?: string; dart_url?: string; doc_type?: string;
  grid?: { columns?: string[]; rows?: { cells?: string[]; hl?: boolean }[] };
  lines?: FinTableLine[];
  raw?: string;
}

/** NDJSON 스트림 이벤트(검색·대화 공용 + 대화 전용 chat_*) */
export interface StreamEvent {
  type: "understanding" | "core" | "progress" | "supplement" | "done" | "error"
      | "chat_meta" | "chat_done";
  understanding?: Understanding;
  answer?: string;
  analysis?: string;
  items?: EvidenceItem[];
  tables?: FinTable[];
  path?: string;
  verified_count?: number;
  insufficient?: boolean;
  msg?: string;
  added?: number;
  // chat_meta
  thread_id?: number;
  used_context?: boolean;
  context_summary?: string;
  resolved?: string;
  // chat_done
  state?: Record<string, unknown>;
  chips?: MemChip[];
  suggestions?: string[];
  summary?: string;
}

export interface MemChip { k: string; label: string }

export interface FactPreset { label: string; question: string; fact_types?: string[]; group?: string }

export interface IndustryTaxonomy {
  as_of?: string; source?: string; version?: string;
  cls1: { code: string; name: string; audit_focus?: string; sub: { code: string; name: string }[] }[];
}

export interface Meta {
  sectors?: string[];
  fact_years?: string[];
  fact_presets?: FactPreset[];
  industry?: IndustryTaxonomy;
}

export interface ChatMessage {
  question: string;
  resolved?: string;
  summary?: string;
  payload?: {
    answer?: string; analysis?: string; items?: EvidenceItem[]; tables?: FinTable[];
    verified_count?: number; insufficient?: boolean;
    used_context?: boolean; context_summary?: string;
  };
}

export interface Thread {
  id: number; title?: string; n?: number;
  state?: Record<string, unknown>; chips?: MemChip[];
  messages?: ChatMessage[]; suggestions?: string[];
}

/** 검색 → 대화 브리지 시드 */
export interface Seed {
  question: string;
  understanding: Understanding;
  answer: string;
  analysis: string;
  items: EvidenceItem[];
  tables: FinTable[];
  path: string;
  verified_count: number;
  insufficient: boolean;
}
