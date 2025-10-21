import asyncio
import aiohttp
import json
import logging
import os
import sys
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any, Tuple
from zoneinfo import ZoneInfo # <--- 추가: zoneinfo 모듈 사용

# FastAPI 및 uvicorn import (웹 서비스 구동을 위해 필요)
from fastapi import FastAPI, Request
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

# ⚠️ 환경 변수에서 2개의 텔레그램 채팅방 ID 로드
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_TARGET_CHAT_ID = os.environ.get('TELEGRAM_TARGET_CHAT_ID')  # [채널 1] 조건부 알림 (5분)
TELEGRAM_TARGET_CHAT_ID_REPORT = os.environ.get('TELEGRAM_TARGET_CHAT_ID_REPORT') # [채널 2] 정기 보고 (10분)

# Render에서 제공하는 외부 호스트 이름 (슬립 방지용)
SELF_PING_HOST = os.environ.get('RENDER_EXTERNAL_HOSTNAME')

# 🇰🇷 한국 표준시 (KST) 정의
KST = ZoneInfo("Asia/Seoul")

FEAR_THRESHOLD = 25
# [채널 1] 조건부 알림 주기: 5분
MONITOR_INTERVAL_SECONDS = 300 
# [채널 2] 정기 보고 주기: 10분
REPORT_INTERVAL_SECONDS = 60 * 10 

# 서버 RAM에서 상태 유지 (채널 1의 조건부 알림 로직 유지를 위해 필요)
status = {"last_alert_date": "1970-01-01", "sent_values_today": []}

ERROR_SCORE_VALUE = 100.00
ERROR_VALUE = 100.0000
ERROR_RATING_STR = "데이터 오류"

# 텔레그램 및 슬립 방지 설정 검사
if not TELEGRAM_BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN 환경 변수가 설정되지 않았습니다. 알림이 작동하지 않습니다.")

if not TELEGRAM_TARGET_CHAT_ID and not TELEGRAM_TARGET_CHAT_ID_REPORT:
    logging.error("TELEGRAM_TARGET_CHAT_ID 와 TELEGRAM_TARGET_CHAT_ID_REPORT 모두 설정되지 않았습니다.")
elif not TELEGRAM_TARGET_CHAT_ID:
    logging.warning("[채널 1] TELEGRAM_TARGET_CHAT_ID가 없어 조건부 알림은 작동하지 않습니다.")
elif not TELEGRAM_TARGET_CHAT_ID_REPORT:
    logging.warning("[채널 2] TELEGRAM_TARGET_CHAT_ID_REPORT가 없어 정기 보고는 작동하지 않습니다.")

# Self-Ping URL 설정
if not SELF_PING_HOST:
    logging.warning("RENDER_EXTERNAL_HOSTNAME 환경 변수가 설정되지 않았습니다. 슬립 방지 기능이 작동하지 않을 수 있습니다.")
    SELF_PING_URL = None
else:
    SELF_PING_URL = f"https://{SELF_PING_HOST}/"
    logging.info(f"Self-Ping URL 설정 완료: {SELF_PING_URL}")
    SELF_PING_INTERVAL_SECONDS = 60 * 5 # 5분 간격으로 셀프 핑

# =========================================================
# --- [3] CNN 데이터 가져오기 ---
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
        today = datetime.now(KST).date() # KST 기준으로 날짜 확인
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
                        # 4xx, 5xx 에러 발생 시 예외 처리
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
                    # IP 차단, 연결 오류 등을 여기서 포착하여 로그에 기록
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
# --- [4] Telegram 알림 관련 함수 및 클래스 ---
# =========================================================

