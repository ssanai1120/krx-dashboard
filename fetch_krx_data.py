# -*- coding: utf-8 -*-
"""
KRX 데이터 수집 스크립트
=========================

pykrx를 이용해 최근 N영업일(기본 20영업일) 동안의
 - 종목별 시세 / 등락률 / 시가총액 / 거래대금
 - 외국인 / 기관 일별 순매수금액 (20일 추이 포함)
을 전체 상장종목 대상으로 수집하여, 대시보드(krx_dashboard.html)가
바로 읽을 수 있는 형태(krx_data.js)로 저장합니다.

사용 전 준비
------------
1) pip install pykrx
2) KRX 정보데이터시스템 계정으로 로그인할 수 있도록 환경변수 설정
     Windows (명령 프롬프트):
         set KRX_ID=내아이디
         set KRX_PW=내비밀번호
     Windows (PowerShell):
         $env:KRX_ID="내아이디"
         $env:KRX_PW="내비밀번호"
     macOS / Linux:
         export KRX_ID=내아이디
         export KRX_PW=내비밀번호

실행
----
    python fetch_krx_data.py [YYYYMMDD]

   - 날짜를 생략하면 오늘 날짜를 기준일(D-day)로 사용합니다.
   - 결과는 이 스크립트와 같은 폴더에 krx_data.js 로 저장되며,
     krx_dashboard.html 과 같은 폴더에 두면 대시보드가 자동으로 읽습니다.

주의
----
- pykrx는 KRX 웹사이트를 스크래핑하는 라이브러리이므로, 전체 상장종목을
  20영업일치 조회하면 API 호출이 60여 회 발생합니다. 서버 부담을 줄이기
  위해 호출 사이에 짧은 대기시간(SLEEP_SEC)을 두었습니다. 전체 실행에는
  몇 분 정도 걸릴 수 있습니다.
- 당일(D-day) 데이터는 장 마감 이후(보통 18시 이후)에 정상적으로
  조회됩니다. 자동화 시에는 평일 18시 이후로 스케줄을 잡는 것을
  권장합니다.
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta

try:
    from pykrx import stock
except ImportError:
    sys.exit("pykrx가 설치되어 있지 않습니다. 먼저 `pip install pykrx`를 실행하세요.")

PERIOD = 20          # 조회 영업일 수 (대시보드의 스파크라인 길이와 일치)
SLEEP_SEC = 0.3      # 호출 사이 대기시간 (초)
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "krx_data.js")


def check_credentials():
    if not os.environ.get("KRX_ID") or not os.environ.get("KRX_PW"):
        sys.exit(
            "KRX_ID / KRX_PW 환경변수가 설정되어 있지 않습니다.\n"
            "KRX 정보데이터시스템 계정 정보를 환경변수로 설정한 뒤 다시 실행해 주세요."
        )


def get_period_dates(base_date: str, period: int):
    """base_date(YYYYMMDD)를 포함한 최근 `period`영업일 목록을 오래된 날짜부터 반환"""
    base_dt = datetime.strptime(base_date, "%Y%m%d")
    from_dt = base_dt - timedelta(days=period * 2 + 14)  # 주말/공휴일 여유분
    fromdate = from_dt.strftime("%Y%m%d")
    days = stock.get_previous_business_days(fromdate=fromdate, todate=base_date)
    if not days:
        sys.exit(f"{base_date} 기준으로 영업일을 찾을 수 없습니다. 날짜를 확인해 주세요.")
    days = [d.strftime("%Y%m%d") for d in days][-period:]
    return days


def to_won_million(value: float) -> int:
    """원 단위 금액을 백만원 단위 정수로 변환 (반올림)"""
    return int(round(value / 1_000_000))


def main():
    check_credentials()

    base_date = sys.argv[1] if len(sys.argv) > 1 else datetime.today().strftime("%Y%m%d")

    # 오늘 날짜로 시도했을 때 KRX 데이터가 아직 없으면 전날 영업일로 자동 후퇴
    if len(sys.argv) <= 1:
        try:
            test_df = stock.get_market_ohlcv_by_ticker(base_date, market="ALL")
            if test_df.empty:
                prev_days = stock.get_previous_business_days(
                    fromdate=(datetime.today() - timedelta(days=10)).strftime("%Y%m%d"),
                    todate=base_date
                )
                if len(prev_days) >= 2:
                    base_date = prev_days[-2].strftime("%Y%m%d")
                    print(f"오늘({datetime.today().strftime('%Y%m%d')}) 데이터 미집계. 기준일을 전날({base_date})로 변경합니다.")
        except Exception:
            pass

    print(f"기준일(D-day): {base_date}")

    dates = get_period_dates(base_date, PERIOD)
    print(f"조회 기간: {dates[0]} ~ {dates[-1]} ({len(dates)}영업일)")

    price_daily = {}     # ticker -> [종가 * PERIOD]
    chg_daily = {}       # ticker -> [등락률 * PERIOD]
    foreign_daily = {}   # ticker -> [외국인 순매수거래대금(원) * PERIOD]
    inst_daily = {}      # ticker -> [기관 순매수거래대금(원) * PERIOD]
    names = {}           # ticker -> 종목명
    trade_amt_dday = {}  # ticker -> D-day 거래대금(원)
    n = len(dates)
    last_collected_date = dates[0]  # 실제로 수집된 마지막 날짜 추적

    for idx, d in enumerate(dates):
        print(f"  [{idx+1}/{n}] {d} 데이터 수집 중...")

        try:
            ohlcv = stock.get_market_ohlcv_by_ticker(d, market="ALL")
            time.sleep(SLEEP_SEC)
            f_df = stock.get_market_net_purchases_of_equities_by_ticker(d, d, market="ALL", investor="외국인")
            time.sleep(SLEEP_SEC)
            i_df = stock.get_market_net_purchases_of_equities_by_ticker(d, d, market="ALL", investor="기관합계")
            time.sleep(SLEEP_SEC)

            if ohlcv.empty or f_df.empty or i_df.empty:
                print(f"    → {d} 데이터 미집계 (장 마감 전이거나 KRX 미제공). 건너뜀.")
                continue

        except Exception as e:
            print(f"    → {d} 데이터 수집 오류: {e}. 건너뜀.")
            continue

        close_map = ohlcv["종가"].to_dict()
        chg_map = ohlcv["등락률"].to_dict()
        trade_amt_map = ohlcv["거래대금"].to_dict()
        f_amt_map = f_df["순매수거래대금"].to_dict()
        f_name_map = f_df["종목명"].to_dict()
        i_amt_map = i_df["순매수거래대금"].to_dict()

        # 종목 전체 집합 갱신 (D-day 기준 거래대금 상위 정렬에 사용)
        last_collected_date = d  # 성공적으로 수집된 날짜 갱신
        for ticker in close_map:
            price_daily.setdefault(ticker, [0] * n)[idx] = int(close_map.get(ticker, 0))
            chg_daily.setdefault(ticker, [0.0] * n)[idx] = float(chg_map.get(ticker, 0.0))
            # 수집된 날짜 중 가장 마지막 날짜의 거래대금으로 계속 갱신
            trade_amt_dday[ticker] = int(trade_amt_map.get(ticker, 0))

        for ticker in f_amt_map:
            foreign_daily.setdefault(ticker, [0] * n)[idx] = int(f_amt_map.get(ticker, 0))
            if ticker in f_name_map:
                names[ticker] = f_name_map[ticker]

        for ticker in i_amt_map:
            inst_daily.setdefault(ticker, [0] * n)[idx] = int(i_amt_map.get(ticker, 0))

    print("시가총액 데이터 수집 중...")
    cap_df = stock.get_market_cap_by_ticker(last_collected_date, market="ALL")
    cap_map = cap_df["시가총액"].to_dict()

    # D-day 거래대금 기준 상위 정렬
    tickers = sorted(trade_amt_dday.keys(), key=lambda t: trade_amt_dday.get(t, 0), reverse=True)

    stocks = []
    for ticker in tickers:
        if ticker not in foreign_daily or ticker not in price_daily:
            continue  # 데이터가 누락된 종목(거래정지 등)은 제외
        f_trend = foreign_daily[ticker]
        i_trend = inst_daily.get(ticker, [0] * n)
        chg = chg_daily.get(ticker, [0.0] * n)

        stocks.append({
            "code": ticker,
            "name": names.get(ticker, stock.get_market_ticker_name(ticker)),
            "price": price_daily[ticker][-1],
            "chg": [
                round(chg[-1], 2) if n >= 1 else 0,
                round(chg[-2], 2) if n >= 2 else 0,
                round(chg[-3], 2) if n >= 3 else 0,
            ],
            "mcap": to_won_million(cap_map.get(ticker, 0)),
            "tradeAmt": to_won_million(trade_amt_dday.get(ticker, 0)),
            "foreign": {
                "d0": to_won_million(f_trend[-1]) if n >= 1 else 0,
                "d1": to_won_million(f_trend[-2]) if n >= 2 else 0,
                "d2": to_won_million(f_trend[-3]) if n >= 3 else 0,
                "d20": to_won_million(sum(f_trend)),
            },
            "inst": {
                "d0": to_won_million(i_trend[-1]) if n >= 1 else 0,
                "d1": to_won_million(i_trend[-2]) if n >= 2 else 0,
                "d2": to_won_million(i_trend[-3]) if n >= 3 else 0,
                "d20": to_won_million(sum(i_trend)),
            },
            "priceTrend": price_daily[ticker],
            "foreignTrend": [to_won_million(v) for v in f_trend],
            "instTrend": [to_won_million(v) for v in i_trend],
        })

    for i, s in enumerate(stocks):
        s["rank"] = i + 1

    payload = {
        "asOfDate": last_collected_date,
        "periodDates": dates,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": stocks,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("// 자동 생성 파일 - fetch_krx_data.py 실행 결과\n")
        f.write("const KRX_DATA = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")

    print(f"완료: {len(stocks)}개 종목 -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
