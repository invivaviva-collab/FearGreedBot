import asyncio
import aiohttp
import json
import logging
import os
import sys
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, Tuple

from fastapi import FastAPI, HTTPException 
import uvicorn

# =========================================================
# --- [1] 로깅 설정 (콘솔 전용 및 레벨 조정) ---
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(funcName)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout 
)
# 반복되는 정보는 DEBUG 레벨로 호출하도록 logger 설정 (개선 사항 ④)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG) 

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

# ⚠️ 환경 변수에서 로드 
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_TARGET_CHAT_ID = os.environ.get('TELEGRAM_TARGET_CHAT_ID')
SELF_PING_HOST = os.environ.get('RENDER_EXTERNAL_HOSTNAME')

# --- 조정된 상수 ---
FEAR_THRESHOLD = 25
MONITOR_INTERVAL_SECONDS = 60 * 5 
SELF_PING_INTERVAL_SECONDS = 60 * 10
MAX_PING_FAILURES = 3 # 개선 사항 ②: 최대 연속 핑 실패 횟수

# 서버 RAM에서 상태 유지
status = {"last_alert_date": "1970-01-01", "sent_values_today": []}

ERROR_SCORE_VALUE = 100.00
ERROR_VALUE = 100.0000
ERROR_RATING_STR = "데이터 오류"

# 전역 aiohttp 세션 선언 (개선 사항 ①)
HTTP_SESSION: Optional[aiohttp.ClientSession] = None 

# 텔레그램 및 셀프 핑 설정 검사
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_TARGET_CHAT_ID:
    logger.error("TELEGRAM_BOT_TOKEN 또는 CHAT_ID 환경 변수가 설정되지 않았습니다. 알림이 작동하지 않습니다.")

if not SELF_PING_HOST:
    logger.warning("RENDER_EXTERNAL_HOSTNAME 환경 변수가 설정되지 않았습니다. 슬립 방지 기능이 작동하지 않을 수 있습니다.")
    SELF_PING_URL = None
else:
    SELF_PING_URL = f"https://{SELF_PING_HOST}/"
    logger.info(f"Self-Ping URL 설정 완료: {SELF_PING_URL}")

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
        """전역 세션(HTTP_SESSION)을 사용하여 데이터를 가져옵니다."""
        
        if not HTTP_SESSION:
            logger.error("HTTP_SESSION is not initialized.")
            self._set_error_values()
            return False

        self._set_error_values()
        cnn_fetch_success = False
        today = datetime.utcnow().date()
        dates_to_try = [today.strftime("%Y-%m-%d"), (today - timedelta(days=1)).strftime("%Y-%m-%d")]

        # 전역 세션(HTTP_SESSION) 사용 (개선 사항 ①)
        for date_str in dates_to_try:
            url = CNN_BASE_URL + date_str
            try:
                # 응답 시간 초과를 5초로 설정
                async with HTTP_SESSION.get(url, timeout=5) as resp: 
                    if resp.status == 404:
                        logger.warning(f"HTTP 404 Not Found for {date_str}")
                        continue
                    resp.raise_for_status()
                    data: Dict[str, Any] = await resp.json()

                    # 데이터 추출 및 타입 변환을 위한 안전한 try-except 블록 (개선 사항 ⑤)
                    try:
                        fg_data = data.get("fear_and_greed", {})
                        self.fg_score = float(fg_data.get("score", ERROR_SCORE_VALUE))
                        fg_rating = fg_data.get("rating", "N/A")
                        self.fg_rating_kr = STOCK_KR_MAP.get(fg_rating.lower(), fg_rating)

                        put_call_data = data.get("put_call_options", {})
                        pc_rating = put_call_data.get("rating", "N/A")
                        self.pc_rating_kr = STOCK_KR_MAP.get(pc_rating.lower(), pc_rating)
                        
                        pc_data_list = put_call_data.get("data", [])
                        # 리스트의 마지막 요소에서 'y' 값 추출
                        pc_last_data = pc_data_list[-1] if pc_data_list else {}
                        self.pc_value = float(pc_last_data.get("y", ERROR_VALUE)) 

                        logger.info(f"Data fetched for {date_str}. FG Score: {self.fg_score:.2f}")
                        cnn_fetch_success = True
                        break
                    
                    except Exception as data_error:
                        # KeyError, ValueError, TypeError 등을 모두 포착
                        logger.error(f"❌ Data structure error during extraction for {date_str}: {data_error}")
                        continue

            except Exception as e:
                logger.error(f"Error fetching CNN data for {date_str}: {e}")
                continue

        if not cnn_fetch_success:
            self._set_error_values()
            logger.error("CNN 데이터 획득 최종 실패. 오류 값 사용.")
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
        if not self.token or not self.chat_id or not HTTP_SESSION: # HTTP_SESSION 유효성 추가 체크
            logger.error("Telegram credentials or HTTP_SESSION missing. Skipping alert send.")
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
        
        # 재시도 로직 추가
        for attempt in range(3):
            try:
                # 전역 세션 사용 (개선 사항 ①)
                async with HTTP_SESSION.post(self.api_url, data=payload, timeout=10) as resp: 
                    resp.raise_for_status()
                    logger.info(f"텔레그램 알림 발송 성공! 값: {current_value}")
                    return # 성공 시 종료
            except Exception as e:
                logger.warning(f"텔레그램 발송 실패 (시도 {attempt + 1}/3): {e}. 잠시 후 재시도.")
                await asyncio.sleep(2 ** attempt) # Exponential Backoff
        logger.error("텔레그램 발송 최종 실패.")


    async def check_and_alert(self, current_index_value, option_5d_ratio, fear_rating_str):
        try:
            current_value_int = round(float(current_index_value))
        except:
            logger.warning(f"Invalid F&G value: {current_index_value}")
            return

        today_str = date.today().strftime("%Y-%m-%d")
        if status['last_alert_date'] != today_str:
            status['last_alert_date'] = today_str
            status['sent_values_today'] = []
            logger.info(f"날짜 변경 감지. 오늘의 발송 목록 초기화: {today_str}")

        if current_value_int <= self.threshold:
            # 극단적 공포 범위(0-25) 내에서 값이 변경될 때만 알림
            if current_value_int not in status['sent_values_today']:
                status['sent_values_today'].append(current_value_int)
                await self._send_telegram_alert(current_value_int, option_5d_ratio, fear_rating_str)
            else:
                # 개선 사항 ④: 반복되는 스킵 정보는 DEBUG 레벨로
                logger.debug(f"Duplicate alert skipped: {current_value_int} (already sent today)") 
        else:
            # 개선 사항 ④: 반복되는 루프 정보는 DEBUG 레벨로
            logger.debug(f"No alert. Score {current_value_int} above threshold ({self.threshold}).")


