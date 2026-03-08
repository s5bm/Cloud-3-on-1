import os
import asyncio
import random
import string
import logging
import time
import hashlib
import json
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

# ============================================================
#  USERNAME HUNTER v5.0 — FIXED EDITION
#  المشاكل المحلولة:
#  1. ما يطفي أبداً (keep-alive مدمج)
#  2. فحص حقيقي يرجع نتائج صحيحة
# ============================================================

# =============== CONFIG ===============
WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "")
USERNAME_LEN = int(os.getenv("USERNAME_LEN", 4))
IG_SESSION   = os.getenv("IG_SESSION", "")
TW_BEARER    = os.getenv("TW_BEARER", "")

# تأخير مناسب — مو سريع جداً عشان ما ينبلوك، مو بطيء عشان يلقى نتائج
CHECK_DELAY_MIN = float(os.getenv("DELAY_MIN", 8))
CHECK_DELAY_MAX = float(os.getenv("DELAY_MAX", 15))

# ملف حفظ النتائج عشان ما تضيع لو أعاد التشغيل
RESULTS_FILE = "results.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("Hunter")

# =============== STATS ===============
stats = {
    "total_scanned": 0,
    "found": [],
    "last_user": "—",
    "start_time": None,
    "errors": 0,
    "ig_available": 0,
    "sc_available": 0,
    "tw_available": 0,
    "ig_rate_limits": 0,
    "sc_rate_limits": 0,
    "tw_rate_limits": 0,
    "running": False,
}


