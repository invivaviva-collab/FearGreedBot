import asyncio
import aiohttp
import json
import logging
import os
import sys
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, Tuple
# 구문 오류를 유발했던 보이지 않는 특수 공백 문자를 제거함.
import pytz

# FastAPI 및 uvicorn import (웹 서비스 구동을 위해 필요)
from fastapi import FastAPI
import uvicorn

# =========================================================
# --- [1] 로깅 설정 (콘솔 전용) ---
# =========================================================

# 한국 시간대(KST) 정의 (UTC+9)
KST = pytz.timezone('Asia/Seoul')

def kst_time(*args):
    """logging.Formatter의 converter를 KST 시간으로 설정하기 위한 함수"""
    return datetime.now(KST).timetuple()

root_logger = logging.getLogger()
# ✅ 수정: 로그 레벨을 INFO에서 DEBUG로 변경하여 Self-Ping 성공 로그를 포함한 모든 상세 로그를 출력
root_logger.setLevel(logging.DEBUG) 
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    fmt='%(asctime)s KST - %(levelname)s - %(funcName)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
formatter.converter = kst_time 

if root_logger.hasHandlers():
    root_logger.handlers.clear() 
root_logger.addHandler(handler)


# =========================================================
# --- [2] 전역 설정 및 환경 변수 로드 ---
# =========================================================
CNN_BASE_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/"
HEADERS = {'User-Agent': 'Mozilla/5.0 (Render FG Monitor)'}
STOCK_KR_MAP: Dict[str, str] = {
    "extreme fear": "극단적 공포",
    "fear": "공포",
    "neutral": "중립",
    "greed": "탐욕",
    "extreme greed": "극단적 탐욕",
    "n/a": "데이터 없음"
}

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_TARGET_CHAT_ID = os.environ.get('TELEGRAM_TARGET_CHAT_ID')
FEAR_THRESHOLD = 25
MONITOR_INTERVAL_SECONDS = 60 * 5 # 5분 간격으로 CNN 데이터 체크

# 🚀 슬립 방지용 설정 추가 (Render의 15분(900초) 슬립을 막기 위해 10분(600초) 간격으로 설정)
SELF_PING_INTERVAL_SECONDS = 60 * 10 
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL') # Render 환경 변수 사용

status = {"last_alert_date": "1970-01-01", "sent_values_today": []}
ERROR_SCORE_VALUE = 100.00
ERROR_VALUE = 100.0000
ERROR_RATING_STR = "데이터 오류"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_TARGET_CHAT_ID:
    logging.error("TELEGRAM_BOT_TOKEN 또는 CHAT_ID 환경 변수가 설정되지 않았습니다. 알림이 작동하지 않습니다.")

if not RENDER_EXTERNAL_URL:
    logging.warning("RENDER_EXTERNAL_URL 환경 변수가 설정되지 않았습니다. 자체 핑(Self-Ping) 기능이 작동하지 않을 수 있습니다.")


# =========================================================
# --- [3] CNN 데이터 가져오기 (클래스 유지) ---
# =========================================================

