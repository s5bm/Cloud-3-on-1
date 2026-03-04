import os
import asyncio
import random
import string
import logging
import time
import hashlib
from datetime import datetime, timedelta
from collections import deque, defaultdict
import httpx
from fastapi import FastAPI
import uvicorn

# ============================================================
#  USERNAME HUNTER v4.0 — MATH EDITION
#  أعلى نسبة نجاح ممكنة — مبني على تحليل رياضي عميق
# ============================================================

WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "")
USERNAME_LEN = int(os.getenv("USERNAME_LEN", 4))
IG_SESSION   = os.getenv("IG_SESSION", "")
TW_BEARER    = os.getenv("TW_BEARER", "")

NORMAL_DELAY_MIN = float(os.getenv("DELAY_MIN", 18))
NORMAL_DELAY_MAX = float(os.getenv("DELAY_MAX", 35))
PURGE_DELAY_MIN  = 4.0
PURGE_DELAY_MAX  = 10.0

PURGE_WATCH_ACCOUNT  = os.getenv("PURGE_WATCH_ACCOUNT", "instagram")
PURGE_DROP_THRESHOLD = float(os.getenv("PURGE_DROP_THRESHOLD", 0.005))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("Hunter")

# ====== تحليل رياضي لمساحة البحث ======
# النمط            | التركيبات | نسبة التوفر | الوزن
# 4 حروف نقية      | 456,976   | ~0%         | 0%  (محذوف)
# 1حرف + 3أرقام    | 104,000   | ~20%        | 30%
# مع نقطة          | +93,000   | ~25%        | 25%
# مع underscore    | +93,000   | ~22%        | 20%
# 2حروف + 2أرقام   | 405,600   | ~5%         | 15%
# double_repeat    | نادر      | ~8%         | 10%

stats = {
    "total_scanned": 0,
    "found": [],
    "last_user": "—",
    "start_time": datetime.now(),
    "rate_limits": {"instagram": 0, "snapchat": 0, "twitter": 0},
    "errors": 0,
    "invalid_skipped": 0,
    "duplicate_skipped": 0,
    "purge_mode": False,
    "purge_count": 0,
    "purge_detected_at": None,
    "by_strategy": {
        "one_letter_3digits": 0,
        "two_letters_2digits": 0,
        "dot": 0,
        "underscore": 0,
        "double_repeat": 0,
    },
    "strategy_success": defaultdict(int),
    "strategy_attempts": defaultdict(int),
}

dashboard_msg_id = None

# ============================================================
# 🧠 SMART RATE LIMITER
# ============================================================
class SmartRateLimiter:
    def __init__(self, name):
        self.name = name
        self.blocked_until = datetime.now()
        self.hit_count = 0
        self.backoff = 60
        self.max_backoff = 600
        self.history = deque(maxlen=10)

    def is_blocked(self): return datetime.now() < self.blocked_until
    def seconds_left(self): return max(0, (self.blocked_until - datetime.now()).total_seconds())

    def register_limit(self, retry_after=None):
        self.hit_count += 1
        stats["rate_limits"][self.name] += 1
        wait = retry_after + 5 if retry_after else min(self.backoff * (1.5 ** self.hit_count), self.max_backoff)
        self.blocked_until = datetime.now() + timedelta(seconds=wait)
        log.warning(f"⏳ [{self.name}] Rate Limit — انتظر {wait:.0f}s (#{self.hit_count})")

    def register_success(self):
        self.history.append(1)
        if self.hit_count > 0 and len(self.history) >= 5 and sum(self.history) == 5:
            self.hit_count = max(0, self.hit_count - 1)

    def register_failure(self): self.history.append(0)


# ============================================================
# 🛡️ USERNAME VALIDATOR
# قواعد Instagram الرسمية:
# - لا يبدأ/ينتهي بـ . أو _
# - لا نقطتان متتاليتان
# - 3-30 حرف فقط
# - حروف + أرقام + . + _ فقط
# ============================================================
ALLOWED_CHARS = set(string.ascii_lowercase + string.digits + "._")

