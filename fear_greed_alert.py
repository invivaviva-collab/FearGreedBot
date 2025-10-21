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

# Render에서 제공하는 외부 호스트 이름 (슬립 방지용)
SELF_PING_HOST = os.environ.get('RENDER_EXTERNAL_HOSTNAME')

FEAR_THRESHOLD = 25
MONITOR_INTERVAL_SECONDS = 60 * 5 # 5분 간격으로 변경하여 무료 서버의 자원 소모를 줄임
SELF_PING_INTERVAL_SECONDS = 60 * 10 # 10분 간격으로 셀프 핑

# 서버 RAM에서 상태 유지 (Render 재시작 시 초기화될 수 있음에 유의)
status = {"last_alert_date": "1970-01-01", "sent_values_today": []}

ERROR_SCORE_VALUE = 100.00
ERROR_VALUE = 100.0000
ERROR_RATING_STR = "데이터 오류"

# 텔레그램 및 셀프 핑 설정 검사
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_TARGET_CHAT_ID:
    logging.error("TELEGRAM_BOT_TOKEN 또는 CHAT_ID 환경 변수가 설정되지 않았습니다. 알림이 작동하지 않습니다.")

if not SELF_PING_HOST:
    logging.warning("RENDER_EXTERNAL_HOSTNAME 환경 변수가 설정되지 않았습니다. 슬립 방지 기능이 작동하지 않을 수 있습니다.")
    SELF_PING_URL = None
else:
    SELF_PING_URL = f"https://{SELF_PING_HOST}/"
    logging.info(f"Self-Ping URL 설정 완료: {SELF_PING_URL}")

