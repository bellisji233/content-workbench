/**
 * 依次更新对标账号（实验功能）。
 *
 * 在用户自己的浏览器里依次打开采集过的账号主页，读取打开时加载的那一页笔记，
 * 结果和手动「抓取这个主页」一样落到下载目录。限制和节奏规则在 shared.js，
 * 这里只管界面和访问循环。循环跑在这个扩展页面里，而不是后台 service worker：
 * 后者空闲 30 秒会被浏览器回收，撑不过账号之间的等待。
 *
 * 带 ?auto=1 打开时（定期自动运行），倒数 15 秒后开始，期间可取消。
 */
const AUTO = new URLSearchParams(location.search).has("auto");

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>]/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[m]));
const slug = s => (s || "note").replace(/[\\/:*?"<>|\n\r\t]/g, "").trim().slice(0, 40) || "note";
const sleep = ms => new Promise(r => setTimeout(r, ms));

let stopped = false;
let running = false;

/** 可被「停止」打断的等待 */
async function pause(ms) {
  const end = Date.now() + ms;
  while (!stopped && Date.now() < end) await sleep(Math.min(500, end - Date.now()));
}

function log(line) {
  const el = $("log");
  el.classList.add("on");
  el.textContent += `${new Date().toLocaleTimeString("zh-CN")}  ${line}\n`;
  el.scrollTop = el.scrollHeight;
}

async function render() {
  const st = await loadState();
  const now = Date.now();
  const all = Object.entries(st.accounts || {}).map(([userId, a]) => ({ userId, ...a }))
    .sort((a, b) => (a.capturedAt || "").localeCompare(b.capturedAt || ""));
  const picked = new Set(queue(st, now).map(a => a.userId));
  $("accounts").innerHTML = all.length
    ? `<div class="list">${all.map(a => `<div class="acc${picked.has(a.userId) ? "" : " skip"}">${esc(a.nickname)}<span>${
        day(a.capturedAt)} 采集${picked.has(a.userId) ? "" : " · 本次跳过"}<button class="rm" data-uid="${esc(a.userId)}"${
        running ? " disabled" : ""}>移除</button></span></div>`).join("")}</div>`
    : `<div class="empty">尚无采集过的账号。在对标账号主页使用插件的「抓取这个主页」后，账号显示在此处。</div>`;

  $("paused").innerHTML = st.pausedUntil && now < Date.parse(st.pausedUntil)
    ? `已暂停至 ${day(st.pausedUntil)}${st.pauseReason ? ` · ${esc(st.pauseReason)}` : ""}` : "";
  $("paused").classList.toggle("on", !!$("paused").innerHTML);

  $("consent").checked = !!st.consentAt;
  if (!running) {
    const why = blocker(st);
    $("start").disabled = !!why || !st.consentAt;
    $("state").textContent = why ? why.text : st.consentAt ? `将更新 ${picked.size} 个账号` : "";
  }
  renderSchedule(st);
}

/** 从批量更新列表移除账号。之后在该账号主页手动抓取，会重新加入 */
$("accounts").addEventListener("click", async e => {
  const uid = e.target.closest("button.rm")?.dataset.uid;
  if (!uid || running) return;
  const { accounts = {} } = await chrome.storage.local.get("accounts");
  delete accounts[uid];
  await chrome.storage.local.set({ accounts });
  await render();
});

// ------------------------------------------------------------ 定期更新

function renderSchedule(st) {
  const s = st.schedule || {};
  const mode = $("mode-auto").checked && !st.autoConsentAt ? "auto" : (s.mode || "off");
  for (const m of ["off", "remind", "auto"]) $(`mode-${m}`).checked = mode === m;
  $("mode-auto").disabled = !st.consentAt;
  $("weekday").value = String(s.weekday ?? 0);
  $("period").value = s.period || "evening";
  $("when").classList.toggle("on", mode !== "off");
  $("auto-risk").classList.toggle("on", mode === "auto");
  $("auto-consent").checked = !!st.autoConsentAt;

  let next = "";
  if (mode === "auto" && !st.autoConsentAt) next = "勾选上方确认后生效";
  else if (s.mode && s.mode !== "off" && st.nextAt)
    next = `下次${s.mode === "auto" ? "自动运行" : "提醒"}：${when(new Date(st.nextAt))}`;
  $("next").textContent = next;
}

async function saveSchedule(patch) {
  const st = await loadState();
  await chrome.storage.local.set({ schedule: { weekday: 0, period: "evening", mode: "off",
                                               ...(st.schedule || {}), ...patch } });
  await planNext();
  render();
}

for (const m of ["off", "remind", "auto"]) {
  $(`mode-${m}`).addEventListener("change", async () => {
    const st = await loadState();
    if (m === "auto" && !st.autoConsentAt) return render();   // 先勾确认
    saveSchedule({ mode: m });
  });
}
$("weekday").addEventListener("change", e => saveSchedule({ weekday: Number(e.target.value) }));
$("period").addEventListener("change", e => saveSchedule({ period: e.target.value }));
$("auto-consent").addEventListener("change", async e => {
  if (e.target.checked) {
    await chrome.storage.local.set({ autoConsentAt: new Date().toISOString() });
    saveSchedule({ mode: "auto" });
  } else {
    await chrome.storage.local.remove("autoConsentAt");
    saveSchedule({ mode: "remind" });
  }
});

// ------------------------------------------------------------ 访问

function save(text, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json;charset=utf-8" }));
  return new Promise(resolve => chrome.downloads.download({ url, filename, saveAs: false }, id => {
    setTimeout(() => URL.revokeObjectURL(url), 20000);
    resolve(id);
  }));
}

function loaded(tabId, timeout = 45000) {
  return new Promise((resolve, reject) => {
    const done = (ok, err) => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdate);
      ok ? resolve() : reject(err);
    };
    const onUpdate = (id, info) => { if (id === tabId && info.status === "complete") done(true); };
    const timer = setTimeout(() => done(false, new Error("页面加载超时")), timeout);
    chrome.tabs.onUpdated.addListener(onUpdate);
    chrome.tabs.get(tabId).then(t => { if (t.status === "complete") done(true); }).catch(() => {});
  });
}

