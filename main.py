import os
import asyncio
import random
import string
import logging
import json
import time
from datetime import datetime, timedelta
from collections import deque
import httpx
from fastapi import FastAPI
import uvicorn

# ============================================================
#  ██████╗ ██████╗ ███╗   ██╗███████╗██╗ ██████╗
# ██╔════╝██╔═══██╗████╗  ██║██╔════╝██║██╔════╝
# ██║     ██║   ██║██╔██╗ ██║█████╗  ██║██║  ███╗
# ██║     ██║   ██║██║╚██╗██║██╔══╝  ██║██║   ██║
# ╚██████╗╚██████╔╝██║ ╚████║██║     ██║╚██████╔╝
#  ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝╚═╝     ╚═╝ ╚═════╝
#  Username Hunter Agent — Instagram + Snapchat + Twitter
#  مشروع تخرج | نظام صيد اليوزرات الذكي
# ============================================================

# ====== الإعدادات من البيئة ======
WEBHOOK_URL   = os.getenv("WEBHOOK_URL", "")
DELAY_MIN     = float(os.getenv("DELAY_MIN", 20))
DELAY_MAX     = float(os.getenv("DELAY_MAX", 45))
USERNAME_LEN  = int(os.getenv("USERNAME_LEN", 4))   # 3 أو 4
IG_SESSION    = os.getenv("IG_SESSION", "")          # اختياري - سيشن انستا
TW_BEARER     = os.getenv("TW_BEARER", "")          # اختياري - توكن تويتر

# ====== Logging ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("Hunter")

# ====== الإحصائيات العامة ======
stats = {
    "total_scanned": 0,
    "found": [],          # قائمة الصيدات
    "last_user": "—",
    "start_time": datetime.now(),
    "rate_limits": {"instagram": 0, "snapchat": 0, "twitter": 0},
    "errors": 0,
}

# ====== معرف رسالة الداشبورد ======
dashboard_msg_id = None

# ============================================================
# 🧠 SMART RATE LIMITER — الدماغ الذكي
# ============================================================
class SmartRateLimiter:
    """
    يتعلم من كل Rate Limit ويكيف نفسه تلقائياً.
    كل منصة مستقلة — لو انستا اتحجبت، سناب وتويتر يكملون.
    """
    def __init__(self, name: str):
        self.name = name
        self.blocked_until = datetime.now()
        self.hit_count = 0
        self.backoff = 60        # ثواني انتظار أول حظر
        self.max_backoff = 600   # 10 دقائق أقصى انتظار
        self.history = deque(maxlen=10)  # آخر 10 استجابات

    def is_blocked(self) -> bool:
        return datetime.now() < self.blocked_until

    def seconds_left(self) -> float:
        delta = (self.blocked_until - datetime.now()).total_seconds()
        return max(0, delta)

    def register_limit(self, retry_after: float = None):
        """سجّل حظر وزيد الـ backoff ذكياً"""
        self.hit_count += 1
        stats["rate_limits"][self.name] += 1

        if retry_after:
            wait = retry_after + 5
        else:
            # backoff تصاعدي: كل حظر يضاعف وقت الانتظار
            wait = min(self.backoff * (1.5 ** self.hit_count), self.max_backoff)

        self.blocked_until = datetime.now() + timedelta(seconds=wait)
        log.warning(f"⏳ [{self.name}] Rate Limited — انتظر {wait:.0f}s (hit #{self.hit_count})")

    def register_success(self):
        """لو نجح الطلب، قلّل الـ backoff تدريجياً"""
        self.history.append(1)
        if self.hit_count > 0 and len(self.history) >= 5 and sum(self.history) == 5:
            self.hit_count = max(0, self.hit_count - 1)

    def register_failure(self):
        self.history.append(0)

# ============================================================
# 🎲 USERNAME GENERATOR — مولّد اليوزرات
# ============================================================
CHARS = string.ascii_lowercase + string.digits

