/**
 * 「依次更新对标账号」的限制、节奏和定时规则。批量页（batch.js）和后台（background.js）共用。
 *
 * 平台识别自动化主要看三样：陌生浏览器、机械节奏、自动化痕迹。
 * 插件跑在用户日常的 Chrome 里，第一样基本不存在；这里处理后两样：
 *   - 节奏：停留和间隔取长尾随机，账号顺序每次打乱，定时落在时段内的随机时刻
 *   - 痕迹：不做程序滚动，只读打开主页时就加载的那一页笔记；5 天内采过的账号跳过
 *   - 兜底：出现验证页或安全提示立即停止，14 天内不再运行，自动运行降为提醒
 */
const LIMITS = {
  maxAccounts: 10,   // 每次最多更新几个账号
  minDays: 7,        // 两次批量更新至少间隔几天
  hours: [9, 23],    // 只在这个时段内运行
  freshDays: 5,      // 这几天内采过的账号跳过
  pauseDays: 14,     // 遇到验证或安全提示后暂停几天
  idleSeconds: 300,  // 自动运行要求电脑在这么多秒内有操作
};

const PERIODS = { morning: ["上午", 9, 12], afternoon: ["下午", 13, 18], evening: ["晚上", 19, 22] };
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const ALARM = "benchmark-update";
const NOTICE = "benchmark-update";

const DAY = 864e5;
function rand(a, b) { return a + Math.random() * (b - a); }

/** 每个主页停留多久再读取：多数 6–14 秒，偶尔 15–30 秒 */
function dwellMs() { return Math.random() < 0.2 ? rand(15e3, 30e3) : rand(6e3, 14e3); }
/** 账号之间等多久：多数 20–50 秒，偶尔 1.5–4 分钟 */
function gapMs() { return Math.random() < 0.15 ? rand(90e3, 240e3) : rand(20e3, 50e3); }

function shuffle(list) {
  const a = [...list];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

const md = d => `${d.getMonth() + 1}月${d.getDate()}日`;
const day = iso => iso ? md(new Date(iso)) : "—";
const when = d => `${md(d)}（${WEEKDAYS[d.getDay()]}）${String(d.getHours()).padStart(2, "0")}:${
  String(d.getMinutes()).padStart(2, "0")}`;

function loadState() { return chrome.storage.local.get(null); }

/** 采集过的账号里，跳过近几天采过的，最久没更新的排前面，最多 maxAccounts 个 */
function queue(st, now = Date.now()) {
  return Object.entries(st.accounts || {})
    .map(([userId, a]) => ({ userId, ...a }))
    .filter(a => !a.capturedAt || now - Date.parse(a.capturedAt) >= LIMITS.freshDays * DAY)
    .sort((a, b) => (a.capturedAt || "").localeCompare(b.capturedAt || ""))
    .slice(0, LIMITS.maxAccounts);
}

/** 现在能不能跑；不能时返回 { code, text } */
function blocker(st, now = new Date()) {
  if (st.pausedUntil && now < new Date(st.pausedUntil))
    return { code: "paused", text: `已暂停至 ${day(st.pausedUntil)}` };
  if (st.lastBatchAt) {
    const next = Date.parse(st.lastBatchAt) + LIMITS.minDays * DAY;
    if (now.getTime() < next)
      return { code: "recent", text: `${LIMITS.minDays} 天内已更新过，${day(new Date(next).toISOString())} 后可再次更新` };
  }
  const h = now.getHours();
  if (h < LIMITS.hours[0] || h >= LIMITS.hours[1])
    return { code: "hours", text: `仅在 ${LIMITS.hours[0]}:00–${LIMITS.hours[1]}:00 可用` };
  if (!queue(st, now.getTime()).length)
    return { code: "empty", text: Object.keys(st.accounts || {}).length
      ? `账号都在 ${LIMITS.freshDays} 天内采集过` : "尚无采集过的账号" };
  return null;
}

/** 从 from 起，第一个落在 period 时段里的随机时刻；weekday 不为空时只看那一天 */
function slot(period, from, weekday = null) {
  const [, h0, h1] = PERIODS[period] || PERIODS.evening;
  for (let i = 0; i < 15; i++) {
    const d = new Date(from);
    d.setDate(d.getDate() + i);
    if (weekday !== null && d.getDay() !== weekday) continue;
    const start = new Date(d).setHours(h0, 0, 0, 0);
    const end = new Date(d).setHours(h1, 0, 0, 0);
    const lo = Math.max(start, from.getTime());
    if (lo < end - 10 * 60e3) return new Date(rand(lo, end));
  }
  return null;
}

function tomorrow(now = new Date()) {
  const d = new Date(now);
  d.setDate(d.getDate() + 1);
  d.setHours(0, 0, 0, 0);
  return d;
}

async function setNext(at) {
  if (!at) return null;
  await chrome.storage.local.set({ nextAt: at.toISOString() });
  await chrome.alarms.create(ALARM, { when: at.getTime() });
  return at;
}

/** 按设置排下一次：每周固定那天的时段里随机一刻，且不早于 7 天间隔和暂停期 */
async function planNext() {
  const st = await loadState();
  const s = st.schedule || {};
  if (!s.mode || s.mode === "off") {
    await chrome.alarms.clear(ALARM);
    await chrome.storage.local.remove("nextAt");
    return null;
  }
  const earliest = Math.max(Date.now(),
    st.lastBatchAt ? Date.parse(st.lastBatchAt) + LIMITS.minDays * DAY : 0,
    st.pausedUntil ? Date.parse(st.pausedUntil) : 0);
  return setNext(slot(s.period, new Date(earliest), s.weekday ?? 0));
}

/** 判断页面是不是被拦了。url 为空表示跳到了插件无权读取的域名 */
function blockedUrl(url) {
  return !url || /captcha|verify|security|website-login|\/login/i.test(url);
}
const BLOCK_TEXT = /安全限制|IP存在风险|300012|验证码|完成验证|滑块|访问频繁|行为异常|账号异常|请稍后再试/;

function notify(title, message) {
  return chrome.notifications.create(NOTICE, { type: "basic", iconUrl: "icons/128.png",
    title, message, priority: 0 });
}