class CnnFearGreedIndexFetcher:
    # ... (로직 생략 - 기존 코드와 동일)
    def __init__(self):
        self.fg_score: Optional[float] = None
        self.fg_rating_kr: Optional[str] = None
        self.pc_value: Optional[float] = None
        self.pc_rating_kr: Optional[str] = None

    def _set_error_values(self):
        self.fg_score = ERROR_SCORE_VALUE
        self.fg_rating_kr = ERROR_RATING_STR
        self.pc_value = ERROR_VALUE
        self.pc_rating_kr = ERROR_RATING_STR

    async def fetch_data(self) -> bool:
        self._set_error_values()
        cnn_fetch_success = False
        today = datetime.now(KST).date() 
        dates_to_try = [today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")]
        
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            for date_str in dates_to_try:
                url = CNN_BASE_URL + date_str
                try:
                    async with session.get(url, timeout=5) as resp:
                        if resp.status == 404:
                            logging.warning(f"HTTP 404 Not Found for {date_str}")
                            continue
                        resp.raise_for_status()
                        data: Dict[str, Any] = await resp.json()
                        fg_data = data.get("fear_and_greed", {})
                        self.fg_score = float(fg_data.get("score", ERROR_SCORE_VALUE))
                        fg_rating = fg_data.get("rating", "N/A")
                        self.fg_rating_kr = STOCK_KR_MAP.get(fg_rating.lower(), fg_rating)
                        put_call_data = data.get("put_call_options", {})
                        pc_rating = put_call_data.get("rating", "N/A")
                        self.pc_rating_kr = STOCK_KR_MAP.get(pc_rating.lower(), pc_rating)
                        pc_data_list = put_call_data.get("data", [])
                        self.pc_value = float(pc_data_list[-1].get("y", ERROR_VALUE)) if pc_data_list else ERROR_VALUE
                        logging.info(f"Data fetched for {date_str}. FG Score: {self.fg_score:.2f}")
                        cnn_fetch_success = True
                        break
                except Exception as e:
                    logging.error(f"Error fetching CNN data for {date_str}: {e}")
                    continue

        if not cnn_fetch_success:
            self._set_error_values()
            logging.error("CNN 데이터 획득 실패. 오류 값 사용.")
        return cnn_fetch_success

    def get_results(self) -> Optional[Tuple[float, str, float, str]]:
        if self.fg_score is None:
            return None
        return self.fg_score, self.fg_rating_kr, self.pc_value, self.pc_rating_kr

# =========================================================
# --- [4] Telegram 알림 (클래스 유지) ---
# =========================================================

class FearGreedAlerter:
    # ... (로직 생략 - 기존 코드와 동일)
    def __init__(self, token: str, chat_id: str, threshold: int):
        self.token = token
        self.chat_id = chat_id
        self.threshold = threshold
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    async def _send_telegram_alert(self, current_value: int, option_5d_ratio: float, fear_rating_str: str):
        if not self.token or not self.chat_id:
            logging.error("Telegram credentials missing. Skipping alert send.")
            return

        kst_now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')
        pc_ratio_str = f"{option_5d_ratio:.4f}"
        message_text = (
            f"🚨 공포/탐욕 지수 알림 🚨\n\n"
            f"공포/탐욕: `극단적 공포(Extreme Fear)`\n"
            f"현재 지수: **{current_value}**\n\n"
            f"PUT AND CALL OPTIONS: `{fear_rating_str}`\n"
            f"5-day average put/call ratio: **{pc_ratio_str}**\n\n"
            f"발송 일시: {kst_now_str}"
        )
        payload = {'chat_id': self.chat_id, 'text': message_text, 'parse_mode': 'Markdown'}
        
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.api_url, data=payload, timeout=10) as resp:
                        resp.raise_for_status()
                        logging.error(f"🚨 텔레그램 알림 발송 성공! 값: {current_value}")
                        return
            except Exception as e:
                logging.warning(f"텔레그램 발송 실패 (시도 {attempt + 1}/3): {e}. 잠시 후 재시도.")
                await asyncio.sleep(2 ** attempt)
        logging.error("텔레그램 발송 최종 실패.")

    async def check_and_alert(self, current_index_value, option_5d_ratio, fear_rating_str):
        try:
            current_value_int = round(float(current_index_value))
        except:
            logging.warning(f"Invalid F&G value: {current_index_value}")
            return

        today_str = datetime.now(KST).date().strftime("%Y-%m-%d")

        if status['last_alert_date'] != today_str:
            status['last_alert_date'] = today_str
            status['sent_values_today'] = []
            logging.info(f"날짜 변경 감지. 오늘의 발송 목록 초기화: {today_str}")

        if current_value_int <= self.threshold:
            if current_value_int not in status['sent_values_today']:
                status['sent_values_today'].append(current_value_int)
                await self._send_telegram_alert(current_value_int, option_5d_ratio, fear_rating_str)
            else:
                logging.info(f"Duplicate alert skipped: {current_value_int} (already sent today)")
        else:
            logging.info(f"[정상 모니터링] F&G 점수 {current_value_int} ({self.threshold} 초과)")

# =========================================================
# --- [4-1] 시작 시 상태 메시지 발송 (수정) ---
# =========================================================

