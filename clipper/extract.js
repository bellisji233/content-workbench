/**
 * 在页面主世界里跑，抽取当前小红书笔记。
 *
 * 优先读 window.__INITIAL_STATE__ —— 小红书把笔记结构化数据放在这里，
 * 比 DOM 选择器稳得多（类名是构建时哈希的，随时会变）。
 * 拿不到才退回 DOM 兜底。
 */
function extractNote() {
  const out = {
    capturedAt: new Date().toISOString(),
    url: location.href,
    noteId: null, type: null, title: "", body: "", tags: [],
    author: {}, stats: {}, publishedAt: null,
    images: [], video: null, comments: [],
    source: "unknown", warnings: [],
  };

  const num = v => {
    if (typeof v === "number") return v;
    if (typeof v !== "string") return 0;
    // 小红书会把大数写成「1.2万」
    const w = v.match(/^([\d.]+)\s*万/);
    if (w) return Math.round(parseFloat(w[1]) * 10000);
    const n = parseInt(v.replace(/[^\d]/g, ""), 10);
    return Number.isFinite(n) ? n : 0;
  };

  const idFromUrl = () => {
    const m = location.pathname.match(/\/(?:explore|discovery\/item|search_result)\/([0-9a-f]{16,32})/);
    return m ? m[1] : null;
  };
  out.noteId = idFromUrl();

  // ---------- 首选：__INITIAL_STATE__ ----------
  try {
    const st = window.__INITIAL_STATE__;
    const map = st && st.note && st.note.noteDetailMap;
    if (map) {
      const key = out.noteId && map[out.noteId] ? out.noteId
                : Object.keys(map).find(k => map[k] && map[k].note);
      const note = key && map[key] && map[key].note;
      if (note) {
        out.source = "__INITIAL_STATE__";
        out.noteId = note.noteId || key;
        out.type = note.type === "video" ? "视频" : "图文";
        out.title = note.title || "";
        out.body = note.desc || "";
        out.publishedAt = note.time ? new Date(note.time).toISOString() : null;

        const u = note.user || {};
        out.author = { name: u.nickname || u.nickName || "", userId: u.userId || "" };

        const i = note.interactInfo || {};
        out.stats = {
          likes: num(i.likedCount), collects: num(i.collectedCount),
          comments: num(i.commentCount), shares: num(i.shareCount),
        };

        out.tags = (note.tagList || []).map(t => t.name).filter(Boolean);
        out.images = (note.imageList || [])
          .map(im => im.urlDefault || im.url || (im.infoList || [])[0]?.url)
          .filter(Boolean);

        if (note.video) {
          const streams = note.video.media?.stream || {};
          const first = Object.values(streams).flat().find(s => s && s.masterUrl);
          out.video = { duration: note.video.capa?.duration || null,
                        url: first ? first.masterUrl : null };
        }
      }
    }
  } catch (e) {
    out.warnings.push("读 __INITIAL_STATE__ 失败：" + e.message);
  }

  // ---------- 兜底：DOM ----------
  if (out.source === "unknown") {
    out.source = "DOM";
    out.warnings.push("未拿到 __INITIAL_STATE__，改用 DOM 兜底，字段可能不全");
    const pick = sels => {
      for (const s of sels) {
        const n = document.querySelector(s);
        if (n && n.textContent.trim()) return n.textContent.trim();
      }
      return "";
    };
    out.title = pick(["#detail-title", ".note-content .title", "h1"]);
    out.body  = pick(["#detail-desc", ".note-content .desc", ".note-text"]);
    out.author.name = pick([".author-wrapper .username", ".author .name", 'a[href*="/user/profile/"]']);
    out.stats = {
      likes:    num(pick([".like-wrapper .count", ".interact-container .like .count"])),
      collects: num(pick([".collect-wrapper .count", ".interact-container .collect .count"])),
      comments: num(pick([".chat-wrapper .count", ".interact-container .comment .count"])),
      shares: 0,
    };
    out.tags = [...document.querySelectorAll("#detail-desc .tag, .note-content .tag")]
      .map(n => n.textContent.trim().replace(/^#/, "")).filter(Boolean);
    out.images = [...document.querySelectorAll(".note-slider-img, .swiper-slide img")]
      .map(n => n.src).filter(Boolean);
    out.type = document.querySelector("video") ? "视频" : "图文";
  }

  // ---------- 评论区（始终走 DOM，它是懒加载的）----------
  try {
    const nodes = [...document.querySelectorAll(".comment-item, .parent-comment .comment-inner")];
    out.comments = nodes.slice(0, 20).map(n => {
      const author = n.querySelector(".name, .author-name, .nickname");
      const text = n.querySelector(".note-text, .content, .comment-text");
      const like = n.querySelector(".like .count, .like-wrapper .count");
      return {
        author: author ? author.textContent.trim() : "",
        text: text ? text.textContent.trim() : "",
        likes: like ? num(like.textContent) : 0,
      };
    }).filter(c => c.text);
    if (!out.comments.length) out.warnings.push("没抓到评论——可能还没滚动到评论区");
  } catch (e) {
    out.warnings.push("评论抽取失败：" + e.message);
  }

  if (!out.title && !out.body) out.warnings.push("标题和正文都是空的，这大概不是笔记详情页");
  if (out.type === "视频") out.warnings.push("视频笔记的口播文案不在页面里，需走本地转写链路");
  return out;
}

window.extractNote = extractNote;


/**
 * 在账号主页上跑，抽取主页信息和笔记列表，作为对标数据。
 *
 * 「依次更新对标账号」（batch.js）用的也是这一个函数，产出格式一致，
 * 收件时直接落进 local/data/benchmark/。只读已经加载好的页面，不发任何请求。
 * 笔记列表是滚动加载的：往下滚过的笔记也会在 __INITIAL_STATE__ 里。
 */
function extractProfile() {
  const m = location.pathname.match(/\/user\/profile\/([0-9a-zA-Z]+)/);
  const out = {
    kind: "profile",
    capturedAt: new Date().toISOString(),
    url: location.href,
    userId: m ? m[1] : "",
    ok: false, nickname: "", desc: "", fans: "", follows: "", interaction: "",
    notes: [], source: "unknown", warnings: [],
  };

  const num = v => {
    v = String(v ?? "0");
    const w = v.match(/^([\d.]+)万/);
    if (w) return Math.round(parseFloat(w[1]) * 10000);
    const n = parseInt(v.replace(/[^\d]/g, ""), 10);
    return Number.isFinite(n) ? n : 0;
  };
  // 主页数据是 Vue 的 ref，值包在 _rawValue 里
  const unwrap = v => (v && typeof v === "object" && "_rawValue" in v) ? v._rawValue : v;

  try {
    const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
    if (u) {
      out.source = "__INITIAL_STATE__";
      if (!u.loggedIn) out.warnings.push("未登录，笔记列表可能不全");

      const page = unwrap(u.userPageData) || {};
      const basic = page.basicInfo || {};
      const inter = (page.interactions || []).reduce((a, x) => (a[x.type] = x.count, a), {});
      out.nickname = basic.nickname || "";
      out.desc = basic.desc || "";
      out.fans = inter.fans || "";
      out.follows = inter.follows || "";
      out.interaction = inter.interaction || "";

      // notes 按主页页签分组（笔记 / 收藏 / 点赞），第一组是笔记
      let notes = unwrap(u.notes);
      while (Array.isArray(notes) && notes.length && Array.isArray(notes[0])) notes = notes[0];
      notes = Array.isArray(notes) ? notes : [];
      out.notes = notes.map(n => {
        const c = n.noteCard || {};
        const i = c.interactInfo || {};
        return { id: n.id || c.noteId || "", token: n.xsecToken || c.xsecToken || "",
                 title: c.displayTitle || "", type: c.type || "",
                 time: c.time || 0,
                 likes: num(i.likedCount), collects: num(i.collectedCount),
                 cover: (c.cover || {}).urlDefault || "" };
      });
    }
  } catch (e) {
    out.warnings.push("读主页数据失败：" + e.message);
  }

  out.ok = out.notes.length > 0;
  return out;
}

window.extractProfile = extractProfile;
