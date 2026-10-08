/**
 * 定期更新对标账号：到点提醒，或在用户同意后自动打开批量页运行。
 *
 * 这里只负责「什么时候」：判断能不能跑、发提醒、打开批量页。
 * 真正的访问循环在 batch.js 里——service worker 空闲 30 秒会被回收，撑不过账号间的等待。
 */
importScripts("shared.js");

chrome.runtime.onInstalled.addListener(restore);
chrome.runtime.onStartup.addListener(restore);

/** 浏览器启动或插件更新后重排闹钟。错过的那次不在启动瞬间补跑，而是随机延后几分钟 */
async function restore() {
  const st = await loadState();
  const s = st.schedule || {};
  if (!s.mode || s.mode === "off") return chrome.alarms.clear(ALARM);
  if (!st.nextAt) return planNext();
  const at = Date.parse(st.nextAt);
  if (at > Date.now()) return chrome.alarms.create(ALARM, { when: at });
  return setNext(new Date(Date.now() + rand(3, 12) * 60e3));
}

chrome.alarms.onAlarm.addListener(a => { if (a.name === ALARM) onDue(); });

async function onDue() {
  const st = await loadState();
  const s = st.schedule || {};
  if (!s.mode || s.mode === "off") return;
  const now = new Date();

  const why = blocker(st, now);
  if (why) {
    // 时段外顺延到下一个时段；其余情况（暂停、7 天内、没账号）按每周规则重排
    if (why.code === "hours") return setNext(slot(s.period, now));
    return planNext();
  }

  const list = queue(st, now.getTime());
  const auto = s.mode === "auto" && st.consentAt && st.autoConsentAt;
  if (!auto) {
    const ago = st.lastBatchAt ? Math.round((now - Date.parse(st.lastBatchAt)) / DAY) : null;
    await notify("对标账号待更新",
      `${list.length} 个账号 · ${ago === null ? "尚未批量更新" : `上次批量更新 ${ago} 天前`}`);
    await chrome.action.setBadgeBackgroundColor({ color: "#E8467F" });
    await chrome.action.setBadgeText({ text: "!" });
    return setNext(slot(s.period, tomorrow(now)));   // 没处理就明天同一时段再提醒
  }

  // 电脑没人用时不跑：锁屏或长时间无操作时出现成串访问，不像真人
  const idle = await chrome.idle.queryState(LIMITS.idleSeconds);
  if (idle !== "active") {
    const retry = new Date(Date.now() + rand(20, 40) * 60e3);
    return setNext(retry.getHours() < LIMITS.hours[1] ? retry : slot(s.period, tomorrow(now)));
  }

  // 批量页没跑起来（被关掉、取消）时的兜底：明天同一时段再看
  await setNext(slot(s.period, tomorrow(now)));
  await chrome.tabs.create({ url: chrome.runtime.getURL("batch.html?auto=1"), active: false });
}

chrome.notifications.onClicked.addListener(id => {
  if (id !== NOTICE) return;
  chrome.notifications.clear(id);
  chrome.tabs.create({ url: chrome.runtime.getURL("batch.html") });
});