def generate_username(length: int = None) -> str:
    """
    يولّد يوزر عشوائي بأنواع متعددة:
    - حروف وأرقام فقط
    - مع نقطة أو underscore (نادر وثمين)
    """
    if length is None:
        length = random.choice([3, 4, USERNAME_LEN])

    mode = random.choices(
        ["plain", "dot", "underscore"],
        weights=[70, 15, 15]
    )[0]

    if mode == "plain":
        return "".join(random.choices(CHARS, k=length))
    elif mode == "dot":
        if length < 3:
            return "".join(random.choices(CHARS, k=length))
        pos = random.randint(1, length - 2)
        chars = list(random.choices(CHARS, k=length))
        chars[pos] = "."
        return "".join(chars)
    else:
        if length < 3:
            return "".join(random.choices(CHARS, k=length))
        pos = random.randint(1, length - 2)
        chars = list(random.choices(CHARS, k=length))
        chars[pos] = "_"
        return "".join(chars)

# ============================================================
# 📡 PLATFORM AGENTS — عمال الفحص
# ============================================================

class InstagramAgent:
    """فاحص انستقرام — يستخدم فحص الـ URL"""
    def __init__(self):
        self.limiter = SmartRateLimiter("instagram")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        if IG_SESSION:
            self.headers["Cookie"] = f"sessionid={IG_SESSION}"

    async def check(self, username: str, client: httpx.AsyncClient) -> str:
        """
        يرجع: 'available' | 'taken' | 'rate_limit' | 'error'
        """
        if self.limiter.is_blocked():
            return "rate_limit"

        try:
            url = f"https://www.instagram.com/{username}/"
            r = await client.get(url, headers=self.headers, timeout=10, follow_redirects=True)

            if r.status_code == 404:
                self.limiter.register_success()
                return "available"
            elif r.status_code == 200:
                self.limiter.register_success()
                return "taken"
            elif r.status_code == 429:
                retry = float(r.headers.get("Retry-After", 60))
                self.limiter.register_limit(retry)
                return "rate_limit"
            elif r.status_code in [302, 301]:
                # redirect لصفحة login = اليوزر موجود غالباً
                self.limiter.register_success()
                return "taken"
            else:
                self.limiter.register_failure()
                return "error"

        except httpx.TimeoutException:
            log.debug(f"[Instagram] Timeout على {username}")
            return "error"
        except Exception as e:
            log.debug(f"[Instagram] خطأ: {e}")
            stats["errors"] += 1
            return "error"


class SnapchatAgent:
    """فاحص سناب شات"""
    def __init__(self):
        self.limiter = SmartRateLimiter("snapchat")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    async def check(self, username: str, client: httpx.AsyncClient) -> str:
        if self.limiter.is_blocked():
            return "rate_limit"

        try:
            url = f"https://www.snapchat.com/add/{username}"
            r = await client.get(url, headers=self.headers, timeout=10, follow_redirects=False)

            if r.status_code == 404:
                self.limiter.register_success()
                return "available"
            elif r.status_code in [200, 301, 302]:
                self.limiter.register_success()
                # 200 يعني الصفحة موجودة = اليوزر مأخوذ
                return "taken"
            elif r.status_code == 429:
                self.limiter.register_limit()
                return "rate_limit"
            else:
                self.limiter.register_failure()
                return "error"

        except httpx.TimeoutException:
            return "error"
        except Exception as e:
            log.debug(f"[Snapchat] خطأ: {e}")
            stats["errors"] += 1
            return "error"


class TwitterAgent:
    """فاحص تويتر/X"""
    def __init__(self):
        self.limiter = SmartRateLimiter("twitter")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        if TW_BEARER:
            self.headers["Authorization"] = f"Bearer {TW_BEARER}"

    async def check(self, username: str, client: httpx.AsyncClient) -> str:
        if self.limiter.is_blocked():
            return "rate_limit"

        try:
            url = f"https://x.com/{username}"
            r = await client.get(url, headers=self.headers, timeout=10, follow_redirects=True)

            if r.status_code == 404:
                self.limiter.register_success()
                return "available"
            elif r.status_code == 200:
                self.limiter.register_success()
                return "taken"
            elif r.status_code == 429:
                retry = float(r.headers.get("Retry-After", 120))
                self.limiter.register_limit(retry)
                return "rate_limit"
            else:
                self.limiter.register_failure()
                return "error"

        except httpx.TimeoutException:
            return "error"
        except Exception as e:
            log.debug(f"[Twitter] خطأ: {e}")
            stats["errors"] += 1
            return "error"