# 범용 메시지 발송 함수 (재시도 로직 포함)
async def _send_telegram_message(token: str, chat_id: str, message_text: str, log_description: str):
    if not token or not chat_id:
        logging.error(f"Telegram credentials missing for {log_description}. Skipping send.")
        return

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': message_text, 'parse_mode': 'Markdown'}
    
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, data=payload, timeout=10) as resp:
                    resp.raise_for_status()
                    
                    # 🟡 [정기 보고 성공] WARNING 레벨로 변경 (사용자 요청)
                    if log_description == "정기 보고":
                        logging.warning(f"🟡 [정기 보고] 텔레그램 발송 성공 완료")
                    # 🟢 [조건부 알림 성공] INFO 레벨 유지
                    elif log_description == "조건부 알림":
                        logging.info(f"[{log_description}] 텔레그램 발송 성공.")
                    # 🔵 [시작 메시지 등 기타] INFO 레벨 유지
                    else: 
                        logging.info(f"[{log_description}] 텔레그램 발송 성공.")
                        
                    return
        except Exception as e:
            # 🔴 [모든 채널 최종 실패] ERROR 레벨 (빨간색)
            if attempt == 2:
                logging.error(f"🔴 [FINAL FAIL] [{log_description}] 텔레그램 발송 최종 실패: {e}")
                return
            
            # 일반 실패 경고는 WARNING 레벨 유지 (주황/노란색)
            logging.warning(f"[{log_description}] 텔레그램 발송 실패 (시도 {attempt + 1}/3): {e}. 잠시 후 재시도.")
            await asyncio.sleep(2 ** attempt)
            
    # 최종 실패는 위의 attempt == 2에서 처리되므로 여기는 도달하지 않음


# [클래스 1: 조건부 알림] (채널 1: 5분 주기, F&G <= 25일 때, 동일 값 중복 방지)
class ConditionalAlerter:
    def __init__(self, token: str, chat_id: str, threshold: int):
        self.token = token
        self.chat_id = chat_id
        self.threshold = threshold

    async def _send_alert_message(self, current_value: int, option_5d_ratio: float, fear_rating_str: str):
        pc_ratio_str = f"{option_5d_ratio:.4f}"
        
        # 🇰🇷 KST 시간 적용 (zoneinfo 사용)
        kst_time = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
        
        message_text = (
            f"🚨 극단적 공포 알림 (5분 조건부) 🚨\n\n"
            f"공포/탐욕: `극단적 공포(Extreme Fear)`\n"
            f"현재 지수: **{current_value}**\n\n"
            f"PUT AND CALL OPTIONS: `{fear_rating_str}`\n"
            f"5-day average put/call ratio: **{pc_ratio_str}**\n\n"
            f"발송 일시: {kst_time} KST" # <--- KST 표시
        )
        await _send_telegram_message(self.token, self.chat_id, message_text, "조건부 알림")

    async def check_and_alert(self, current_index_value, option_5d_ratio, fear_rating_str):
        if not self.chat_id:
            return

        try:
            current_value_int = round(float(current_index_value))
        except:
            logging.warning(f"Invalid F&G value: {current_index_value}")
            return

        # 🇰🇷 KST 기준으로 오늘 날짜 확인
        today_str = datetime.now(KST).strftime("%Y-%m-%d")
        if status['last_alert_date'] != today_str:
            status['last_alert_date'] = today_str
            status['sent_values_today'] = []
            logging.info(f"[조건부] 날짜 변경 감지. 오늘의 발송 목록 초기화: {today_str}")

        if current_value_int <= self.threshold:
            # 극단적 공포 범위(0-25) 내에서 값이 변경될 때만 알림
            if current_value_int not in status['sent_values_today']:
                status['sent_values_today'].append(current_value_int)
                await self._send_alert_message(current_value_int, option_5d_ratio, fear_rating_str)
            else:
                # 🔴 [중복 차단] ERROR 레벨로 변경 (사용자 요청)
                logging.error(f"🔴 [조건부 차단] Duplicate alert blocked: {current_value_int} (already sent today)")
        else:
            # 🔴 [조건 미충족] ERROR 레벨로 변경 (사용자 요청)
            logging.error(f"🔴 [조건 미충족] Alert skip. Score {current_value_int} above threshold ({self.threshold}).")