def is_valid_instagram(u: str) -> bool:
    if not u or len(u) < 3 or len(u) > 30: return False
    if u[0] in "._" or u[-1] in "._": return False
    if ".." in u or "__" in u: return False
    return all(c in ALLOWED_CHARS for c in u)

def is_valid_twitter(u: str) -> bool:
    if "." in u or len(u) > 15: return False
    return all(c in set(string.ascii_lowercase + string.digits + "_") for c in u)


# ============================================================
# 🔁 DEDUPLICATOR — لا تفحص نفس اليوزر مرتين
# ============================================================
class SeenFilter:
    def __init__(self, max_size=500_000):
        self.seen = set()
        self.max_size = max_size

    def check_and_mark(self, username: str) -> bool:
        h = hashlib.md5(username.encode()).hexdigest()[:8]
        if h in self.seen: return True
        if len(self.seen) > self.max_size:
            self.seen = set(list(self.seen)[self.max_size // 2:])
        self.seen.add(h)
        return False

seen_filter = SeenFilter()


# ============================================================
# 🎲 ULTRA-MATH USERNAME GENERATOR v4
# ============================================================
LETTERS = string.ascii_lowercase
DIGITS  = string.digits
CHARS   = LETTERS + DIGITS

BASE_WEIGHTS = {
    "one_letter_3digits": 30,
    "dot":                25,
    "underscore":         20,
    "two_letters_2digits": 15,
    "double_repeat":      10,
}

def get_weights() -> dict:
    weights = dict(BASE_WEIGHTS)
    total = sum(stats["strategy_attempts"].values())
    if total > 200:
        for s in weights:
            att = stats["strategy_attempts"][s]
            if att > 20:
                sr = stats["strategy_success"][s] / att
                weights[s] = int(BASE_WEIGHTS[s] * (1 + sr * 3))
    return weights

def generate_username(length: int = 4) -> str:
    w = get_weights()
    strategy = random.choices(list(w.keys()), weights=list(w.values()))[0]
    stats["by_strategy"][strategy] += 1
    stats["strategy_attempts"][strategy] += 1

    for _ in range(10):
        u = _build(strategy, length)
        if is_valid_instagram(u):
            return u
    return _fallback(length)

def _build(strategy: str, length: int) -> str:
    if strategy == "one_letter_3digits":
        chars = [random.choice(LETTERS)] + random.choices(DIGITS, k=3)
        random.shuffle(chars)
        return "".join(chars)

    elif strategy == "two_letters_2digits":
        chars = random.choices(LETTERS, k=2) + random.choices(DIGITS, k=2)
        random.shuffle(chars)
        return "".join(chars)

    elif strategy == "dot":
        base = _mix(length - 1)
        pos = random.randint(1, length - 2)
        base.insert(pos, ".")
        return "".join(base[:length])

    elif strategy == "underscore":
        base = _mix(length - 1)
        pos = random.randint(1, length - 2)
        base.insert(pos, "_")
        return "".join(base[:length])

    elif strategy == "double_repeat":
        pattern = random.choices(["aabb", "abab", "aabc"], weights=[40, 35, 25])[0]
        if pattern == "aabb":
            a, b = random.choice(CHARS), random.choice(CHARS)
            return a + a + b + b
        elif pattern == "abab":
            a, b = random.choice(CHARS), random.choice(CHARS)
            return a + b + a + b
        else:
            a, b, c = random.choice(CHARS), random.choice(CHARS), random.choice(CHARS)
            return a + a + b + c

    return _fallback(length)

def _mix(n: int) -> list:
    chars = [random.choice(LETTERS), random.choice(DIGITS)] + random.choices(CHARS, k=max(0, n - 2))
    random.shuffle(chars)
    return chars

def _fallback(length: int) -> str:
    chars = [random.choice(LETTERS)] + random.choices(DIGITS, k=length - 1)
    random.shuffle(chars)
    return "".join(chars)


# ============================================================
# 🔍 PURGE DETECTOR
# ============================================================
class PurgeDetector:
    def __init__(self):
        self.last_count = None

    async def start(self, client: httpx.AsyncClient):
        while True:
            try:
                await self._check(client)
            except Exception as e:
                log.debug(f"[Purge] {e}")
            await asyncio.sleep(300)

    async def _check(self, client: httpx.AsyncClient):
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "X-IG-App-ID": "936619743392459",
        }
        if IG_SESSION: headers["Cookie"] = f"sessionid={IG_SESSION}"

        r = await client.get(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={PURGE_WATCH_ACCOUNT}",
            headers=headers, timeout=15
        )
        if r.status_code != 200: return

        count = r.json()["data"]["user"]["edge_followed_by"]["count"]
        if self.last_count:
            drop = (self.last_count - count) / self.last_count
            if drop >= PURGE_DROP_THRESHOLD and not stats["purge_mode"]:
                stats["purge_mode"] = True
                stats["purge_count"] += 1
                stats["purge_detected_at"] = datetime.now()
                log.warning(f"🚨 PURGE! انخفاض {drop*100:.2f}% — وضع الطوارئ!")
                asyncio.create_task(self._end_after(10800))
        self.last_count = count

    async def _end_after(self, secs):
        await asyncio.sleep(secs)
        stats["purge_mode"] = False
        log.info("✅ انتهى Purge Mode")


# ============================================================
# 📡 PLATFORM AGENTS
# ============================================================
class InstagramAgent:
    def __init__(self):
        self.limiter = SmartRateLimiter("instagram")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        if IG_SESSION: self.headers["Cookie"] = f"sessionid={IG_SESSION}"

    async def check(self, username: str, client: httpx.AsyncClient) -> str:
        if self.limiter.is_blocked(): return "rate_limit"
        try:
            r = await client.get(f"https://www.instagram.com/{username}/", headers=self.headers, timeout=10, follow_redirects=True)
            if r.status_code == 404:   self.limiter.register_success(); return "available"
            elif r.status_code == 200: self.limiter.register_success(); return "taken"
            elif r.status_code == 429: self.limiter.register_limit(float(r.headers.get("Retry-After", 60))); return "rate_limit"
            elif r.status_code in [301, 302]: self.limiter.register_success(); return "taken"
            else: self.limiter.register_failure(); return "error"
        except httpx.TimeoutException: return "error"
        except Exception as e: log.debug(f"[IG] {e}"); stats["errors"] += 1; return "error"


class SnapchatAgent:
    def __init__(self):
        self.limiter = SmartRateLimiter("snapchat")
        self.headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"}

    async def check(self, username: str, client: httpx.AsyncClient) -> str:
        if "." in username: return "invalid"
        if self.limiter.is_blocked(): return "rate_limit"
        try:
            r = await client.get(f"https://www.snapchat.com/add/{username}", headers=self.headers, timeout=10, follow_redirects=False)
            if r.status_code == 404:              self.limiter.register_success(); return "available"
            elif r.status_code in [200,301,302,308]: self.limiter.register_success(); return "taken"
            elif r.status_code == 429:            self.limiter.register_limit(); return "rate_limit"
            else: self.limiter.register_failure(); return "error"
        except httpx.TimeoutException: return "error"
        except Exception as e: log.debug(f"[SC] {e}"); return "error"


class TwitterAgent:
    def __init__(self):
        self.limiter = SmartRateLimiter("twitter")
        self.use_api = bool(TW_BEARER)
        self.headers = ({"Authorization": f"Bearer {TW_BEARER}"} if self.use_api
                        else {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    async def check(self, username: str, client: httpx.AsyncClient) -> str:
        if not is_valid_twitter(username): return "invalid"
        if self.limiter.is_blocked(): return "rate_limit"
        try:
            if self.use_api:
                r = await client.get(f"https://api.twitter.com/2/users/by/username/{username}", headers=self.headers, timeout=10)
                if r.status_code == 200:   self.limiter.register_success(); return "taken"
                elif r.status_code == 404: self.limiter.register_success(); return "available"
                elif r.status_code == 429:
                    reset = float(r.headers.get("x-rate-limit-reset", time.time() + 900)) - time.time()
                    self.limiter.register_limit(max(reset, 60)); return "rate_limit"
                else: return "error"
            else:
                r = await client.get(f"https://x.com/{username}", headers=self.headers, timeout=10, follow_redirects=True)
                if r.status_code == 404:   self.limiter.register_success(); return "available"
                elif r.status_code == 200: self.limiter.register_success(); return "taken"
                elif r.status_code == 429: self.limiter.register_limit(); return "rate_limit"
                else: self.limiter.register_failure(); return "error"
        except httpx.TimeoutException: return "error"
        except Exception as e: log.debug(f"[TW] {e}"); return "error"


# ============================================================
# 🏆 VALUE SCORER
# ============================================================
def score_username(username: str, platforms: list) -> dict:
    score, tags = 0, []
    clean = username.replace(".", "").replace("_", "")

    score += len(platforms) * 20
    if len(platforms) == 3: tags.append("💎 TRIPLE")
    if "." in username:  score += 15; tags.append("🔵 DOT")
    if "_" in username:  score += 15; tags.append("🔵 UNDERSCORE")

    digits  = sum(c.isdigit() for c in clean)
    letters = sum(c.isalpha() for c in clean)
    if digits >= 3:  score += 20; tags.append("🔢 DIGIT-HEAVY")
    if letters == 1: score += 30; tags.append("⭐ SINGLE-LETTER")

    if len(clean) >= 4:
        if clean[0] == clean[1] and clean[2] == clean[3]: score += 40; tags.append("👑 AABB")
        elif clean[0] == clean[2] and clean[1] == clean[3]: score += 35; tags.append("👑 ABAB")
        elif clean[0] == clean[1]: score += 20; tags.append("✨ DOUBLE")

    tier = "🥇 S-TIER" if score >= 90 else "🥈 A-TIER" if score >= 60 else "🥉 B-TIER"
    return {"score": score, "tier": tier, "tags": tags}


# ============================================================
# 🔔 DISCORD
# ============================================================
async def send_catch_alert(username: str, platforms: list):
    if not WEBHOOK_URL: return
    ev = score_username(username, platforms)
    links = "\n".join(filter(None, [
        f"[Instagram](https://instagram.com/{username})" if "instagram" in platforms else "",
        f"[Snapchat](https://snapchat.com/add/{username})"  if "snapchat"  in platforms else "",
        f"[Twitter/X](https://x.com/{username})"            if "twitter"   in platforms else "",
    ]))
    payload = {"embeds": [{"title": f"🎯 صيدة — @{username}", "color": 0xFFD700 if ev["score"] >= 90 else 0x5865F2,
        "fields": [
            {"name": "📱 المنصات", "value": " + ".join(p.capitalize() for p in platforms), "inline": True},
            {"name": "⭐ التقييم", "value": f"{ev['tier']} ({ev['score']}pts)", "inline": True},
            {"name": "🏷️ الأنماط", "value": " ".join(ev["tags"]) or "—", "inline": False},
            {"name": "🔗 الروابط", "value": links, "inline": False},
        ],
        "footer": {"text": f"Purge: {'🚨 نشط' if stats['purge_mode'] else '😴 عادي'} • {datetime.now().strftime('%H:%M:%S')}"}
    }]}
    try:
        async with httpx.AsyncClient() as c: await c.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e: log.warning(f"⚠️ إشعار فشل: {e}")


async def update_dashboard():
    global dashboard_msg_id
    if not WEBHOOK_URL: return
    while True:
        uptime = datetime.now() - stats["start_time"]
        up_str = f"{int(uptime.total_seconds()//3600)}h {int((uptime.total_seconds()%3600)//60)}m"
        rate   = stats["total_scanned"] / max(uptime.total_seconds() / 60, 1)
        finds  = stats["found"][-5:]
        find_str = "\n".join(f"`{f['username']}` {f.get('tier','—')} على {', '.join(f['platforms'])}" for f in reversed(finds)) or "لا يوجد بعد..."

        total_gen  = stats["total_scanned"] + stats["invalid_skipped"] + stats["duplicate_skipped"]
        efficiency = stats["total_scanned"] / max(total_gen, 1) * 100

        strat_lines = [
            f"{s[:16]:16} {stats['strategy_attempts'][s]:4d} فحص | "
            f"{stats['strategy_success'][s]/max(stats['strategy_attempts'][s],1)*100:.1f}% نجاح"
            for s in stats["by_strategy"] if stats["strategy_attempts"][s] > 0
        ]
        strat_str = "\n".join(strat_lines) or "لا بيانات بعد..."

        def ps(agent): return f"⏳({agent.limiter.seconds_left():.0f}s)" if agent.limiter.is_blocked() else "🟢"

        payload = {"embeds": [{"title": "🎯 Hunter v4 — Math Edition", "color": 0xFF0000 if stats["purge_mode"] else 0x00FF88,
            "fields": [
                {"name": "📊 الإحصائيات", "value": f"```\nإجمالي الفحص  : {stats['total_scanned']:,}\nالصيدات       : {len(stats['found'])}\nمعدل الفحص    : {rate:.1f}/دقيقة\nكفاءة الجيل   : {efficiency:.1f}%\nوقت التشغيل   : {up_str}\nأخطاء         : {stats['errors']}\n```", "inline": False},
                {"name": "⚡ الوضع", "value": "🚨 PURGE MODE" if stats["purge_mode"] else "😴 عادي", "inline": True},
                {"name": "📡 المنصات", "value": f"📸 IG: {ps(ig_agent)}\n👻 SC: {ps(sc_agent)}\n🐦 TW: {ps(tw_agent)}", "inline": True},
                {"name": "🔢 Skip", "value": f"Invalid: {stats['invalid_skipped']}\nDuplicate: {stats['duplicate_skipped']}", "inline": True},
                {"name": "🧠 أداء الاستراتيجيات", "value": f"```\n{strat_str}\n```", "inline": False},
                {"name": "🎯 آخر الصيدات", "value": find_str, "inline": False},
                {"name": "🔍 آخر يوزر", "value": f"`{stats['last_user']}`", "inline": False},
            ],
            "footer": {"text": f"Purge: {stats['purge_count']}x • {datetime.now().strftime('%H:%M:%S')}"}
        }]}

        try:
            async with httpx.AsyncClient() as c:
                if not dashboard_msg_id:
                    r = await c.post(f"{WEBHOOK_URL}?wait=true", json=payload, timeout=10)
                    if r.status_code in [200, 204]: dashboard_msg_id = r.json().get("id"); log.info(f"✅ داشبورد ID:{dashboard_msg_id}")
                else:
                    await c.patch(f"{WEBHOOK_URL}/messages/{dashboard_msg_id}", json=payload, timeout=10)
        except Exception as e: log.warning(f"⚠️ داشبورد: {e}")
        await asyncio.sleep(15)


# ============================================================
# 🤖 AGENTS & LOOP
# ============================================================
ig_agent       = InstagramAgent()
sc_agent       = SnapchatAgent()
tw_agent       = TwitterAgent()
purge_detector = PurgeDetector()


def _detect_strategy(u: str) -> str:
    if "." in u: return "dot"
    if "_" in u: return "underscore"
    clean = u.replace(".", "").replace("_", "")
    d, l = sum(c.isdigit() for c in clean), sum(c.isalpha() for c in clean)
    if l == 1 and d == 3: return "one_letter_3digits"
    if l == 2 and d == 2: return "two_letters_2digits"
    if len(clean) >= 2 and clean[0] == clean[1]: return "double_repeat"
    return "one_letter_3digits"


async def scan_username(username: str, client: httpx.AsyncClient):
    stats["total_scanned"] += 1
    stats["last_user"] = username

    results = await asyncio.gather(
        ig_agent.check(username, client),
        sc_agent.check(username, client),
        tw_agent.check(username, client),
        return_exceptions=True
    )
    ig_r, sc_r, tw_r = [r if isinstance(r, str) else "error" for r in results]

    available = [p for p, r in [("instagram", ig_r), ("snapchat", sc_r), ("twitter", tw_r)] if r == "available"]

    if available:
        ev = score_username(username, available)
        stats["strategy_success"][_detect_strategy(username)] += 1
        stats["found"].append({"username": username, "platforms": available, "time": datetime.now().strftime("%H:%M:%S"), "tier": ev["tier"], "score": ev["score"]})
        log.info(f"🎯 [{username}] {ev['tier']} على: {' + '.join(p.capitalize() for p in available)}")
        await send_catch_alert(username, available)
    else:
        log.info(f"{'🚨' if stats['purge_mode'] else '🔍'} [{username}] IG:{ig_r[:2].upper()} SC:{sc_r[:2].upper()} TW:{tw_r[:2].upper()}")


async def hunter_loop():
    log.info("🚀 Username Hunter v4 — Math Edition!")
    log.info("📐 1حرف+3أرقام(30%) | نقطة(25%) | underscore(20%) | 2+2(15%) | repeat(10%)")
    log.info("🛡️ Validator نشط | 🔁 Deduplicator نشط | 🧠 Adaptive Learning نشط")

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=10, max_keepalive_connections=5), timeout=httpx.Timeout(15.0)) as client:
        asyncio.create_task(purge_detector.start(client))

        while True:
            # ولّد يوزر valid وغير مكرر
            for _ in range(20):
                username = generate_username(USERNAME_LEN)
                if not is_valid_instagram(username): stats["invalid_skipped"] += 1; continue
                if seen_filter.check_and_mark(username): stats["duplicate_skipped"] += 1; continue
                break

            await scan_username(username, client)
            await asyncio.sleep(random.uniform(
                PURGE_DELAY_MIN if stats["purge_mode"] else NORMAL_DELAY_MIN,
                PURGE_DELAY_MAX if stats["purge_mode"] else NORMAL_DELAY_MAX,
            ))


# ============================================================
# 🌐 FastAPI
# ============================================================
app = FastAPI(title="Username Hunter v4", version="4.0")

@app.get("/")
async def health():
    t, f = stats["total_scanned"], len(stats["found"])
    return {"status": "running", "version": "4.0-math", "scanned": t, "found": f,
            "success_rate": f"{f/max(t,1)*100:.2f}%", "purge_mode": stats["purge_mode"],
            "efficiency": f"{t/(t+stats['invalid_skipped']+stats['duplicate_skipped']+1)*100:.1f}%"}

@app.get("/stats")
async def get_stats():
    return {**{k: v for k, v in stats.items() if k not in ["strategy_success","strategy_attempts","by_strategy"]},
            "found": stats["found"][-20:],
            "strategy_performance": {s: {"attempts": stats["strategy_attempts"][s], "success": stats["strategy_success"][s],
                "rate": f"{stats['strategy_success'][s]/max(stats['strategy_attempts'][s],1)*100:.1f}%"} for s in stats["by_strategy"]},
            "uptime": str(datetime.now() - stats["start_time"]).split(".")[0]}

@app.post("/purge/on")
async def purge_on():
    stats["purge_mode"] = True; stats["purge_count"] += 1
    asyncio.create_task(purge_detector._end_after(3600))
    return {"status": "Purge Mode ON — 1hr"}

@app.post("/purge/off")
async def purge_off():
    stats["purge_mode"] = False; return {"status": "Purge Mode OFF"}


async def main():
    if not WEBHOOK_URL: log.warning("⚠️ WEBHOOK_URL مو موجود")
    asyncio.create_task(hunter_loop())
    asyncio.create_task(update_dashboard())
    port = int(os.getenv("PORT", 10000))
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")).serve()

if __name__ == "__main__":
    asyncio.run(main())