# =========================================================
# --- [4-1] 시작 시 상태 메시지 발송 (개선 사항 ③ 제외: 데이터 페치 유지) ---
# =========================================================
async def send_startup_message(cnn_fetcher: CnnFearGreedIndexFetcher, alerter: FearGreedAlerter):
    """
    ⚠️ Render 부팅 지연의 원인이 됩니다. 부팅 후 Health Check 실패 가능성이 높습니다.
    (요청에 따라 데이터 페치 로직을 유지합니다.)
    """
    if not alerter.token or not alerter.chat_id or not HTTP_SESSION: # HTTP_SESSION 유효성 추가 체크
        logger.error("Telegram credentials or HTTP_SESSION missing. Skipping startup message.")
        return

    # 데이터 가져오기를 시작 메시지 발송 전에 실행 (부팅 지연 유발)
    success = await cnn_fetcher.fetch_data()
    if success:
        fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
    else:
        fg_score, fg_rating, pc_value, pc_rating = ERROR_SCORE_VALUE, ERROR_RATING_STR, ERROR_VALUE, ERROR_RATING_STR

    message_text = (f"🚀 공포/탐욕 모니터링 시작 (느린 부팅 버전) 🚀\n\n"
            f"현재 공포/탐욕 지수: {fg_score:.2f} ({fg_rating})\n"
            f"5-day average put/call ratio: {pc_value:.4f}\n"
            f"모니터링 주기: {MONITOR_INTERVAL_SECONDS}초\n\n"
            f"발송 일시: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
    

    payload = {'chat_id': alerter.chat_id, 'text': message_text, 'parse_mode': 'Markdown'}
    try:
        # 전역 세션 사용 (개선 사항 ①)
        async with HTTP_SESSION.post(alerter.api_url, data=payload, timeout=5) as resp:
            resp.raise_for_status()
            logger.info("정상 시작 메시지 발송 성공")
    except Exception as e:
        logger.error(f"정상 시작 메시지 발송 실패: {e}")

# =========================================================
# --- [5] 서버 슬립 방지 루프 (안정성 강화) ---
# =========================================================
async def self_ping_loop():
    if not SELF_PING_URL:
        logger.warning("Self-Ping URL이 설정되지 않아 슬립 방지 루프를 시작하지 않습니다.")
        return
    
    # 세션 초기화 대기 (안정성 강화)
    while HTTP_SESSION is None:
        logger.debug("Self-Ping: Waiting for HTTP_SESSION initialization...")
        await asyncio.sleep(1)

    logger.info("--- 서버 유휴 상태 방지 (Self-Ping) 루프 시작 ---")
    failure_count = 0 # 개선 사항 ②: 연속 실패 횟수 카운터
    
    while True:
        await asyncio.sleep(SELF_PING_INTERVAL_SECONDS) # 주기적으로 대기
        
        try:
            # 전역 세션 사용 (개선 사항 ①)
            async with HTTP_SESSION.get(SELF_PING_URL, timeout=5) as resp: 
                status_code = resp.status
                if status_code == 200:
                    failure_count = 0
                    # 개선 사항 ④: 반복적인 성공 정보는 DEBUG 레벨로
                    logger.debug(f"Self-Ping 성공 ({status_code}). 서버 유휴 타이머 리셋됨.") 
                else:
                    raise Exception(f"Non-200 status received: {status_code}")
                    
        except asyncio.TimeoutError:
            failure_count += 1
            logger.warning(f"Self-Ping 시간 초과 (Timeout Error). 실패 횟수: {failure_count}/{MAX_PING_FAILURES}")
        except Exception as e:
            failure_count += 1
            logger.error(f"Self-Ping 오류: {e}. 실패 횟수: {failure_count}/{MAX_PING_FAILURES}")
            
        # 개선 사항 ②: 연속 실패 횟수 초과 시 루프 중단
        if failure_count >= MAX_PING_FAILURES:
            logger.critical("🚨 최대 연속 Self-Ping 실패 횟수 도달. 무한 루프 방지를 위해 루프 중단.")
            break

# =========================================================
# --- [6] 메인 모니터링 루프 (백그라운드 작업용) ---
# =========================================================
async def main_monitor_loop():
    logging.info("--- F&G 모니터링 프로그램 (백그라운드) 시작 ---")
    cnn_fetcher = CnnFearGreedIndexFetcher()
    alerter = FearGreedAlerter(TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET_CHAT_ID, FEAR_THRESHOLD)
    
    # 세션 초기화 대기 (안정성 강화)
    while HTTP_SESSION is None:
        logger.debug("Monitor: Waiting for HTTP_SESSION initialization...")
        await asyncio.sleep(1)

    # 시작 시 한 번 발송 (⚠️ 이 부분이 부팅 지연의 원인입니다.)
    await send_startup_message(cnn_fetcher, alerter)

    while True:
        # 개선 사항 ④: 반복되는 루프 정보는 DEBUG 레벨로
        logger.debug(f"--- 데이터 체크 시작 ({MONITOR_INTERVAL_SECONDS}s 주기) ---")
        try:
            # send_startup_message에서 이미 fetch_data를 한 번 호출했으므로,
            # 여기서는 다음 주기부터 정상적으로 데이터 페치 및 알림을 수행합니다.
            if await cnn_fetcher.fetch_data():
                fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
                logger.info(f"F&G 점수: {fg_score:.2f} ({fg_rating}), P/C 값: {pc_value:.4f}")
                await alerter.check_and_alert(fg_score, pc_value, pc_rating)
            else:
                 logger.warning("CNN 데이터 획득 실패. 알림 프로세스 건너뜀.")
        except Exception as e:
            logger.error(f"모니터링 루프 중 오류: {e}")
        
        # 주기적인 대기
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)

