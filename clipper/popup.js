const $ = id => document.getElementById(id);

const esc = s => String(s ?? "").replace(/[&<>]/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[m]));
const slug = s => (s || "note").replace(/[\\/:*?"<>|\n\r\t]/g, "").trim().slice(0, 40) || "note";

// 笔记页采笔记进采集箱；账号主页采笔记列表，作为对标数据
const MODES = {
  note:    { title: "采集当前笔记", button: "抓取这一篇", fn: "extractNote" },
  profile: { title: "采集账号主页", button: "抓取这个主页", fn: "extractProfile" },
};
const modeOf = url => /xiaohongshu\.com\/user\/profile\//.test(url || "") ? "profile" : "note";

chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
  const m = MODES[modeOf(tab && tab.url)];
  $("title").textContent = m.title;
  $("go").textContent = m.button;
});

/** 记住采集过的账号，「依次更新对标账号」只更新这些 */
async function remember(data) {
  const { accounts = {} } = await chrome.storage.local.get("accounts");
  accounts[data.userId] = { nickname: data.nickname || data.userId, url: data.url,
                            capturedAt: data.capturedAt };
  await chrome.storage.local.set({ accounts });
}

$("batch").addEventListener("click", e => {
  e.preventDefault();
  chrome.tabs.create({ url: chrome.runtime.getURL("batch.html") });
});

function save(text, filename, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: mime + ";charset=utf-8" }));
  return new Promise(resolve => {
    chrome.downloads.download({ url, filename, saveAs: false }, id => {
      setTimeout(() => URL.revokeObjectURL(url), 20000);
      resolve(id);
    });
  });
}

function render(n, files) {
  const p = $("preview");
  p.classList.add("on");
  p.innerHTML = `
    <div class="pt">${esc(n.title || "(无标题)")}</div>
    <div class="meta">
      <span class="pill hi">${esc(n.type || "?")}</span>
      <span class="pill">赞 ${n.stats.likes}</span>
      <span class="pill">藏 ${n.stats.collects}</span>
      <span class="pill">评 ${n.stats.comments}</span>
      ${n.comments.length ? `<span class="pill">评论 ${n.comments.length} 条</span>` : ""}
      ${n.tags.length ? `<span class="pill">话题 ${n.tags.length}</span>` : ""}
    </div>
    <div class="body">${esc((n.body || "(正文为空)").slice(0, 400))}</div>
    ${n.warnings.length ? `<div class="warn">${n.warnings.map(esc).join("<br>")}</div>` : ""}
    <div class="done">已保存 · 稍后出现在工作台采集箱</div>
    <div class="path">${files.map(esc).join("<br>")}</div>`;
}

function renderProfile(a, files) {
  const p = $("preview");
  const top = [...a.notes].sort((x, y) => y.likes - x.likes).slice(0, 5);
  p.classList.add("on");
  p.innerHTML = `
    <div class="pt">${esc(a.nickname || a.userId)}</div>
    <div class="meta">
      <span class="pill hi">账号主页</span>
      ${a.fans ? `<span class="pill">粉丝 ${esc(a.fans)}</span>` : ""}
      <span class="pill">笔记 ${a.notes.length} 条</span>
    </div>
    <div class="body">${top.map(n => `赞 ${n.likes} · ${esc(n.title || "(无标题)")}`).join("\n")}</div>
    ${a.warnings.length ? `<div class="warn">${a.warnings.map(esc).join("<br>")}</div>` : ""}
    <div class="done">已保存 · 稍后出现在工作台对标账号</div>
    <div class="path">${files.map(esc).join("<br>")}</div>`;
}

$("go").addEventListener("click", async () => {
  const btn = $("go"), msg = $("msg");
  btn.disabled = true;
  msg.className = "msg";
  msg.textContent = "抓取中…";

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/xiaohongshu\.com/.test(tab.url || "")) {
      throw new Error("当前页面不是小红书笔记或账号主页");
    }
    const mode = modeOf(tab.url);

    // MAIN world：__INITIAL_STATE__ 只存在于页面自己的上下文里
    const target = { tabId: tab.id };
    // 第一步注入定义，第二步调用——分开写，失败时能看出是哪一步
    await chrome.scripting.executeScript({ target, world: "MAIN", files: ["extract.js"] });
    const [res] = await chrome.scripting.executeScript({
      target, world: "MAIN", func: fn => window[fn](), args: [MODES[mode].fn],
    });

    const data = res && res.result;
    if (!data) throw new Error("没拿到数据，刷新页面再试一次");

    if (mode === "profile") {
      if (!data.userId) throw new Error("没认出账号，刷新主页再试一次");
      if (!data.notes.length) throw new Error("没读到笔记列表，等页面加载完再试一次");
      const files = [`xhs-inbox/profile-${data.capturedAt.slice(0, 10)}-${slug(data.nickname || data.userId)}.json`];
      await save(JSON.stringify(data, null, 2), files[0], "application/json");
      await remember(data);
      msg.textContent = "";
      renderProfile(data, files);
      return;
    }

    const date = (data.publishedAt || data.capturedAt).slice(0, 10);
    // 只落一个 JSON：正文渲染交给工作台，不再另存一份 md
    const files = [`xhs-inbox/${date}-${slug(data.title)}.json`];
    await save(JSON.stringify(data, null, 2), files[0], "application/json");

    msg.textContent = "";
    render(data, files);
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});