# [클래스 2: 정기 보고] (채널 2: 10분 주기, 조건 없이 무조건 발송)
class PeriodicReporter:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        
    async def _send_report_message(self, fg_score: float, fg_rating: str, pc_value: float, pc_rating: str):
        pc_ratio_str = f"{pc_value:.4f}"
        
        # 🇰🇷 KST 시간 적용 (zoneinfo 사용)
        kst_time = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
        
        # 이전 응답에서 추가된, 점수에 따른 제목/내용 강조 로직을 유지합니다.
        if fg_score <= FEAR_THRESHOLD:
            # 25 이하일 때 (극단적 공포)
            title = "🚨 [긴급] 10분 주기 지수 보고 🚨"
            fg_score_str = f"**🔥 {fg_score:.2f} 🔥**" # 점수 빨간색으로 하이라이트 효과
            fg_rating_str = f"`❗ {fg_rating} ❗`"
        else:
            # 26 이상일 때 (일반)
            title = "📊 10분 주기 지수 보고 📊"
            fg_score_str = f"**{fg_score:.2f}**"
            fg_rating_str = f"`{fg_rating}`"

        message_text = (
            f"{title}\n\n" 
            f"➡️ FEAR & GREED INDEX: {fg_score_str}\n"
            f"   - Rating: {fg_rating_str}\n\n"
            f"➡️ PUT AND CALL OPTIONS:\n"
            f"   - Rating: `{pc_rating}`\n"
            f"   - P/C Ratio (5-day avg): **{pc_ratio_str}**\n\n"
            f"발송 일시: {kst_time} KST" # <--- KST 표시
        )
        await _send_telegram_message(self.token, self.chat_id, message_text, "정기 보고")

    async def send_report(self, fg_score: float, fg_rating: str, pc_value: float, pc_rating: str):
        if self.chat_id:
            await self._send_report_message(fg_score, fg_rating, pc_value, pc_rating)


# =========================================================
# --- [4-1] 시작 시 상태 메시지 발송 (각 채널에 맞춰 분리) ---
# =========================================================
async def send_startup_message(conditional_alerter: ConditionalAlerter, periodic_reporter: PeriodicReporter):
    
    cnn_fetcher = CnnFearGreedIndexFetcher()
    success = await cnn_fetcher.fetch_data()

    if success:
        fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
    else:
        fg_score, fg_rating, pc_value, pc_rating = ERROR_SCORE_VALUE, ERROR_RATING_STR, ERROR_VALUE, ERROR_RATING_STR

    # 🇰🇷 KST 시간 적용 (zoneinfo 사용)
    kst_time = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    
    common_info = (f"현재 F&G 지수: {fg_score:.2f} ({fg_rating})\n"
                   f"P/C Ratio (5일): {pc_value:.4f}\n"
                   f"서버 시작: {kst_time} KST") # <--- KST 표시

    # [채널 1] 조건부 알림 채널에 전용 시작 메시지 발송
    if conditional_alerter.chat_id:
        message_ch1 = (f"🚀 공포/탐욕 모니터링 시작 🚀\n\n"
                        f"현재 공포/탐욕 지수: {fg_score:.2f} ({fg_rating})\n"
                        f"5-day average put/call ratio: {pc_value:.4f}\n"
                        f"모니터링 주기: {MONITOR_INTERVAL_SECONDS}초\n\n"
                        f"서버 시작: {kst_time} KST" # <--- KST 표시
                    )
        await _send_telegram_message(conditional_alerter.token, conditional_alerter.chat_id, message_ch1, "시작 메시지_CH1")

    # [채널 2] 정기 보고 채널에 전용 시작 메시지 발송
    if periodic_reporter.chat_id:
        message_ch2 = (f"📈 정기 보고 모니터링 시작 📈\n\n"
                       f"✅ 주기: {REPORT_INTERVAL_SECONDS}초\n"
                       f"✅ 발송 조건: 무조건 발송\n"
                       f"{common_info}"
                       )
        # 시작 메시지는 INFO 레벨로 출력
        await _send_telegram_message(periodic_reporter.token, periodic_reporter.chat_id, message_ch2, "시작 메시지_CH2")


# =========================================================
# --- [5] 서버 슬립 방지 루프 ---
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
                        logging.info(f"Self-Ping 성공 ({status_code}). 서버 유휴 타이머 리셋됨.")
                    else:
                        logging.warning(f"Self-Ping 비정상 응답 ({status_code}).")
                    
        except asyncio.TimeoutError:
            logging.warning("Self-Ping 시간 초과 (Timeout Error).")
        except Exception as e:
            logging.error(f"Self-Ping 오류: {e}")


# =========================================================
# --- [6] 메인 모니터링 루프 (채널 1: 5분 조건부 알림) ---
# =========================================================
async def main_monitor_loop(alerter: ConditionalAlerter):
    logging.info(f"--- F&G 조건부 알림 (백그라운드) 시작. 주기: {MONITOR_INTERVAL_SECONDS}s ---")
    cnn_fetcher = CnnFearGreedIndexFetcher()
    
    while True:
        # [조건부] 알림 체크는 INFO 레벨 유지
        logging.info(f"[조건부] 데이터 체크 시작 ({MONITOR_INTERVAL_SECONDS}s 주기)")
        try:
            if await cnn_fetcher.fetch_data():
                fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
                logging.info(f"[조건부] F&G 점수: {fg_score:.2f} ({fg_rating})")
                await alerter.check_and_alert(fg_score, pc_value, pc_rating)
        except Exception as e:
            logging.error(f"[조건부] 모니터링 루프 중 오류: {e}")
        
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)