async def send_startup_message(cnn_fetcher: CnnFearGreedIndexFetcher, alerter: FearGreedAlerter):
    # ... (로직 생략 - 기존 코드와 동일)
    if not alerter.token or not alerter.chat_id:
        logging.error("Telegram credentials missing. Skipping startup message.")
        return

    python_version = sys.version.split()[0] 
    logging.info(f"Python Version: {python_version}") 
    
    success = await cnn_fetcher.fetch_data()
    if success:
        fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
    else:
        fg_score, fg_rating, pc_value, pc_rating = ERROR_SCORE_VALUE, ERROR_RATING_STR, ERROR_VALUE, ERROR_RATING_STR

    kst_now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')

    message_text = (
        f"🚀 공포/탐욕 모니터링 시작 🚀\n\n"        
        f"현재 공포/탐욕 지수: {fg_score:.2f} ({fg_rating})\n"
        f"5-day average put/call ratio: {pc_value:.4f}\n"
        f"모니터링 주기: {MONITOR_INTERVAL_SECONDS}초\n\n"
        f"Python Version: {python_version}\n"
        f"서버 시작: {kst_now_str}"
    )

    payload = {'chat_id': alerter.chat_id, 'text': message_text, 'parse_mode': 'Markdown'}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(alerter.api_url, data=payload, timeout=5) as resp:
                resp.raise_for_status()
                logging.info("정상 시작 메시지 발송 성공")
        except Exception as e:
            logging.error(f"정상 시작 메시지 발송 실패: {e}")


# =========================================================
# --- [5] 메인 루프 (백그라운드 작업용) ---
# =========================================================

async def main_monitor_loop():
    logging.info("--- F&G 모니터링 프로그램 (백그라운드) 시작 ---")
    cnn_fetcher = CnnFearGreedIndexFetcher()
    alerter = FearGreedAlerter(TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET_CHAT_ID, FEAR_THRESHOLD)
    await send_startup_message(cnn_fetcher, alerter)
    while True:
        logging.info(f"--- 데이터 체크 시작 ({MONITOR_INTERVAL_SECONDS}s 주기) ---")
        try:
            if await cnn_fetcher.fetch_data():
                fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
                logging.info(f"F&G 점수: {fg_score:.2f} ({fg_rating}), P/C 값: {pc_value:.4f}")
                await alerter.check_and_alert(fg_score, pc_value, pc_rating)
        except Exception as e:
            logging.error(f"모니터링 루프 중 오류: {e}")
        
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


# =========================================================
# --- [5-1] 🚀 슬립 방지용 루프 추가 ---
# =========================================================

async def self_ping_loop():
    """
    Render Free Tier의 유휴(Idle) 슬립을 방지하기 위해 
    주기적으로 (10분 간격) 서버 자신의 Health Check 엔드포인트에 요청을 보냅니다.
    """
    if not RENDER_EXTERNAL_URL:
        logging.warning("Self-Ping 루프 시작 안 함: RENDER_EXTERNAL_URL 환경 변수가 없습니다.")
        return

    logging.info(f"--- 🚀 Self-Ping 루프 시작 ({SELF_PING_INTERVAL_SECONDS}s 주기) ---")
    ping_url = f"{RENDER_EXTERNAL_URL}" # Health check 엔드포인트 (/)
    logging.info(f"Ping 대상 URL: {ping_url}")

    while True:
        await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ping_url, timeout=10) as resp:
                    resp.raise_for_status()
                    # Self-Ping 성공 시 DEBUG 레벨로 로그 출력 (이제 DEBUG 활성화로 로그 출력됨)
                    logging.debug(f"Self-Ping 성공 ({resp.status}). 서버 유휴 타이머 리셋됨.")
        except Exception as e:
            logging.error(f"Self-Ping 실패: 서버가 외부 URL에 접근하지 못합니다. ({e})")
            # 네트워크가 다운되었을 경우, 완전한 서버 종료를 막기 위해 잠시만 대기
            await asyncio.sleep(30)


# =========================================================
# --- [6] FastAPI 웹 서비스 설정 ---
# =========================================================

app = FastAPI(
    title="Fear & Greed Monitor",
    description="CNN Fear & Greed Index monitor running as a background task on Render Free Tier.",
    version="1.0.0"
)

# 서버 시작 시 백그라운드 작업 시작

@app.on_event("startup")
async def startup_event():
    logging.info("FastAPI Server Startup: Launching background tasks.")
    # 1. 모니터링 루프 시작 (데이터 체크 및 알림)
    asyncio.create_task(main_monitor_loop())
    
    # 2. 🚀 Self-Ping 루프 시작 (슬립 방지)
    asyncio.create_task(self_ping_loop())

# Health Check Endpoint (Render가 서버가 살아있는지 확인하는 용도)
@app.get("/")
async def health_check():
    return {
        "status": "running", 
        "message": "F&G monitor is active in the background.",
        "last_alert_date": status.get('last_alert_date'),
        "sent_values_today": status.get('sent_values_today')
    }

# =========================================================
# --- [7] 실행 ---
# =========================================================

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    logging.info(f"Starting uvicorn server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
