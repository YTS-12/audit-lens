"""KRX OpenAPI 클라이언트 (코스피 종목 명단).

실제 엔드포인트(2026-06 검증 완료):
  GET http://data-dbg.krx.co.kr/svc/apis/sto/stk_isu_base_info?basDd=YYYYMMDD
  헤더: AUTH_KEY: <발급키>
  응답: {"OutBlock_1":[{ISU_CD, ISU_SRT_CD, ISU_NM, ISU_ABBRV, ISU_ENG_NM, LIST_DD,
                       MKT_TP_NM(KOSPI/KOSDAQ/KONEX), SECUGRP_NM(주권/ETF/ETN…),
                       SECT_TP_NM(소속부; KOSPI는 대개 공란),
                       KIND_STKCERT_TP_NM(보통주/우선주), PARVAL, LIST_SHRS}, ...]}

⚠️ 이 '종목기본정보'에는 업종(산업) 필드가 없다(SECT_TP_NM은 KOSPI에서 공란).
   → 산업 라벨은 OpenDART induty_code(KSIC, company.json)로 보완한다(설계 §3.5.3, Phase 0 결정).

반환 표준 스키마(build_universe가 의존):
  {"stock_code":"000720", "name":"현대건설", "krx_sector":"",
   "market":"KOSPI", "secugrp":"주권", "kind":"보통주", "isu_cd":"KR7000720003"}
"""
from __future__ import annotations
from datetime import date, timedelta
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

_BASE = "http://data-dbg.krx.co.kr/svc/apis"
_SERVICE_KOSPI_BASE = "sto/stk_isu_base_info"   # 유가증권 종목기본정보
_FIELD_MAP = {                                  # KRX 응답필드 → 표준필드
    "stock_code": "ISU_SRT_CD",   # 단축코드(6자리)
    "name": "ISU_ABBRV",          # 종목약명
    "market": "MKT_TP_NM",        # 시장구분(KOSPI/KOSDAQ/KONEX)
    "krx_sector": "SECT_TP_NM",   # 소속부(업종 아님, 대개 공란)
    "secugrp": "SECUGRP_NM",      # 증권구분(주권/ETF/ETN…)
    "kind": "KIND_STKCERT_TP_NM", # 주식종류(보통주/우선주)
    "isu_cd": "ISU_CD",           # 표준코드(KR…)
}


class KrxClient:
    def __init__(self, api_key: str, timeout: int = 30, max_retry: int = 4):
        self.key = api_key
        self.timeout = timeout
        self.max_retry = max_retry
        self._s = requests.Session()

    def _get(self, service: str, **params) -> dict:
        @retry(stop=stop_after_attempt(self.max_retry),
               wait=wait_exponential(multiplier=1, max=20), reraise=True)
        def _call():
            headers = {"AUTH_KEY": self.key}
            r = self._s.get(f"{_BASE}/{service}", params=params,
                            headers=headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        return _call()

    def _rows(self, base_date: str) -> list[dict]:
        return self._get(_SERVICE_KOSPI_BASE, basDd=base_date).get("OutBlock_1") or []

    def kospi_listing(self, base_date: str, market: str = "KOSPI",
                      only_common: bool = True) -> list[dict]:
        """코스피 종목 명단. base_date에 데이터가 없으면 직전 영업일로 최대 10일 소급.
        only_common=True → 주권·보통주만(운영기업 1社1코드). REITs도 주권으로 포함.
        ETF/ETN/우선주는 제외(감사보고서 대상 아님 또는 동일 corp 중복 방지).
        """
        rows = self._rows(base_date)
        if not rows:                       # 휴장일·미정산일 폴백
            d = date.fromisoformat(f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:]}")
            for _ in range(10):
                d -= timedelta(days=1)
                if d.weekday() >= 5:       # 주말 건너뜀
                    continue
                rows = self._rows(d.strftime("%Y%m%d"))
                if rows:
                    break
        out = []
        for row in rows:
            if (row.get("MKT_TP_NM") or "").strip() != market:
                continue
            if only_common:
                if (row.get("SECUGRP_NM") or "").strip() != "주권":
                    continue
                if (row.get("KIND_STKCERT_TP_NM") or "").strip() not in ("보통주", ""):
                    continue
            rec = {std: (row.get(src, "") or "").strip()
                   for std, src in _FIELD_MAP.items()}
            if rec["stock_code"]:
                out.append(rec)
        return out


# 키가 없을 때 개발용 더미 ─────────────────────
class DummyKrxClient:
    """KRX 키 없을 때 흐름 검증용. 소수 종목만 반환(업종은 DART로 보완)."""
    def kospi_listing(self, base_date: str, market: str = "KOSPI",
                      only_common: bool = True) -> list[dict]:
        return [
            {"stock_code": "005930", "name": "삼성전자", "krx_sector": ""},
            {"stock_code": "000660", "name": "SK하이닉스", "krx_sector": ""},
            {"stock_code": "000720", "name": "현대건설", "krx_sector": ""},
            {"stock_code": "047040", "name": "대우건설", "krx_sector": ""},
            {"stock_code": "028050", "name": "삼성E&A", "krx_sector": ""},
        ]


# ── KRX 업종분류 CSV (data.krx.co.kr 수동 다운로드) ─────────────────
# OpenAPI 종목기본정보엔 업종이 없으므로, data.krx.co.kr의 '업종분류 현황'을
# 수동 다운로드(cp949 CSV)해 종목코드→업종명으로 보완한다.
# 컬럼: 종목코드,종목명,시장구분,업종명,종가,대비,등락률,시가총액 (업종명 26종, KOSPI)
def load_sector_csv(path) -> dict:
    """KRX 업종분류 CSV → {stock_code(6자리): {'krx_sector','mktcap'}}."""
    import csv as _csv
    out: dict[str, dict] = {}
    with open(path, encoding="cp949", newline="") as f:
        for r in _csv.DictReader(f):
            sc = (r.get("종목코드") or "").strip().zfill(6)
            if not sc:
                continue
            out[sc] = {
                "krx_sector": (r.get("업종명") or "").strip(),
                "mktcap": (r.get("시가총액") or "").strip(),
            }
    return out


def find_latest_sector_csv(inputs_dir) -> "str | None":
    """inputs/krx_sector_*.csv 중 최신(파일명 기준) 경로. 없으면 None."""
    from pathlib import Path as _Path
    files = sorted(_Path(inputs_dir).glob("krx_sector_*.csv"))
    return str(files[-1]) if files else None