# =========================================================
# --- [7] 정기 보고 루프 (채널 2: 10분 무조건 발송) ---
# =========================================================
async def periodic_report_loop(reporter: PeriodicReporter):
    logging.info(f"--- F&G 정기 보고 (백그라운드) 시작. 주기: {REPORT_INTERVAL_SECONDS}s ---")
    cnn_fetcher = CnnFearGreedIndexFetcher()
    
    # 정기 보고는 10분 주기에 맞춰 시작 (예: 10분, 20분...)
    await asyncio.sleep(REPORT_INTERVAL_SECONDS / 2) 

    while True:
        # [정기보고] 데이터 체크는 INFO 레벨 유지
        logging.info(f"[정기보고] 데이터 체크 시작 ({REPORT_INTERVAL_SECONDS}s 주기)")
        try:
            if await cnn_fetcher.fetch_data():
                fg_score, fg_rating, pc_value, pc_rating = cnn_fetcher.get_results()
                logging.info(f"[정기보고] F&G 점수: {fg_score:.2f} ({fg_rating}). 무조건 발송.")
                # [핵심] 조건 없이 발송
                await reporter.send_report(fg_score, fg_rating, pc_value, pc_rating)
        except Exception as e:
            logging.error(f"[정기보고] 모니터링 루프 중 오류: {e}")
        
        await asyncio.sleep(REPORT_INTERVAL_SECONDS)


# =========================================================
# --- [8] FastAPI 웹 서비스 설정 ---
# =========================================================
app = FastAPI(
    title="Fear & Greed Monitor (Dual Channel)",
    description="CNN Fear & Greed Index monitor with dual Telegram channels.",
    version="1.1.2"
)

# 서버 시작 시 백그라운드 작업 시작
@app.on_event("startup")
async def startup_event():
    logging.info("FastAPI Server Startup: Initializing dual background tasks.")
    
    # 1. 알리미 및 리포터 인스턴스 생성
    conditional_alerter = ConditionalAlerter(TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET_CHAT_ID, FEAR_THRESHOLD)
    periodic_reporter = PeriodicReporter(TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET_CHAT_ID_REPORT)
    
    # 2. 시작 메시지 발송 (각 채널에 맞춰 분리)
    await send_startup_message(conditional_alerter, periodic_reporter)
    
    # 3. 백그라운드 루프 실행
    if conditional_alerter.chat_id:
        # 채널 1: 조건부 알림 루프 (5분)
        asyncio.create_task(main_monitor_loop(conditional_alerter))
    
    if periodic_reporter.chat_id:
        # 채널 2: 정기 보고 루프 (10분)
        asyncio.create_task(periodic_report_loop(periodic_reporter))

    # 서버 슬립 방지 루프 실행
    asyncio.create_task(self_ping_loop())


# Health Check Endpoint (Render가 서버가 살아있는지 확인하는 용도)
@app.get("/")
@app.head("/")
async def health_check():
    return {
        "status": "running", 
        "message": "Dual-channel F&G monitor is active in the background.",
        "channel_1_status": {
            "target_chat_id_set": TELEGRAM_TARGET_CHAT_ID is not None,
            "last_alert_date": status.get('last_alert_date'),
            "sent_values_today": status.get('sent_values_today'),
            "description": "5분 주기, F&G <= 25 조건부 발송 (동일 값 중복 방지)"
        },
        "channel_2_status": {
            "target_chat_id_set": TELEGRAM_TARGET_CHAT_ID_REPORT is not None,
            "report_interval_seconds": REPORT_INTERVAL_SECONDS,
            "description": "10분 주기, 조건 없이 무조건 발송"
        },
        "ping_url_active": SELF_PING_URL is not None
    }

# =========================================================
# --- [9] 실행 ---
# =========================================================
if __name__ == '__main__':
    # Render는 환경 변수로 PORT를 제공합니다.
    port = int(os.environ.get("PORT", 8000))
    
    logging.info(f"Starting uvicorn server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