# ============================================================
# 🔔 DISCORD NOTIFIER — إشعارات ديسكورد
# ============================================================

async def send_catch_alert(username: str, platforms: list):
    """
    يرسل إشعار منفصل لما يلقى صيدة.
    رسالة واضحة مع منشن @everyone.
    """
    if not WEBHOOK_URL:
        return

    platform_icons = {"instagram": "📸", "snapchat": "👻", "twitter": "🐦"}
    platform_str = " + ".join(
        f"{platform_icons.get(p, '✅')} {p.capitalize()}" for p in platforms
    )

    payload = {
        "content": f"@everyone",
        "embeds": [{
            "title": f"🎯 صيدة جديدة: `{username}`",
            "description": f"اليوزر **`{username}`** متاح على:\n{platform_str}",
            "color": 0x00FF88,
            "fields": [
                {"name": "⚡ سجّله الحين قبل ما أحد يسبقك!", "value": "\u200b", "inline": False}
            ],
            "footer": {"text": f"Username Hunter • {datetime.now().strftime('%H:%M:%S')}"}
        }]
    }

    try:
        async with httpx.AsyncClient() as client:
            await client.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        log.warning(f"⚠️ فشل إرسال إشعار الصيدة: {e}")


async def update_dashboard():
    """
    داشبورد حي — رسالة وحيدة تتحدث كل 15 ثانية.
    ما يرسل رسايل جديدة = ما نتبند من ديسكورد.
    """
    global dashboard_msg_id

    if not WEBHOOK_URL:
        log.warning("⚠️ WEBHOOK_URL مو موجود — الداشبورد مو شغال")
        return

    while True:
        # حساب الإحصائيات
        uptime = datetime.now() - stats["start_time"]
        uptime_str = f"{int(uptime.total_seconds() // 3600)}h {int((uptime.total_seconds() % 3600) // 60)}m"
        scan_rate = stats["total_scanned"] / max(uptime.total_seconds() / 60, 1)

        # آخر 5 صيدات
        recent_finds = stats["found"][-5:] if stats["found"] else []
        finds_str = "\n".join(
            f"`{f['username']}` على {', '.join(f['platforms'])}"
            for f in reversed(recent_finds)
        ) or "لا توجد بعد..."

        # حالة المنصات
        def platform_status(name, agent):
            if agent.limiter.is_blocked():
                left = agent.limiter.seconds_left()
                return f"⏳ محجوب ({left:.0f}s)"
            return "🟢 يعمل"

        payload = {
            "embeds": [{
                "title": "🎯 Username Hunter — لوحة التحكم",
                "color": 0x5865F2,
                "fields": [
                    {
                        "name": "📊 الإحصائيات",
                        "value": (
                            f"```\n"
                            f"إجمالي الفحص : {stats['total_scanned']:,}\n"
                            f"الصيدات      : {len(stats['found'])}\n"
                            f"معدل الفحص   : {scan_rate:.1f}/دقيقة\n"
                            f"وقت التشغيل  : {uptime_str}\n"
                            f"أخطاء        : {stats['errors']}\n"
                            f"```"
                        ),
                        "inline": False
                    },
                    {
                        "name": "📡 حالة المنصات",
                        "value": (
                            f"📸 Instagram : {platform_status('instagram', ig_agent)}\n"
                            f"👻 Snapchat  : {platform_status('snapchat', sc_agent)}\n"
                            f"🐦 Twitter   : {platform_status('twitter', tw_agent)}"
                        ),
                        "inline": True
                    },
                    {
                        "name": "⏳ Rate Limits",
                        "value": (
                            f"IG: {stats['rate_limits']['instagram']}x\n"
                            f"SC: {stats['rate_limits']['snapchat']}x\n"
                            f"TW: {stats['rate_limits']['twitter']}x"
                        ),
                        "inline": True
                    },
                    {
                        "name": "🎯 آخر الصيدات",
                        "value": finds_str,
                        "inline": False
                    },
                    {
                        "name": "🔍 آخر يوزر فُحص",
                        "value": f"`{stats['last_user']}`",
                        "inline": False
                    }
                ],
                "footer": {
                    "text": f"آخر تحديث: {datetime.now().strftime('%H:%M:%S')} • يتحدث كل 15 ثانية"
                }
            }]
        }

        try:
            async with httpx.AsyncClient() as client:
                if not dashboard_msg_id:
                    r = await client.post(
                        f"{WEBHOOK_URL}?wait=true",
                        json=payload, timeout=10
                    )
                    if r.status_code in [200, 204]:
                        dashboard_msg_id = r.json().get("id")
                        log.info(f"✅ داشبورد أُنشئ — ID: {dashboard_msg_id}")
                else:
                    await client.patch(
                        f"{WEBHOOK_URL}/messages/{dashboard_msg_id}",
                        json=payload, timeout=10
                    )
        except Exception as e:
            log.warning(f"⚠️ خطأ في تحديث الداشبورد: {e}")

        await asyncio.sleep(15)


