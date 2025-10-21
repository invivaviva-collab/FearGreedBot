import asyncio
import aiohttp
import json
import logging
import os
import sys
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, Tuple

# FastAPI 및 uvicorn import (웹 서비스 구동을 위해 필요)
from fastapi import FastAPI
import uvicorn

# =========================================================
# --- [1] 로깅 설정 (콘솔 전용) ---
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(funcName)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout # Render 로그 스트림 설정
)
logging.getLogger('uvicorn.error').setLevel(logging.WARNING)
logging.getLogger('uvicorn.access').setLevel(logging.WARNING)


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

# ⚠️ 환경 변수에서 로드 (보안 및 Render 환경에 필수)
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_TARGET_CHAT_ID = os.environ.get('TELEGRAM_TARGET_CHAT_ID')

# 5분 간격으로 무조건 발송
MONITOR_INTERVAL_SECONDS = 300 

# ❌ 무조건 발송을 위해 status 변수 제거

ERROR_SCORE_VALUE = 100.00
ERROR_VALUE = 100.0000
ERROR_RATING_STR = "데이터 오류"

# 텔레그램 설정 검사
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_TARGET_CHAT_ID:
    logging.error("TELEGRAM_BOT_TOKEN 또는 CHAT_ID 환경 변수가 설정되지 않았습니다. 알림이 작동하지 않습니다.")

