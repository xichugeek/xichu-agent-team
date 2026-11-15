"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const esc = value => String(value ?? "—").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const views = {overview:["工作区总览","任务、制作与账号状态，一眼看清。"],tickets:["电报工单","查看任务进度、执行记录与关联期号。"],accounts:["账号额度","查看 A / B / C 的剩余额度与可用重置机会。"],studio:["视频制作","按期号查看制作流程、团队、交付与审核记录。"],site:["官网运维","集中查看官网任务、执行进度与本地草稿交付。"]};
  const panels = ["overview","flow","team","artifacts","checks"];
  const roles = {pm:"项目经理",video:"视频制作",site:"官网运维"};
  let studio = null, agent = null, lastStudioShape = "", lastAgentShape = "";
  function viewHref(view, extra={}) {
    const url = new URL(location.href);
    url.searchParams.set("view",view); url.hash="";
    for (const [k,v] of Object.entries(extra)) url.searchParams.set(k,v);
    return url.pathname+url.search;
  }
  function activeView() {
    const query = new URLSearchParams(location.search), explicit = query.get("view");
    if (views[explicit]) return explicit;
    if (location.hash === "#accounts-heading") return "accounts";
    if (location.hash === "#tickets-heading") return "tickets";
    if (location.hash === "#episode-title") return "studio";
    return query.has("ticket") ? "tickets" : query.has("episode") ? "studio" : "overview";
  }
  function applyRoute() {
    const view = activeView(), raw = new URLSearchParams(location.search).get("panel"), panel = panels.includes(raw) ? raw : "overview";
    document.body.dataset.view = view;
    document.querySelectorAll("[data-workspace-view]").forEach(node=>{ node.hidden = node.dataset.workspaceView !== view; });
    document.querySelectorAll("[data-studio-panel]").forEach(node=>{ node.hidden = node.dataset.studioPanel !== panel; });
    document.querySelectorAll("a[data-view]").forEach(link=> {
      link.href = viewHref(link.dataset.view);
      if (link.closest(".workspace-nav") && link.dataset.view === view) link.setAttribute("aria-current","page");
      else link.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-panel]").forEach(link=> {
      link.href = viewHref("studio",{panel:link.dataset.panel});
      if (link.dataset.panel === panel) link.setAttribute("aria-current","page");
      else link.removeAttribute("aria-current");
    });
    byId("view-title").textContent = views[view][0];
    byId("view-subtitle").textContent = views[view][1];
    document.title = `${views[view][0]} · XichuGeek Studio`;
    document.dispatchEvent(new CustomEvent("workspace-view",{detail:{view,panel}}));
  }
  function navigate(href) {
    const url = new URL(href,location.href);
    if (url.origin !== location.origin) return;
    history.pushState(null,"",url.pathname+url.search+url.hash);
    applyRoute(); window.scrollTo(0,0);
    byId("view-title").focus({preventScroll:true});
  }
  window.StudioNavigation = {navigate,viewHref};
  document.addEventListener("click",event=> {
    const link = event.target.closest("a[data-view],a[data-panel]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault(); navigate(link.href);
  });
  window.addEventListener("popstate",applyRoute);
  const time = value => { const d = new Date(value); return value && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",hour12:false}) : "—"; };
  const ticketTime = value => { const d = new Date(value); return value && !Number.isNaN(d.getTime()) ? new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(d) : "—"; };
  const ticketColor = s => ["done","draft_ready"].includes(s) ? "green" : ["failed","review_rejected"].includes(s) ? "red" : s === "running" ? "blue" : "gray";

  function renderStudio() {
    const ep = studio.episode;
    byId("nav-episode-count").textContent = studio.episodes.filter(e=>!e.test_only).length;
    if (!ep) { byId("home-production").innerHTML = '<p class="muted">尚无制作期号。</p>'; return; }
    byId("home-production").innerHTML = `<p class="home-episode-label">${ep.test_only ? "合成测试" : "EPISODE"} ${esc(ep.id)} <span>文件制作记录</span></p><h3 class="home-episode-title" title="${esc(ep.title)}"><a data-episode="${esc(ep.ref)}" href="${esc(viewHref("studio",{episode:ep.ref,panel:"overview"}))}">${esc(ep.title)}</a></h3><div class="home-progress-label"><span>${esc(ep.stage)} · ${esc(ep.current_role)}</span><strong>${ep.progress}%</strong></div><progress class="home-progress" max="100" value="${ep.progress}" aria-label="制作阶段进度 ${ep.progress}%"></progress><p class="home-next-action" title="${esc(ep.next_action)}"><span>下一步</span>${esc(ep.next_action)}</p>`;
  }
  function renderAgent() {
    const ok = agent.status === "ok", n = agent.counts;
    const counts = [["执行中",n.running || 0,"blue"],["等待人工",(n.waiting_publish_link || 0)+(n.needs_info || 0),"orange"],["排队中",n.queued || 0,"purple"],["异常工单",(n.failed || 0)+(n.review_rejected || 0),"red"]];
    byId("overview-counts").innerHTML = counts.map(([label,num,color])=>`<div data-tone="${color}"><span>${label}</span><strong>${ok ? num : "—"}</strong></div>`).join("");
    byId("nav-ticket-count").textContent = ok ? agent.total : "—";
    byId("nav-account-count").textContent = agent.accounts.length || "—";
    byId("nav-site-count").textContent = ok ? agent.site_total : "—";
    byId("sidebar-service").textContent = ok && agent.scheduler_reachable ? "调度器已监听" : "工单连接待检查";
    byId("home-tickets").innerHTML = agent.tickets.slice(0,3).map(t=>`<a class="home-ticket" data-ticket="${esc(t.id)}" href="${esc(viewHref("tickets",{ticket:t.id}))}"><div><strong>${esc(t.id)}</strong><span class="status-pill" data-tone="${ticketColor(t.state)}">${esc(t.state_label)}</span></div><p title="${esc(t.title)}">${esc(t.title)}</p><small>${esc(t.stage_label)} · ${ticketTime(t.updated_at)}</small></a>`).join("") || `<p class="muted">${ok ? "暂无工单。" : "电报数据源暂不可读。"}</p>`;
    byId("home-accounts").innerHTML = agent.accounts.map(a=> {
      const ready = a.status === "ok", count = a.reset_credits?.available_count;
      return `<a class="home-account" href="${esc(viewHref("accounts"))}" data-view="accounts"><div><strong>${esc(a.label)}</strong><span>${a.primary_roles.map(r=>esc(roles[r] || r)).join(" / ") || "备用"}</span></div><p>${a.windows.length ? a.windows.map(w=>`<span class="home-quota" data-tone="${ready && w.remaining_percent <= 10 ? "red" : "gray"}">${esc(w.label)} <b>${w.remaining_percent == null ? "未知" : esc(w.remaining_percent)+"%"}</b></span>`).join("") : '<span class="muted">额度读取中</span>'}</p><small>${ready ? "可用重置 "+(count == null ? "未知" : count+" 次") : a.status === "loading" ? "读取中…" : "额度读数待更新"}<span aria-hidden="true">↗</span></small></a>`;
    }).join("") || '<p class="muted">未配置账号。</p>';
    byId("home-scan-time").textContent = `${ok ? "工单共 "+agent.total+" 条" : agent.message} · 最近读取 ${time(agent.scanned_at)} · 任务每 4 秒刷新`;
  }
  document.addEventListener("studio-snapshot",event=> {
    const next = event.detail, shape = JSON.stringify([next.episode?.ref,next.episode?.fingerprint,next.episode?.overall_status,next.episode?.progress,next.episode?.next_action,next.episodes]);
    studio = next;
    if (shape !== lastStudioShape) { lastStudioShape = shape; renderStudio(); }
  });
  document.addEventListener("agent-snapshot",event=> {
    agent = event.detail;
    const shape = JSON.stringify([agent.status,agent.counts,agent.total,agent.site_total,agent.tickets,agent.accounts,agent.scheduler_reachable]);
    if (shape !== lastAgentShape) { lastAgentShape = shape; renderAgent(); }
    byId("home-scan-time").textContent = `${agent.status === "ok" ? "工单共 "+agent.total+" 条" : agent.message} · 最近读取 ${time(agent.scanned_at)} · 任务每 4 秒刷新`;
  });
  applyRoute();
})();