function fail(message, kind = "error") {
  return Object.assign(new Error(message), { kind });
}

async function read(target) {
  const [res] = await chrome.scripting.executeScript({ target, world: "MAIN",
    func: () => window.extractProfile() });
  return res && res.result;
}

/** 打开一个主页：等加载、随机停留、读数据，不滚动。失败抛错，kind 区分是否疑似被拦 */
async function visit(acc) {
  const tab = await chrome.tabs.create({ url: acc.url, active: false });
  const target = { tabId: tab.id };
  try {
    await loaded(tab.id);
    await pause(dwellMs());

    const url = (await chrome.tabs.get(tab.id)).url;
    if (blockedUrl(url)) throw fail("页面跳转到了验证或登录页", "risk");
    if (!/\/user\/profile\//.test(url)) throw fail("主页打不开，账号可能已注销或改了链接");

    await chrome.scripting.executeScript({ target, world: "MAIN", files: ["extract.js"] });
    let data = await read(target);
    if (data && !data.notes.length) {       // 列表可能还在加载，再等一会儿读一次
      await pause(rand(3000, 6000));
      data = await read(target);
    }
    if (!data) throw fail("没拿到页面数据");
    if (!data.notes.length) {
      const [t] = await chrome.scripting.executeScript({ target,
        func: () => document.title + "\n" + (document.body ? document.body.innerText : "").slice(0, 800) });
      if (BLOCK_TEXT.test((t && t.result) || "")) throw fail("页面出现验证或安全提示", "risk");
    }
    if (data.warnings.some(w => w.includes("未登录"))) throw fail("浏览器里没有登录小红书", "login");
    if (!data.notes.length) throw fail("没读到笔记列表");
    return data;
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

/** 疑似被拦：暂停 14 天；自动运行降为提醒。未登录：只把自动运行降为提醒 */
async function backOff(e) {
  const st = await loadState();
  const patch = {};
  if (e.kind === "risk") {
    patch.pausedUntil = new Date(Date.now() + LIMITS.pauseDays * DAY).toISOString();
    patch.pauseReason = `${day(new Date().toISOString())} 更新时${e.message}`;
  }
  const wasAuto = (st.schedule || {}).mode === "auto";
  if (wasAuto) patch.schedule = { ...st.schedule, mode: "remind" };
  await chrome.storage.local.set(patch);
  return wasAuto;
}

async function run() {
  const st = await loadState();
  if (blocker(st) || !st.consentAt || running) return render();
  running = true;
  stopped = false;
  $("start").disabled = true;
  $("stop").disabled = false;
  // 一开始就记下时间：中途停止也算一次，避免反复重跑
  await chrome.storage.local.set({ lastBatchAt: new Date().toISOString() });
  chrome.action.setBadgeText({ text: "" });

  const list = shuffle(queue(st));
  let ok = 0, failed = null;
  for (let i = 0; i < list.length && !stopped; i++) {
    const acc = list[i];
    $("state").textContent = `正在更新 ${i + 1}/${list.length} · ${acc.nickname}`;
    try {
      // batch 标记：工作台里删除过的账号，批量更新交来的数据不收
      const data = { ...(await visit(acc)), batch: true };
      await save(JSON.stringify(data, null, 2),
        `xhs-inbox/profile-${data.capturedAt.slice(0, 10)}-${slug(data.nickname || data.userId)}.json`);
      const { accounts = {} } = await chrome.storage.local.get("accounts");
      accounts[acc.userId] = { nickname: data.nickname || acc.nickname, url: data.url,
                               capturedAt: data.capturedAt };
      await chrome.storage.local.set({ accounts });
      ok++;
      log(`${acc.nickname}：${data.notes.length} 条笔记`);
    } catch (e) {
      failed = e;
      log(`${acc.nickname}：${e.message}。已停止，剩下的账号未更新`);
      if (e.kind === "risk" || e.kind === "login") {
        const wasAuto = await backOff(e);
        if (e.kind === "risk") log(`${LIMITS.pauseDays} 天内不再运行`);
        if (wasAuto) log("定期更新已从自动运行改为提醒");
      }
      break;
    }
    if (i < list.length - 1 && !stopped) {
      const wait = gapMs();
      $("state").textContent = `等待 ${Math.round(wait / 1000)} 秒后更新下一个`;
      await pause(wait);
    }
  }
  if (stopped) log("已停止");
  running = false;
  $("stop").disabled = true;
  await chrome.storage.local.set({ lastResult: { at: new Date().toISOString(), ok, total: list.length,
                                                 error: failed ? failed.message : null } });
  await planNext();
  if (AUTO) {
    if (failed && failed.kind === "risk") await notify("对标账号更新已停止", `${failed.message}，${LIMITS.pauseDays} 天内不再运行`);
    else await notify("对标账号已更新", `${ok}/${list.length} 个账号${failed ? ` · ${failed.message}` : ""}`);
  }
  await render();
  $("state").textContent = `已更新 ${ok}/${list.length} 个账号`;
}

/** 定期自动运行：条件都满足时倒数 15 秒开始，期间可取消 */
async function autoStart() {
  const st = await loadState();
  const s = st.schedule || {};
  if (s.mode !== "auto" || !st.consentAt || !st.autoConsentAt || blocker(st)) {
    log("未满足自动运行条件，本次未开始");
    return;
  }
  log("定期自动运行");
  $("stop").disabled = false;
  $("start").disabled = true;
  for (let n = 15; n > 0; n--) {
    $("state").textContent = `${n} 秒后开始，可点「停止」取消`;
    await pause(1000);
    if (stopped) {
      $("stop").disabled = true;
      log("已取消本次自动运行");
      return render();
    }
  }
  run();
}

$("consent").addEventListener("change", async e => {
  if (e.target.checked) await chrome.storage.local.set({ consentAt: new Date().toISOString() });
  else {
    await chrome.storage.local.remove(["consentAt", "autoConsentAt"]);
    const { schedule } = await chrome.storage.local.get("schedule");
    if (schedule && schedule.mode === "auto") await saveSchedule({ mode: "remind" });
  }
  render();
});
$("start").addEventListener("click", run);
$("stop").addEventListener("click", () => { stopped = true; $("stop").disabled = true; });

chrome.action.setBadgeText({ text: "" });
render().then(() => { if (AUTO) autoStart(); });