# =========================================================
# --- [3] CNN 데이터 가져오기 (클래스 유지) ---
# ... (내용 변경 없음)
# =========================================================
class CnnFearGreedIndexFetcher:
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
        today = datetime.utcnow().date()
        dates_to_try = [today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")]

        # Timeout을 짧게 조정
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            for date_str in dates_to_try:
                url = CNN_BASE_URL + date_str
                try:
                    # 응답 시간 초과를 5초로 설정
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
# --- [4] Telegram 알림 ---
# =========================================================
class FearGreedAlerter:
    # ❌ threshold 제거 (더 이상 조건 없이 발송)
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage" # API URL 정의

    async def _send_telegram_report(self, current_value: float, rating_str: str, option_5d_ratio: float, pc_rating_str: str):
        if not self.token or not self.chat_id:
            logging.error("Telegram credentials missing. Skipping alert send.")
            return
            
        pc_ratio_str = f"{option_5d_ratio:.4f}"
        
        # 메시지 제목을 "주기적 지수 보고"로 변경
        message_text = (
            f"📊 5분 주기 지수 보고 📊\n\n"
            f"➡️ FEAR & GREED INDEX: **{current_value:.2f}**\n"
            f"   - Rating: `{rating_str}`\n\n"
            f"➡️ PUT AND CALL OPTIONS:\n"
            f"   - Rating: `{pc_rating_str}`\n"
            f"   - P/C Ratio (5-day avg): **{pc_ratio_str}**\n\n"
            f"발송 일시: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        payload = {'chat_id': self.chat_id, 'text': message_text, 'parse_mode': 'Markdown'}
        # 재시도 로직 추가 (Render 환경에서는 네트워크 이슈가 있을 수 있음)
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.api_url, data=payload, timeout=10) as resp:
                        resp.raise_for_status()
                        logging.info(f"텔레그램 주기적 보고 발송 성공! FG 값: {current_value:.2f}")
                        return # 성공 시 종료
            except Exception as e:
                logging.warning(f"텔레그램 발송 실패 (시도 {attempt + 1}/3): {e}. 잠시 후 재시도.")
                await asyncio.sleep(2 ** attempt) # Exponential Backoff
        logging.error("텔레그램 발송 최종 실패.")


    # 💡 [핵심 수정] 조건이나 중복 확인 없이 무조건 텔레그램 보고서를 발송합니다.
    async def check_and_alert(self, current_index_value, rating_str, option_5d_ratio, pc_rating_str):
        try:
            current_value_float = float(current_index_value)
        except:
            logging.warning(f"Invalid F&G value: {current_index_value}")
            return
        
        # 조건 없이 즉시 보고서를 발송
        await self._send_telegram_report(current_value_float, rating_str, option_5d_ratio, pc_rating_str)
        
        # ❌ 조건부 알림 로직 제거
        # ❌ status 업데이트 로직 제거
        # ❌ No alert 로그 제거


# =========================================================
# --- [4-1] 시작 시 상태 메시지 발송 ---
# =========================================================
async def send_startup_message(cnn_fetcher: CnnFearGreedIndexFetcher, alerter: FearGreedAlerter):
    if not alerter.token or not alerter.chat_id:
        logging.error("Telegram credentials missing. Skipping startup message.")
        return

    # 데이터 가져오기는 한 번 더 시도
    success = await cnn_fetcher.fetch_data()
    if success:
        fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
    else:
        fg_score, fg_rating, pc_value, pc_rating = ERROR_SCORE_VALUE, ERROR_RATING_STR, ERROR_VALUE, ERROR_RATING_STR

    message_text = (f"🚀 공포/탐욕 모니터링 시작 (주기적 보고 모드) 🚀\n\n"
            f"현재 공포/탐욕 지수: {fg_score:.2f} ({fg_rating})\n"
            f"5-day average put/call ratio: {pc_value:.4f}\n"
            f"보고 주기: **{MONITOR_INTERVAL_SECONDS}초 (5분)**\n\n"
            f"서버 시작: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
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
# --- [5] 메인 모니터링 루프 (백그라운드 작업용) ---
# =========================================================
async def main_monitor_loop():
    logging.info("--- F&G 모니터링 프로그램 (백그라운드) 시작 ---")
    cnn_fetcher = CnnFearGreedIndexFetcher()
    # ❌ FEAR_THRESHOLD 제거하고 alerter 초기화
    alerter = FearGreedAlerter(TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET_CHAT_ID) 

    # 시작 시 한 번 발송
    await send_startup_message(cnn_fetcher, alerter)

    while True:
        logging.info(f"--- 데이터 체크 시작 ({MONITOR_INTERVAL_SECONDS}s 주기) ---")
        try:
            if await cnn_fetcher.fetch_data():
                fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
                logging.info(f"F&G 점수: {fg_score:.2f} ({fg_rating}), P/C 값: {pc_value:.4f}")
                # 💡 [핵심 수정] 무조건 알림 발송
                await alerter.check_and_alert(fg_score, fg_rating, pc_value, pc_rating)
            else:
                 # 데이터 획득 실패 시에도 알림을 원할 경우 여기에 알림 로직 추가 가능
                 logging.warning("데이터 획득 실패. 이번 주기 알림 건너뜁니다.")
        except Exception as e:
            logging.error(f"모니터링 루프 중 오류: {e}")
        
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)

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
    logging.info("FastAPI Server Startup: Launching main_monitor_loop as a background task (External ping active).")
    # 1. 모니터링 루프를 독립적인 비동기 작업으로 실행
    asyncio.create_task(main_monitor_loop())

# 🚀 UptimeRobot의 HEAD 요청을 처리하여 405 에러를 방지합니다.
@app.head("/")
async def health_check_head():
    # HEAD 요청은 본문을 반환하지 않고 200 OK 상태만 반환합니다.
    return {"status": "running"}

# Health Check Endpoint (Render가 서버가 살아있는지 확인하는 용도)
@app.get("/")
async def health_check():
    return {
        "status": "running", 
        "message": "F&G monitor is active in the background. Sending report every 5 minutes regardless of score.",
        # ❌ status 변수가 제거되어 해당 정보는 제공하지 않음
    }

# =========================================================
# --- [7] 실행 ---
# =========================================================
if __name__ == '__main__':
    # Render는 환경 변수로 PORT를 제공합니다.
    port = int(os.environ.get("PORT", 8000))
    
    logging.info(f"Starting uvicorn server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