# =========================================================
# --- [7] FastAPI 웹 서비스 설정 ---
# =========================================================
app = FastAPI(
    title="Fear & Greed Monitor (Unoptimized Startup)",
    description="CNN Fear & Greed Index monitor, intentionally keeping the slow startup process.",
    version="1.0.0"
)

# 서버 시작 시 백그라운드 작업 시작
@app.on_event("startup")
async def startup_event():
    """
    개선 사항 ①: 전역 HTTP 세션 초기화
    Self-Ping 루프를 백그라운드 태스크로 추가
    """
    global HTTP_SESSION
    logger.info("FastAPI Server Startup: Initializing HTTP Session and launching background tasks.")
    
    # 1. 전역 aiohttp 세션 초기화 (개선 사항 ①)
    HTTP_SESSION = aiohttp.ClientSession(headers=HEADERS) 

    # 2. 모니터링 루프를 독립적인 비동기 작업으로 실행
    asyncio.create_task(main_monitor_loop())
    
    # 3. 서버 슬립 방지 루프를 독립적인 비동기 작업으로 실행 (개선 사항 ②)
    asyncio.create_task(self_ping_loop())

@app.on_event("shutdown")
async def shutdown_event():
    """전역 HTTP 세션을 안전하게 닫습니다. (개선 사항 ①)"""
    global HTTP_SESSION
    if HTTP_SESSION and not HTTP_SESSION.closed:
        await HTTP_SESSION.close()
        logger.info("HTTP Session closed successfully.")

# Health Check Endpoint
@app.get("/")
async def health_check():
    return {
        "status": "running", 
        "message": "F&G monitor and self-ping are active in the background. Stability optimizations applied.",
        "last_alert_date": status.get('last_alert_date'),
        "sent_values_today": status.get('sent_values_today'),
        "ping_url_active": SELF_PING_URL is not None
    }

# =========================================================
# --- [8] 실행 ---
# =========================================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    
    logger.info(f"Starting uvicorn server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
