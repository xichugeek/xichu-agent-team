"use strict";
(() => {
  const el = id=>document.getElementById(id), esc = escapeHTML;
  const labels = {queued:"等待执行",running:"执行中",done:"处理完成",draft_ready:"草稿就绪",failed:"失败",review_rejected:"质检未过",partial:"部分完成，仍有阻塞",waiting_approval:"等待本人确认",needs_info:"待补充",waiting_publish_link:"等待发布链接",cancelled:"已取消"};
  const colors = s=>["done","draft_ready","ok"].includes(s) ? "green" : ["failed","review_rejected"].includes(s) ? "red" : s === "running" ? "blue" : "gray";
  const badge = (s,label=labels[s] || s)=>`<span class="status-pill" data-tone="${colors(s)}">${esc(label)}</span>`;
  const active = ()=>document.body.dataset.view === "site";
  const siteHref = id=>StudioNavigation.viewHref("site",{site_ticket:id});
  let data = null, selected = new URLSearchParams(location.search).get("site_ticket"), fetching = false, episode = null;
  let listShape = "", detailShape = "", focusNext = false;
  const accountName = id=>data?.accounts.find(a=>a.id === id)?.label || (id === "claude" ? "Claude（历史记录）" : id) || "待分派";

  function list() {
    if (!data) return;
    const query = el("site-search").value.trim().toLowerCase(), filter = el("site-filter").value;
    const rows = data.tickets.filter(t=> {
      if (el("site-episode-only").checked && !t.episodes.some(e=>e.ref === episode?.ref)) return false;
      if (query && !`${t.id} ${t.title} ${t.episodes.map(e=>`${e.id} ${e.title}`).join(" ")}`.toLowerCase().includes(query)) return false;
      if (filter === "done") return ["done","draft_ready"].includes(t.state);
      if (filter === "failed") return ["failed","review_rejected"].includes(t.state);
      if (filter === "active") return !["done","draft_ready","failed","review_rejected","cancelled"].includes(t.state);
      return true;
    });
    el("site-list-note").textContent = `显示 ${rows.length} / ${data.tickets.length} 条${data.total > data.tickets.length ? ` · 官网工单共 ${data.total} 条，仅载入最近 200 条及指定工单` : ""}`;
    const shape = JSON.stringify([rows,selected,data.accounts.map(a=>[a.id,a.label])]);
    if (shape === listShape) return;
    listShape = shape;
    el("site-ticket-list").innerHTML = rows.map(t=>`<a class="ticket-row ${t.id === selected ? "selected" : ""}" data-site-ticket="${esc(t.id)}" href="${esc(siteHref(t.id))}" ${t.id === selected ? 'aria-current="true"' : ""}><div class="ticket-row-top"><strong>${esc(t.id)}</strong>${badge(t.state)}</div><p>${esc(t.title)}</p><div class="ticket-row-foot"><span>${esc(t.stage_label)} · ${esc(accountName(t.executor))}</span><time>${formatTime(t.updated_at)}</time></div>${t.episodes.length ? `<span class="ticket-ep-chip">Episode ${t.episodes.map(e=>esc(e.id)).join(" / ")}</span>` : ""}</a>`).join("") || '<p class="empty-tickets">没有符合条件的官网任务。</p>';
  }

  function detail() {
    const t = data.ticket, shape = JSON.stringify([t,data.accounts.map(a=>[a.id,a.label])]);
    if (shape === detailShape) return;
    detailShape = shape;
    if (!t) { el("site-ticket-detail").innerHTML = '<p class="muted">请选择有效的官网工单。</p>'; return; }
    const artifactCount = t.stages.reduce((n,s)=>n+s.artifacts.length,0);
    const links = t.episodes.map(e=>`<a class="ticket-episode-link" data-episode="${esc(e.ref)}" href="${esc(StudioNavigation.viewHref("studio",{episode:e.ref,panel:"overview"}))}" title="${esc(e.title)}">↗ Episode ${esc(e.id)} <small>${esc(e.source)}</small></a>`).join("");
    el("site-ticket-detail").innerHTML = `<div class="ticket-detail-top"><div><p class="eyebrow">WEBSITE WORK ORDER</p><h3 id="site-detail-title" tabindex="-1">${esc(t.id)} · ${esc(t.stage_label)}</h3></div>${badge(t.state)}</div><div class="ticket-links">${links || '<span class="muted">尚无明确期号关联</span>'}<a class="ticket-episode-link" data-ticket="${esc(t.id)}" href="${esc(StudioNavigation.viewHref("tickets",{ticket:t.id}))}">查看完整工单 →</a></div><div class="ticket-summary"><span>执行账号 <strong>${esc(accountName(t.executor))}</strong></span><span>执行尝试 <strong>${t.attempts} 次</strong></span><span>完成阶段 <strong>${t.completed_stages}</strong></span><span>交付记录 <strong>${artifactCount} 项</strong></span></div><details class="site-request"><summary>原始需求</summary><p class="ticket-request">${esc(t.request)}</p></details>${t.simulated ? '<p class="account-warning">此任务包含模拟执行记录。</p>' : ""}<ol class="stage-timeline">${t.stages.map(s=>`<li data-state="${esc(s.status)}"><div class="stage-heading"><strong>${esc(s.label)}</strong>${badge(s.status,s.status === "ok" ? "本轮交付完成" : labels[s.status] || s.status)}</div><p class="stage-meta">${esc(accountName(s.executor))} · 第 ${s.round} 轮 / 第 ${s.attempt} 次</p><p class="stage-meta">${s.started_at ? "开始 "+formatTime(s.started_at) : "待开始"}${s.finished_at ? " · 结束 "+formatTime(s.finished_at) : s.last_heartbeat ? " · 最近心跳 "+formatTime(s.last_heartbeat) : ""}</p>${s.summary ? `<p class="stage-summary">${esc(s.summary)}</p>` : ""}${s.error ? `<p class="account-warning">${esc(s.error)}</p>` : ""}${s.progress?.length ? `<details ${s.status === "running" ? "open" : ""}><summary>最近执行进度 · 最多 12 行</summary><pre class="progress-log">${esc(s.progress.join("\n"))}</pre></details>` : ""}${s.artifacts.length ? `<details class="site-deliveries" open><summary>本地交付记录 · ${s.artifacts.length} 项</summary><ul class="ticket-artifacts">${s.artifacts.map(a=>`<li><span>${esc(a.name)}</span><small>${size(a.bytes)}</small></li>`).join("")}</ul></details>` : ""}</li>`).join("") || '<li class="muted">等待官网运维开始执行。</li>'}</ol>`;
  }

  function render() {
    const p = data.project, ok = data.status === "ok", n = data.counts;
    el("site-project-state").textContent = p.exists ? (p.wordpress ? "WordPress 官网工作区" : "官网本地工作区") : p.configured ? "官网工作区未找到" : "尚未配置官网工作区";
    el("site-project-path").textContent = p.path || "暂未读取到本地官网目录";
    el("site-project-path").title = p.path || "";
    el("site-pool").textContent = data.account_pool.length ? data.account_pool.map(id=>/^codex_[a-z]$/.test(id) ? id.slice(-1).toUpperCase() : accountName(id)).join(" → ") : "账号池暂不可读";
    el("site-service").textContent = !ok ? "工单数据未连接" : data.scheduler_reachable ? "调度器已监听" : "调度器状态待检查";
    el("site-service").dataset.tone = ok && data.scheduler_reachable ? "green" : "orange";
    el("site-note").textContent = `${data.message}${ok && !data.scheduler_reachable ? " · 显示已保存记录，执行状态可能已过期" : ""}`;
    const metrics = [["官网工单",data.total],["执行中",n.running || 0],["待处理",(n.queued || 0)+(n.partial || 0)+(n.waiting_approval || 0)+(n.needs_info || 0)+(n.waiting_publish_link || 0)],["交付就绪",(n.done || 0)+(n.draft_ready || 0)],["异常任务",(n.failed || 0)+(n.review_rejected || 0)]];
    el("site-counts").innerHTML = metrics.map(([label,num])=>`<div><span>${label}</span><strong>${ok ? num : "—"}</strong></div>`).join("");
    el("site-scan-time").textContent = `最近读取 ${formatTime(data.scanned_at)} · 每 4 秒刷新 · 状态来自官网工单与本地交付记录`;
    list(); detail();
    if (focusNext) { focusNext = false; el("site-detail-title")?.focus({preventScroll:true}); if (innerWidth < 800) el("site-ticket-detail").scrollIntoView({behavior:"smooth",block:"start"}); }
  }

  async function load() {
    if (!active() || fetching) return;
    fetching = true;
    const requested = selected;
    try {
      const response = await fetch(`/api/site${requested ? "?ticket="+encodeURIComponent(requested) : ""}`,{cache:"no-store"});
      if (!response.ok) throw new Error("Cannot read website work orders");
      const next = await response.json();
      if (requested !== selected) return;
      data = next;
      if (!selected && data.ticket) selected = data.ticket.id;
      render();
    } catch (_) {
      el("site-service").textContent = "读取失败"; el("site-service").dataset.tone = "orange";
      el("site-note").textContent = "官网工单连接中断，保留上次记录；请检查本地服务后刷新。";
    } finally {
      fetching = false;
      if (requested !== selected && data?.ticket?.id !== selected) load();
    }
  }
  document.addEventListener("click",event=> {
    const link = event.target.closest("[data-site-ticket]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault(); selected = link.dataset.siteTicket; focusNext = true;
    StudioNavigation.navigate(link.href); list();
  });
  document.addEventListener("workspace-view",()=> {
    if (!active()) return;
    selected = new URLSearchParams(location.search).get("site_ticket");
    load();
  });
  document.addEventListener("studio-snapshot",event=> {
    episode = event.detail.episode;
    el("site-episode-label").textContent = episode ? `仅看第 ${episode.id} 期` : "仅看当前期号";
    list();
  });
  el("site-search").addEventListener("input",list);
  el("site-filter").addEventListener("change",list);
  el("site-episode-only").addEventListener("change",list);
  el("refresh").addEventListener("click",load);
  setInterval(()=>{ if (active() && !document.hidden && el("auto-refresh").checked) load(); },4000);
  document.addEventListener("visibilitychange",()=> { if (!document.hidden && el("auto-refresh").checked) load(); });
  load();
})();