# ============================================================
# 💾 حفظ واسترجاع النتائج
# ============================================================
def save_results():
    """يحفظ النتائج في ملف عشان ما تضيع"""
    try:
        data = {"found": stats["found"][-100:], "total_scanned": stats["total_scanned"]}
        Path(RESULTS_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        log.debug(f"Save error: {e}")


def load_results():
    """يسترجع النتائج السابقة"""
    try:
        if Path(RESULTS_FILE).exists():
            data = json.loads(Path(RESULTS_FILE).read_text())
            stats["found"] = data.get("found", [])
            stats["total_scanned"] = data.get("total_scanned", 0)
            log.info(f"📂 استرجعت {len(stats['found'])} نتيجة سابقة")
    except Exception as e:
        log.debug(f"Load error: {e}")


# ============================================================
# 🛡️ VALIDATORS
# ============================================================
ALLOWED = set(string.ascii_lowercase + string.digits + "._")

def valid_ig(u):
    if not u or len(u) < 3 or len(u) > 30:
        return False
    if u[0] in "._" or u[-1] in "._":
        return False
    if ".." in u or "__" in u:
        return False
    return all(c in ALLOWED for c in u)


def valid_tw(u):
    if "." in u or len(u) > 15 or len(u) < 1:
        return False
    allowed = set(string.ascii_lowercase + string.digits + "_")
    return all(c in allowed for c in u)


# ============================================================
# 🔁 DEDUP
# ============================================================
class SeenFilter:
    def __init__(self):
        self.seen = set()

    def is_new(self, username):
        h = hashlib.md5(username.encode()).hexdigest()[:10]
        if h in self.seen:
            return False
        self.seen.add(h)
        if len(self.seen) > 200000:
            # نحذف نصف القديم
            self.seen = set(list(self.seen)[100000:])
        return True

seen = SeenFilter()


# ============================================================
# 🎲 USERNAME GENERATOR
# ============================================================
LETTERS = string.ascii_lowercase
DIGITS = string.digits

def generate_username(length=4):
    """يولد يوزر نيم بأنماط مختلفة"""
    for _ in range(50):
        u = _gen_one(length)
        if valid_ig(u) and seen.is_new(u):
            return u
    # fallback
    u = random.choice(LETTERS) + "".join(random.choices(DIGITS, k=length-1))
    seen.is_new(u)
    return u


def _gen_one(length):
    strategy = random.choices(
        ["l1d3", "l2d2", "dot", "under", "repeat", "d2l2", "l1d2l1"],
        weights=[25, 15, 15, 10, 10, 15, 10]
    )[0]

    if strategy == "l1d3":
        # حرف + 3 أرقام (أعلى نسبة توفر)
        c = [random.choice(LETTERS)] + random.choices(DIGITS, k=3)
        random.shuffle(c)
        return "".join(c)

    elif strategy == "l2d2":
        c = random.choices(LETTERS, k=2) + random.choices(DIGITS, k=2)
        random.shuffle(c)
        return "".join(c)

    elif strategy == "d2l2":
        c = random.choices(DIGITS, k=2) + random.choices(LETTERS, k=2)
        random.shuffle(c)
        return "".join(c)

    elif strategy == "l1d2l1":
        return (random.choice(LETTERS)
                + random.choice(DIGITS)
                + random.choice(DIGITS)
                + random.choice(LETTERS))

    elif strategy == "dot":
        # مثل a.23 أو 3.ab
        left = random.choice(LETTERS + DIGITS)
        right_len = length - 2  # 1 char + dot + rest
        right = "".join(random.choices(LETTERS + DIGITS, k=right_len))
        return left + "." + right

    elif strategy == "under":
        left = random.choice(LETTERS + DIGITS)
        right_len = length - 2
        right = "".join(random.choices(LETTERS + DIGITS, k=right_len))
        return left + "_" + right

    elif strategy == "repeat":
        a = random.choice(LETTERS + DIGITS)
        b = random.choice(LETTERS + DIGITS)
        pattern = random.choice(["aabb", "abab"])
        if pattern == "aabb":
            return a + a + b + b
        else:
            return a + b + a + b

    return random.choice(LETTERS) + "".join(random.choices(DIGITS, k=3))


# ============================================================
# 📡 INSTAGRAM CHECKER — الطريقة الصحيحة
# ============================================================
class InstagramChecker:
    def __init__(self):
        self.blocked_until = 0
        self.hit_count = 0

    def is_blocked(self):
        return time.time() < self.blocked_until

    async def check(self, username, client):
        if self.is_blocked():
            return "skip"

        try:
            # ✅ الطريقة الصحيحة: استخدم web_profile_info API
            # هذا يرجع JSON مباشرة بدون HTML
            headers = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                              "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                              "Version/17.5 Mobile/15E148 Safari/604.1",
                "X-IG-App-ID": "936619743392459",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.instagram.com/",
            }
            if IG_SESSION:
                headers["Cookie"] = f"sessionid={IG_SESSION}"

            r = await client.get(
                f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                headers=headers,
                timeout=12,
            )

            if r.status_code == 404:
                # ✅ 404 = اليوزر فعلاً ما موجود = متاح
                self._success()
                return "available"

            elif r.status_code == 200:
                # موجود — نتأكد من الـ JSON
                try:
                    data = r.json()
                    if data.get("data", {}).get("user"):
                        self._success()
                        return "taken"
                    else:
                        self._success()
                        return "available"
                except Exception:
                    self._success()
                    return "taken"

            elif r.status_code == 429:
                retry = int(r.headers.get("Retry-After", 120))
                self._rate_limit(retry)
                return "rate_limit"

            elif r.status_code in (401, 403):
                # سيشن منتهي أو مطلوب login
                # نجرب الطريقة البديلة
                return await self._fallback_check(username, client)

            else:
                self.hit_count += 1
                return "error"

        except httpx.TimeoutException:
            return "timeout"
        except Exception as e:
            log.debug(f"[IG] {e}")
            stats["errors"] += 1
            return "error"

    async def _fallback_check(self, username, client):
        """طريقة بديلة بدون session"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Linux; Android 14) "
                              "AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
            }
            r = await client.get(
                f"https://www.instagram.com/{username}/",
                headers=headers,
                timeout=10,
                follow_redirects=True,
            )

            body = r.text

            # ✅ الطريقة الصحيحة: نفحص محتوى الصفحة
            if r.status_code == 404:
                return "available"

            # لو الصفحة فيها "Page Not Found" أو ما فيها بيانات يوزر
            if ("page_not_found" in body.lower()
                or '"HttpErrorPage"' in body
                or "Sorry, this page" in body
                or "isn't available" in body):
                return "available"

            # لو فيها بيانات يوزر حقيقية
            if (f'"username":"{username}"' in body
                or f"/{username}/" in body
                or '"edge_followed_by"' in body):
                return "taken"

            # لو ما نقدر نحدد
            return "unknown"

        except Exception:
            return "error"

    def _success(self):
        self.hit_count = max(0, self.hit_count - 1)

    def _rate_limit(self, wait=120):
        self.hit_count += 1
        actual_wait = wait + random.randint(10, 30)
        self.blocked_until = time.time() + actual_wait
        stats["ig_rate_limits"] += 1
        log.warning(f"⏳ [IG] Rate limit — ننتظر {actual_wait}s")


# ============================================================
# 📡 SNAPCHAT CHECKER — الطريقة الصحيحة
# ============================================================
class SnapchatChecker:
    def __init__(self):
        self.blocked_until = 0

    def is_blocked(self):
        return time.time() < self.blocked_until

    async def check(self, username, client):
        if "." in username:
            return "invalid"
        if self.is_blocked():
            return "skip"

        try:
            # ✅ Snapchat Bitmoji API — أدق طريقة
            # لو اليوزر موجود، يكون عنده Bitmoji أو على الأقل profile
            headers = {
                "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                              "AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }

            r = await client.head(
                f"https://www.snapchat.com/add/{username}",
                headers=headers,
                timeout=10,
                follow_redirects=False,
            )

            if r.status_code == 404:
                return "available"
            elif r.status_code in (200, 301, 302):
                # نتحقق أكثر
                r2 = await client.get(
                    f"https://www.snapchat.com/add/{username}",
                    headers=headers,
                    timeout=10,
                    follow_redirects=True,
                )
                body = r2.text
                if ("not found" in body.lower()
                    or "page isn" in body.lower()
                    or "userNotFound" in body
                    or len(body) < 500):
                    return "available"
                return "taken"
            elif r.status_code == 429:
                self.blocked_until = time.time() + 180
                stats["sc_rate_limits"] += 1
                log.warning("⏳ [SC] Rate limit — 180s")
                return "rate_limit"
            else:
                return "unknown"

        except httpx.TimeoutException:
            return "timeout"
        except Exception as e:
            log.debug(f"[SC] {e}")
            return "error"


# ============================================================
# 📡 TWITTER CHECKER
# ============================================================
class TwitterChecker:
    def __init__(self):
        self.blocked_until = 0

    def is_blocked(self):
        return time.time() < self.blocked_until

    async def check(self, username, client):
        if not valid_tw(username):
            return "invalid"
        if self.is_blocked():
            return "skip"

        try:
            if TW_BEARER:
                # ✅ Twitter API v2 — أدق طريقة
                r = await client.get(
                    f"https://api.twitter.com/2/users/by/username/{username}",
                    headers={"Authorization": f"Bearer {TW_BEARER}"},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("data"):
                        return "taken"
                    elif data.get("errors"):
                        for err in data["errors"]:
                            if "not found" in err.get("detail", "").lower():
                                return "available"
                        return "taken"
                    return "taken"
                elif r.status_code == 404:
                    return "available"
                elif r.status_code == 429:
                    reset = float(r.headers.get("x-rate-limit-reset", time.time() + 900))
                    wait = max(reset - time.time(), 60) + 10
                    self.blocked_until = time.time() + wait
                    stats["tw_rate_limits"] += 1
                    log.warning(f"⏳ [TW] Rate limit — {wait:.0f}s")
                    return "rate_limit"
                elif r.status_code == 400:
                    return "invalid"
                else:
                    return "error"
            else:
                # بدون token — نستخدم syndication API
                r = await client.get(
                    f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    },
                    timeout=10,
                )
                if r.status_code == 404:
                    return "available"
                elif r.status_code == 200:
                    return "taken"
                elif r.status_code == 429:
                    self.blocked_until = time.time() + 300
                    stats["tw_rate_limits"] += 1
                    return "rate_limit"
                else:
                    return "unknown"

        except httpx.TimeoutException:
            return "timeout"
        except Exception as e:
            log.debug(f"[TW] {e}")
            return "error"


# ============================================================
# 🔔 DISCORD WEBHOOK
# ============================================================
async def send_alert(username, platforms, client):
    if not WEBHOOK_URL:
        return

    plat_text = " + ".join(p.upper() for p in platforms)
    links = []
    if "instagram" in platforms:
        links.append(f"📸 https://instagram.com/{username}")
    if "snapchat" in platforms:
        links.append(f"👻 https://snapchat.com/add/{username}")
    if "twitter" in platforms:
        links.append(f"🐦 https://x.com/{username}")

    # تقييم بسيط
    score = len(platforms) * 30
    if len(username) <= 4:
        score += 20
    clean = username.replace(".", "").replace("_", "")
    if sum(c.isdigit() for c in clean) >= 3:
        score += 10
    tier = "S" if score >= 80 else "A" if score >= 50 else "B"

    payload = {
        "content": "@everyone" if len(platforms) >= 2 else "",
        "embeds": [{
            "title": f"🎯 صيدة: @{username}",
            "color": 0xFFD700 if tier == "S" else 0x5865F2 if tier == "A" else 0x57F287,
            "fields": [
                {"name": "📱 المنصات", "value": plat_text, "inline": True},
                {"name": "⭐ التقييم", "value": f"{tier}-Tier ({score}pts)", "inline": True},
                {"name": "🔗 الروابط", "value": "\n".join(links), "inline": False},
            ],
            "footer": {"text": f"Hunter v5 • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
        }]
    }

    try:
        await client.post(WEBHOOK_URL, json=payload, timeout=10)
        log.info(f"📨 إشعار أُرسل: @{username}")
    except Exception as e:
        log.warning(f"⚠️ فشل إرسال الإشعار: {e}")


# ============================================================
# 📊 DASHBOARD MESSAGE
# ============================================================
dashboard_msg_id = None

async def update_dashboard_loop(client):
    global dashboard_msg_id
    if not WEBHOOK_URL:
        return

    while True:
        try:
            await _send_dashboard(client)
        except Exception as e:
            log.debug(f"Dashboard error: {e}")
        await asyncio.sleep(20)


async def _send_dashboard(client):
    global dashboard_msg_id

    if not stats["start_time"]:
        return

    uptime = datetime.now() - stats["start_time"]
    hours = int(uptime.total_seconds() // 3600)
    mins = int((uptime.total_seconds() % 3600) // 60)
    rate = stats["total_scanned"] / max(uptime.total_seconds() / 60, 1)

    recent = stats["found"][-5:]
    finds_text = "\n".join(
        f"• `{f['username']}` — {', '.join(f['platforms'])}"
        for f in reversed(recent)
    ) or "ما لقينا شي بعد..."

    ig_status = "🟢" if not ig_checker.is_blocked() else f"🔴 ({int(ig_checker.blocked_until - time.time())}s)"
    sc_status = "🟢" if not sc_checker.is_blocked() else f"🔴 ({int(sc_checker.blocked_until - time.time())}s)"
    tw_status = "🟢" if not tw_checker.is_blocked() else f"🔴 ({int(tw_checker.blocked_until - time.time())}s)"

    payload = {
        "embeds": [{
            "title": "📊 Hunter v5 — Dashboard",
            "color": 0x00FF88 if stats["running"] else 0xFF0000,
            "fields": [
                {
                    "name": "📈 إحصائيات",
                    "value": (
                        f"```\n"
                        f"الفحوصات  : {stats['total_scanned']:,}\n"
                        f"الصيدات   : {len(stats['found'])}\n"
                        f"المعدل    : {rate:.1f}/دقيقة\n"
                        f"التشغيل   : {hours}h {mins}m\n"
                        f"أخطاء     : {stats['errors']}\n"
                        f"```"
                    ),
                    "inline": False,
                },
                {
                    "name": "📡 حالة المنصات",
                    "value": f"📸 IG: {ig_status}\n👻 SC: {sc_status}\n🐦 TW: {tw_status}",
                    "inline": True,
                },
                {
                    "name": "⏳ Rate Limits",
                    "value": f"IG: {stats['ig_rate_limits']}\nSC: {stats['sc_rate_limits']}\nTW: {stats['tw_rate_limits']}",
                    "inline": True,
                },
                {
                    "name": "🎯 آخر الصيدات",
                    "value": finds_text,
                    "inline": False,
                },
                {
                    "name": "🔍 آخر يوزر",
                    "value": f"`{stats['last_user']}`",
                    "inline": False,
                },
            ],
            "footer": {"text": f"آخر تحديث: {datetime.now().strftime('%H:%M:%S')}"},
        }]
    }

    try:
        if not dashboard_msg_id:
            r = await client.post(f"{WEBHOOK_URL}?wait=true", json=payload, timeout=10)
            if r.status_code == 200:
                dashboard_msg_id = r.json().get("id")
        else:
            r = await client.patch(
                f"{WEBHOOK_URL}/messages/{dashboard_msg_id}",
                json=payload,
                timeout=10,
            )
            if r.status_code == 404:
                dashboard_msg_id = None
    except Exception:
        pass


# ============================================================
# 🤖 MAIN HUNTER LOOP
# ============================================================
ig_checker = InstagramChecker()
sc_checker = SnapchatChecker()
tw_checker = TwitterChecker()


async def check_one(username, client):
    """يفحص يوزر واحد على كل المنصات"""
    stats["total_scanned"] += 1
    stats["last_user"] = username

    # فحص متوازي
    ig_task = ig_checker.check(username, client)
    sc_task = sc_checker.check(username, client)
    tw_task = tw_checker.check(username, client)

    results = await asyncio.gather(ig_task, sc_task, tw_task, return_exceptions=True)

    ig_r = results[0] if isinstance(results[0], str) else "error"
    sc_r = results[1] if isinstance(results[1], str) else "error"
    tw_r = results[2] if isinstance(results[2], str) else "error"

    # نجمع المنصات المتاحة
    available = []
    if ig_r == "available":
        available.append("instagram")
        stats["ig_available"] += 1
    if sc_r == "available":
        available.append("snapchat")
        stats["sc_available"] += 1
    if tw_r == "available":
        available.append("twitter")
        stats["tw_available"] += 1

    # حالة الطباعة
    def short(r):
        return {"available": "✅", "taken": "❌", "skip": "⏭️",
                "rate_limit": "⏳", "error": "⚠️", "timeout": "⏰",
                "invalid": "🚫", "unknown": "❓"}.get(r, "?")

    log.info(
        f"🔍 [{username}] "
        f"IG:{short(ig_r)} SC:{short(sc_r)} TW:{short(tw_r)}"
        f"{' 🎯 صيدة!' if available else ''}"
    )

    if available:
        entry = {
            "username": username,
            "platforms": available,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        stats["found"].append(entry)
        save_results()
        await send_alert(username, available, client)


async def hunter_loop():
    """الحلقة الرئيسية — ما توقف أبداً"""
    log.info("=" * 50)
    log.info("🚀 Username Hunter v5 — FIXED EDITION")
    log.info("✅ فحص Instagram API صحيح")
    log.info("✅ فحص Snapchat محسّن")
    log.info("✅ Keep-alive مدمج")
    log.info("=" * 50)

    stats["start_time"] = datetime.now()
    stats["running"] = True
    load_results()

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=15, max_keepalive_connections=5),
        timeout=httpx.Timeout(15.0),
    ) as client:

        # شغل الداشبورد
        asyncio.create_task(update_dashboard_loop(client))

        while True:
            try:
                username = generate_username(USERNAME_LEN)
                await check_one(username, client)

                # تأخير عشوائي
                delay = random.uniform(CHECK_DELAY_MIN, CHECK_DELAY_MAX)

                # لو كل المنصات blocked ننتظر أكثر
                if (ig_checker.is_blocked()
                    and sc_checker.is_blocked()
                    and tw_checker.is_blocked()):
                    min_wait = min(
                        ig_checker.blocked_until,
                        sc_checker.blocked_until,
                        tw_checker.blocked_until,
                    ) - time.time()
                    if min_wait > 0:
                        log.warning(f"⏳ كل المنصات blocked — ننتظر {min_wait:.0f}s")
                        await asyncio.sleep(min(min_wait, 120))
                        continue

                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                log.warning("⚠️ Task cancelled — نعيد التشغيل...")
                await asyncio.sleep(5)
                continue
            except Exception as e:
                log.error(f"❌ خطأ: {e}")
                stats["errors"] += 1
                await asyncio.sleep(10)
                continue


# ============================================================
# 🌐 FastAPI — مع KEEP-ALIVE مدمج
# ============================================================
app = FastAPI(title="Username Hunter v5")

# ✅ هذا هو الحل الأساسي لمشكلة الإطفاء
# السيرفر يبنق نفسه كل 10 دقائق
_self_ping_started = False

async def self_ping():
    """يبنق نفسه عشان Render ما يطفيه"""
    port = int(os.getenv("PORT", 10000))
    await asyncio.sleep(30)  # ننتظر السيرفر يشتغل
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await client.get(f"http://0.0.0.0:{port}/health", timeout=5)
                log.debug("💓 Self-ping OK")
            except Exception:
                pass
            await asyncio.sleep(600)  # كل 10 دقائق


# ✅ المهم: نشغل الـ hunter كـ background task بطريقة ما تموت
hunter_task = None

@app.on_event("startup")
async def startup():
    global hunter_task, _self_ping_started
    log.info("🌐 FastAPI Starting...")

    # شغل الـ hunter
    hunter_task = asyncio.create_task(hunter_loop())

    # شغل الـ self-ping
    if not _self_ping_started:
        asyncio.create_task(self_ping())
        _self_ping_started = True

    log.info("✅ كل شي شغال!")


@app.on_event("shutdown")
async def shutdown():
    stats["running"] = False
    save_results()
    log.info("💾 النتائج محفوظة — إلى اللقاء!")


@app.get("/")
async def root():
    total = stats["total_scanned"]
    found = len(stats["found"])
    return {
        "status": "🟢 running" if stats["running"] else "🔴 stopped",
        "version": "5.0-fixed",
        "scanned": total,
        "found": found,
        "success_rate": f"{found/max(total,1)*100:.3f}%",
    }


@app.get("/health")
async def health():
    """endpoint للـ keep-alive"""
    global hunter_task

    # ✅ لو الـ hunter task مات — نشغله من جديد!
    if hunter_task is None or hunter_task.done():
        log.warning("🔄 Hunter task died — restarting!")
        hunter_task = asyncio.create_task(hunter_loop())

    return {"status": "alive", "running": not (hunter_task is None or hunter_task.done())}


@app.get("/stats")
async def get_stats():
    return {
        "total_scanned": stats["total_scanned"],
        "found_count": len(stats["found"]),
        "found_usernames": stats["found"][-20:],
        "last_user": stats["last_user"],
        "errors": stats["errors"],
        "rate_limits": {
            "instagram": stats["ig_rate_limits"],
            "snapchat": stats["sc_rate_limits"],
            "twitter": stats["tw_rate_limits"],
        },
        "uptime": str(datetime.now() - stats["start_time"]).split(".")[0] if stats["start_time"] else "—",
    }


@app.get("/found")
async def get_found():
    return {"count": len(stats["found"]), "results": stats["found"]}


@app.get("/dashboard", response_class=HTMLResponse)
async def web_dashboard():
    """داشبورد ويب بسيط"""
    total = stats["total_scanned"]
    found = len(stats["found"])
    uptime = str(datetime.now() - stats["start_time"]).split(".")[0] if stats["start_time"] else "—"

    rows = ""
    for f in reversed(stats["found"][-20:]):
        rows += f"<tr><td>{f['username']}</td><td>{', '.join(f['platforms'])}</td><td>{f['time']}</td></tr>\n"

    return f"""
    <html>
    <head>
        <title>Hunter v5 Dashboard</title>
        <meta http-equiv="refresh" content="15">
        <style>
            body {{ font-family: monospace; background: #1a1a2e; color: #eee; padding: 20px; }}
            .stat {{ display: inline-block; background: #16213e; padding: 15px; margin: 5px; border-radius: 8px; min-width: 150px; text-align: center; }}
            .stat h3 {{ margin: 0; color: #0f3460; font-size: 14px; }}
            .stat p {{ margin: 5px 0 0; font-size: 24px; color: #e94560; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid #333; }}
            th {{ background: #16213e; }}
            h1 {{ color: #e94560; }}
        </style>
    </head>
    <body>
        <h1>🎯 Hunter v5 Dashboard</h1>
        <div>
            <div class="stat"><h3>Scanned</h3><p>{total:,}</p></div>
            <div class="stat"><h3>Found</h3><p>{found}</p></div>
            <div class="stat"><h3>Uptime</h3><p>{uptime}</p></div>
            <div class="stat"><h3>Last</h3><p>{stats['last_user']}</p></div>
        </div>
        <h2>🎯 الصيدات</h2>
        <table>
            <tr><th>Username</th><th>Platforms</th><th>Time</th></tr>
            {rows if rows else '<tr><td colspan="3">ما لقينا شي بعد...</td></tr>'}
        </table>
        <p style="color:#666; margin-top:20px;">يتحدث تلقائياً كل 15 ثانية</p>
    </body>
    </html>
    """


# ============================================================
# 🚀 MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    log.info(f"🌐 Starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