# ============================================================
# 🤖 MAIN AGENT — الأجينت الرئيسي
# ============================================================

# إنشاء العمال (يُستخدمون في الداشبورد أيضاً)
ig_agent = InstagramAgent()
sc_agent = SnapchatAgent()
tw_agent = TwitterAgent()


async def scan_username(username: str, client: httpx.AsyncClient):
    """
    يفحص يوزر على الثلاث منصات بشكل متوازي.
    كل منصة مستقلة — لو وحدة تأخرت، الباقين يكملون.
    """
    stats["total_scanned"] += 1
    stats["last_user"] = username

    # فحص متوازي على الثلاث منصات في نفس الوقت
    results = await asyncio.gather(
        ig_agent.check(username, client),
        sc_agent.check(username, client),
        tw_agent.check(username, client),
        return_exceptions=True
    )

    ig_result, sc_result, tw_result = results

    # اجمع المنصات المتاحة
    available_platforms = []
    if ig_result == "available":
        available_platforms.append("instagram")
    if sc_result == "available":
        available_platforms.append("snapchat")
    if tw_result == "available":
        available_platforms.append("twitter")

    # لو لقى صيدة
    if available_platforms:
        catch = {"username": username, "platforms": available_platforms, "time": datetime.now().strftime("%H:%M:%S")}
        stats["found"].append(catch)

        platforms_str = " + ".join(p.capitalize() for p in available_platforms)
        log.info(f"🎯 صيدة! [{username}] متاح على: {platforms_str}")

        await send_catch_alert(username, available_platforms)
    else:
        # لوق تفصيلي للمطورين
        log.info(
            f"🔍 [{username}] "
            f"IG:{ig_result[:2].upper()} "
            f"SC:{sc_result[:2].upper()} "
            f"TW:{tw_result[:2].upper()}"
        )


async def hunter_loop():
    """الحلقة الرئيسية — تشتغل إلى الأبد"""
    log.info("🚀 Username Hunter بدأ يشتغل!")
    log.info(f"⚙️  إعدادات: تأخير {DELAY_MIN}-{DELAY_MAX}s | طول اليوزر {USERNAME_LEN}")

    # HTTP client مشترك للكفاءة
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        timeout=httpx.Timeout(15.0)
    ) as client:
        while True:
            username = generate_username(USERNAME_LEN)
            await scan_username(username, client)

            # تأخير عشوائي لتجنب الحظر
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            await asyncio.sleep(delay)


# ============================================================
# 🌐 FastAPI — عشان Render ما يوقفنا
# ============================================================
app = FastAPI(title="Username Hunter", version="2.0")

@app.get("/")
async def health():
    uptime = (datetime.now() - stats["start_time"]).total_seconds()
    return {
        "status": "running",
        "scanned": stats["total_scanned"],
        "found": len(stats["found"]),
        "uptime_seconds": int(uptime),
        "last_user": stats["last_user"],
    }

@app.get("/stats")
async def get_stats():
    return {
        **stats,
        "found": stats["found"][-20:],  # آخر 20 صيدة
        "uptime": str(datetime.now() - stats["start_time"]).split(".")[0],
    }


# ============================================================
# 🏁 التشغيل
# ============================================================
async def main():
    if not WEBHOOK_URL:
        log.warning("⚠️ WEBHOOK_URL مو موجود — الإشعارات مو شغالة")

    # شغّل كل شيء مع بعض
    asyncio.create_task(hunter_loop())
    asyncio.create_task(update_dashboard())

    port = int(os.getenv("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