# =========================================================
# --- [3] CNN 데이터 가져오기 (클래스 유지) ---
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
# --- [4] Telegram 알림 (클래스 유지) ---
# =========================================================
class FearGreedAlerter:
    def __init__(self, token: str, chat_id: str, threshold: int):
        self.token = token
        self.chat_id = chat_id
        self.threshold = threshold
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    async def _send_telegram_alert(self, current_value: int, option_5d_ratio: float, fear_rating_str: str):
        if not self.token or not self.chat_id:
            logging.error("Telegram credentials missing. Skipping alert send.")
            return
            
        pc_ratio_str = f"{option_5d_ratio:.4f}"
        message_text = (
            f"🚨 공포 탐욕 지수 알림 🚨\n\n"
            f"공포/탐욕: `극단적 공포(Extreme Fear)`\n"
            f"현재 지수: **{current_value}**\n\n"
            f"PUT AND CALL OPTIONS: `{fear_rating_str}`\n"
            f"5-day average put/call ratio: **{pc_ratio_str}**\n\n"
            f"발송 일시: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        payload = {'chat_id': self.chat_id, 'text': message_text, 'parse_mode': 'Markdown'}
        # 재시도 로직 추가 (Render 환경에서는 네트워크 이슈가 있을 수 있음)
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.api_url, data=payload, timeout=10) as resp:
                        resp.raise_for_status()
                        logging.info(f"텔레그램 알림 발송 성공! 값: {current_value}")
                        return # 성공 시 종료
            except Exception as e:
                logging.warning(f"텔레그램 발송 실패 (시도 {attempt + 1}/3): {e}. 잠시 후 재시도.")
                await asyncio.sleep(2 ** attempt) # Exponential Backoff
        logging.error("텔레그램 발송 최종 실패.")


    async def check_and_alert(self, current_index_value, option_5d_ratio, fear_rating_str):
        try:
            current_value_int = round(float(current_index_value))
        except:
            logging.warning(f"Invalid F&G value: {current_index_value}")
            return

        today_str = date.today().strftime("%Y-%m-%d")
        if status['last_alert_date'] != today_str:
            status['last_alert_date'] = today_str
            status['sent_values_today'] = []
            logging.info(f"날짜 변경 감지. 오늘의 발송 목록 초기화: {today_str}")

        if current_value_int <= self.threshold:
            # 극단적 공포 범위(0-25) 내에서 값이 변경될 때만 알림
            if current_value_int not in status['sent_values_today']:
                status['sent_values_today'].append(current_value_int)
                await self._send_telegram_alert(current_value_int, option_5d_ratio, fear_rating_str)
            else:
                logging.info(f"Duplicate alert skipped: {current_value_int} (already sent today)")
        else:
            logging.info(f"No alert. Score {current_value_int} above threshold ({self.threshold}).")


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

    message_text = (f"🚀 공포/탐욕 모니터링 시작 🚀\n\n"
            f"현재 공포/탐욕 지수: {fg_score:.2f} ({fg_rating})\n"
            f"5-day average put/call ratio: {pc_value:.4f}\n"
            f"모니터링 주기: {MONITOR_INTERVAL_SECONDS}초\n\n"
            f"발송 일시: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
    
    alerter_api_url = f"https://api.telegram.org/bot{alerter.token}/sendMessage"
    payload = {'chat_id': alerter.chat_id, 'text': message_text, 'parse_mode': 'Markdown'}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(alerter_api_url, data=payload, timeout=5) as resp:
                resp.raise_for_status()
                logging.info("정상 시작 메시지 발송 성공")
        except Exception as e:
            logging.error(f"정상 시작 메시지 발송 실패: {e}")

# =========================================================
# --- [5] 서버 슬립 방지 루프 (추가된 부분) ---
# =========================================================
async def self_ping_loop():
    if not SELF_PING_URL:
        logging.warning("Self-Ping URL이 설정되지 않아 슬립 방지 루프를 시작하지 않습니다.")
        return

    logging.info("--- 서버 유휴 상태 방지 (Self-Ping) 루프 시작 ---")
    while True:
        await asyncio.sleep(SELF_PING_INTERVAL_SECONDS) # 주기적으로 대기
        try:
            async with aiohttp.ClientSession() as session:
                # Health Check 엔드포인트에 요청
                async with session.get(SELF_PING_URL, timeout=5) as resp:
                    status_code = resp.status
                    if status_code == 200:
                        logging.debug(f"Self-Ping 성공 ({status_code}). 서버 유휴 타이머 리셋됨.")
                    else:
                        logging.warning(f"Self-Ping 비정상 응답 ({status_code}).")
                    
        except asyncio.TimeoutError:
            logging.warning("Self-Ping 시간 초과 (Timeout Error).")
        except Exception as e:
            logging.error(f"Self-Ping 오류: {e}")


# =========================================================
# --- [6] 메인 모니터링 루프 (백그라운드 작업용) ---
# =========================================================
async def main_monitor_loop():
    logging.info("--- F&G 모니터링 프로그램 (백그라운드) 시작 ---")
    cnn_fetcher = CnnFearGreedIndexFetcher()
    alerter = FearGreedAlerter(TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET_CHAT_ID, FEAR_THRESHOLD)

    # 시작 시 한 번 발송
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
        
        # Render Free Tier에서 너무 잦은 요청은 피하기 위해 대기 시간 사용
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)

# =========================================================
# --- [7] FastAPI 웹 서비스 설정 ---
# =========================================================
app = FastAPI(
    title="Fear & Greed Monitor",
    description="CNN Fear & Greed Index monitor running as a background task on Render Free Tier.",
    version="1.0.0"
)

# 서버 시작 시 백그라운드 작업 시작
@app.on_event("startup")
async def startup_event():
    logging.info("FastAPI Server Startup: Launching main_monitor_loop and self_ping_loop as background tasks.")
    # 1. 모니터링 루프를 독립적인 비동기 작업으로 실행
    asyncio.create_task(main_monitor_loop())
    # 2. 서버 슬립 방지 루프를 독립적인 비동기 작업으로 실행 (추가됨)
    asyncio.create_task(self_ping_loop())

# Health Check Endpoint (Render가 서버가 살아있는지 확인하는 용도)
@app.get("/")
async def health_check():
    return {
        "status": "running", 
        "message": "F&G monitor and self-ping are active in the background.",
        "last_alert_date": status.get('last_alert_date'),
        "sent_values_today": status.get('sent_values_today'),
        "ping_url_active": SELF_PING_URL is not None
    }

# =========================================================
# --- [8] 실행 ---
# =========================================================
if __name__ == '__main__':
    # Render는 환경 변수로 PORT를 제공합니다.
    port = int(os.environ.get("PORT", 8000))
    
    logging.info(f"Starting uvicorn server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
