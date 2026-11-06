"use strict";
/* CanBot arayüzü — bağımlılıksız, tek dosya. */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

  const TOKEN_KEY = "canbot_token";
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* yok say */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* yok say */ } },
  };
  let token = store.get(TOKEN_KEY);
  let isDesktopWindow = false;
  try { isDesktopWindow = sessionStorage.getItem("canbot_desktop") === "1"; } catch { /* yok say */ }
  const hashMatch = location.hash.match(/desktop=([^&]+)/);
  if (hashMatch) {
    token = decodeURIComponent(hashMatch[1]);
    store.set(TOKEN_KEY, token);
    isDesktopWindow = true;
    try { sessionStorage.setItem("canbot_desktop", "1"); } catch { /* yok say */ }
    history.replaceState(null, "", location.pathname);
  }

  let state = null;
  let tab = "home";
  let settingsDirty = false;
  let settingsFilled = false;
  let marketFilter = "all";
  let keysMode = "live";
  let selectedMode = null;
  let connectLoaded = false;
  let wizardShown = false;
  let pollTimer = null;
  let detailSymbol = null;
  let detailData = null;

  /* ------------------------------------------------------------ biçimlendirme */
  const nf2 = new Intl.NumberFormat("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const minus = "−";
  function money(v, sign = false) {
    if (v === null || v === undefined || Number.isNaN(+v)) return "—";
    const s = nf2.format(Math.abs(v));
    return (v < 0 ? minus : sign && v > 0 ? "+" : "") + s;
  }
  const usdt = (v, sign = false) => (v === null || v === undefined ? "—" : `${money(v, sign)} USDT`);
  function pct(v, sign = true, d = 2) {
    if (v === null || v === undefined || Number.isNaN(+v)) return "—";
    return (v < 0 ? minus : sign && v > 0 ? "+" : "") + "%" + Math.abs(v).toFixed(d).replace(".", ",");
  }
  function price(v) {
    if (v === null || v === undefined || Number.isNaN(+v)) return "—";
    const a = Math.abs(v);
    const d = a >= 1000 ? 2 : a >= 100 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 5 : 7;
    return new Intl.NumberFormat("tr-TR", { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d }).format(v);
  }
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const base = (sym) => String(sym || "").replace(/USDT$/, "");
  function ago(ms) {
    if (!ms) return "—";
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return "az önce";
    if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
    if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
    return `${Math.floor(s / 86400)} gün önce`;
  }
  function until(ms) {
    const s = Math.max(0, (ms - Date.now()) / 1000);
    if (s < 60) return "birazdan";
    if (s < 3600) return `${Math.ceil(s / 60)} dk sonra`;
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return `${h} sa ${m} dk sonra`;
  }
  const dt = (ms) => (ms ? new Date(ms).toLocaleString("tr-TR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
  const hm = (ms) => new Date(ms).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
  function dur(ms) {
    const m = Math.round(ms / 60000);
    if (m < 60) return `${m} dk`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h} sa ${m % 60} dk`;
    return `${Math.floor(h / 24)} gün ${h % 24} sa`;
  }
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const TF_LABEL = { "5m": "5 dakikalık", "15m": "15 dakikalık", "30m": "30 dakikalık", "1h": "1 saatlik", "2h": "2 saatlik", "4h": "4 saatlik", "1d": "günlük" };
  const tfLabel = (tf) => TF_LABEL[tf] || tf;

  /* ---------------------------------------------------------------- tema */
  function applyTheme(t) {
    if (t === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t || "dark";
  }
  applyTheme(store.get("canbot_theme") || "dark");

  /* ------------------------------------------------------------------ API */
  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    let res;
    try {
      res = await fetch(path, {
        method: opts.method || "GET",
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        cache: "no-store",
      });
    } catch {
      throw new Error("Bota ulaşılamıyor. CanBot çalışıyor mu?");
    }
    let data = null;
    try { data = await res.json(); } catch { /* boş yanıt */ }
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      logout();
      throw new Error("Oturum süresi doldu, tekrar giriş yapın");
    }
    if (!res.ok) {
      const d = data && data.detail;
      throw new Error(typeof d === "string" ? d : `İşlem başarısız (hata ${res.status})`);
    }
    return data;
  }

  /* ------------------------------------------------------------ bildirim */
  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 6500 : 3800);
  }
  function confirmBox(title, text, okText = "Onayla", danger = true) {
    return new Promise((resolve) => {
      $("#modal-title").textContent = title;
      $("#modal-text").textContent = text;
      const ok = $("#modal-ok");
      ok.textContent = okText;
      ok.className = `btn ${danger ? "danger" : "primary"}`;
      $("#modal").classList.remove("hidden");
      const done = (v) => {
        $("#modal").classList.add("hidden");
        ok.onclick = null;
        $("#modal-cancel").onclick = null;
        resolve(v);
      };
      ok.onclick = () => done(true);
      $("#modal-cancel").onclick = () => done(false);
    });
  }
  async function act(btn, fn, okMsg) {
    if (btn) btn.disabled = true;
    try {
      const r = await fn();
      if (okMsg) toast(okMsg, "ok");
      await refresh().catch(() => {});
      return r;
    } catch (e) {
      toast(e.message, "err");
      return undefined;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /* ------------------------------------------------------------- giriş */
  async function showAuth() {
    stopPolling();
    $("#app").classList.add("hidden");
    $("#auth").classList.remove("hidden");
    let st = { has_password: true, can_setup: false };
    try { st = await api("/api/auth/status"); } catch { /* sunucu yoksa normal giriş */ }
    const setup = !st.has_password;
    $("#auth-pass2-wrap").classList.toggle("hidden", !setup);
    $("#auth-pass").autocomplete = setup ? "new-password" : "current-password";
    $("#auth-sub").textContent = setup ? "Panel için bir şifre belirleyin." : "Devam etmek için şifrenizi girin.";
    $("#auth-btn").textContent = setup ? "Şifreyi kaydet ve başla" : "Giriş yap";
    const blocked = setup && !st.can_setup;
    $("#auth-btn").disabled = blocked;
    $("#auth-hint").textContent = blocked
      ? "Henüz şifre belirlenmemiş. Önce bilgisayardaki CanBot'u açın: Ayarlar → Telefon bölümünden şifre belirleyin."
      : setup ? "Bu şifre, telefondan bağlanırken de kullanılır." : "";
    $("#auth-form").onsubmit = async (ev) => {
      ev.preventDefault();
      $("#auth-error").textContent = "";
      const pw = $("#auth-pass").value;
      try {
        if (setup) {
          if (pw !== $("#auth-pass2").value) throw new Error("Şifreler aynı değil");
          const r = await api("/api/auth/setup", { method: "POST", body: { password: pw } });
          token = r.token;
        } else {
          const r = await api("/api/auth/login", { method: "POST", body: { password: pw } });
          token = r.token;
        }
        store.set(TOKEN_KEY, token);
        $("#auth-pass").value = "";
        $("#auth-pass2").value = "";
        await boot();
      } catch (e) {
        $("#auth-error").textContent = e.message;
      }
    };
  }

  function logout() {
    token = null;
    store.del(TOKEN_KEY);
    state = null;
    showAuth();
  }

  async function boot() {
    if (!token) return showAuth();
    try {
      await refresh();
    } catch (e) {
      if (!token) return undefined;
      $("#auth").classList.add("hidden");
      $("#app").classList.remove("hidden");
      toast(e.message, "err");
    }
    $("#auth").classList.add("hidden");
    $("#app").classList.remove("hidden");
    go(tab);
    startPolling();
    if ("serviceWorker" in navigator && window.isSecureContext && !isDesktopWindow) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
    return undefined;
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
      if (!document.hidden && token) refresh().catch(() => {});
    }, 3000);
  }
  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && token) refresh().catch(() => {});
  });

  async function refresh() {
    const s = await api("/api/state");
    state = s;
    render();
    return s;
  }

  /* --------------------------------------------------------------- gezinme */
  const TITLES = { home: "Özet", market: "Piyasa", trades: "İşlemler", test: "Geçmiş testi", settings: "Ayarlar" };
  function buildTabbar() {
    const bar = $("#tabbar");
    bar.innerHTML = "";
    $$("#nav button").forEach((b) => {
      const c = b.cloneNode(true);
      c.addEventListener("click", () => go(c.dataset.tab));
      bar.appendChild(c);
    });
    $$("#nav button").forEach((b) => b.addEventListener("click", () => go(b.dataset.tab)));
  }
  function go(name) {
    tab = name;
    $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}`));
    $$("#nav button, #tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $("#page-title").textContent = TITLES[name] || "";
    window.scrollTo({ top: 0 });
    if (name === "settings") {
      settingsFilled = false;
      loadConnect();
    }
    render();
  }
  document.addEventListener("click", (ev) => {
    const g = ev.target.closest("[data-goto]");
    if (g) go(g.dataset.goto);
  });

  /* ----------------------------------------------------------------- çizim */
  function render() {
    if (!state) return;
    renderChrome();
    if (tab === "home") renderHome();
    else if (tab === "market") renderMarket();
    else if (tab === "trades") renderTrades();
    else if (tab === "test") renderBacktest();
    else if (tab === "settings") renderSettings();
    maybeWizard();
  }

  function renderChrome() {
    const b = state.bot;
    const pill = $("#mode-pill");
    pill.textContent = b.mode === "live" ? "GERÇEK" : b.mode === "testnet" ? "TESTNET" : "SANAL";
    pill.className = `pill ${b.mode}`;
    const dot = $("#conn-dot");
    dot.className = `conn ${b.connected && !b.error ? "ok" : "bad"}`;
    dot.title = b.connected ? "Binance bağlantısı tamam" : `Bağlantı yok: ${b.error || ""}`;
    $("#mini-status").innerHTML = `<span class="dot ${b.auto_trade ? "on" : ""}"></span>${b.auto_trade ? "Otomatik işlem açık" : "Otomatik işlem kapalı"}`;
    $("#version").textContent = `Sürüm ${state.version}`;
  }

  /* ------------------------------------------------------- bağlantı teşhisi */
  function diagHtml(r) {
    const items = (r.steps || []).map((s) => `<li><span class="${s.ok ? "i-ok" : "i-bad"}">${s.ok ? "✓" : "✗"}</span><span>${esc(s.name)}<small>${esc(s.detail)}</small></span></li>`).join("");
    const when = r.time ? `<p class="tiny muted">Test zamanı: ${hm(r.time)}</p>` : "";
    return `<div class="callout ${r.ok ? "ok" : "warn"}"><strong>${esc(r.summary)}</strong><ul class="diag">${items}</ul>${when}</div>`;
  }
  // Hedef seçiciyle verilir: panel her 3 sn'de yenilendiğinde öğe değişse de sonuç doğru yere yazılır.
  async function runDiagnose(selector, btn) {
    const put = (html) => { const el = $(selector); if (el) el.innerHTML = html; };
    if (btn) btn.disabled = true;
    put(`<p class="tiny muted">Bağlantı adım adım test ediliyor (yaklaşık 10 saniye)…</p>`);
    try {
      put(diagHtml(await api("/api/diagnose", { method: "POST" })));
    } catch (e) {
      put(`<div class="callout danger">${esc(e.message)}</div>`);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /* ----------------------------------------------------------------- özet */
  function renderHome() {
    const b = state.bot;
    const alerts = [];
    if (!b.connected) {
      alerts.push(`<div class="callout danger"><strong>Binance bağlantısı yok.</strong><p>${esc(b.error || "Bağlanılıyor…")}</p><button class="btn sm" id="reconnect-btn">Yeniden bağlan</button> <button class="btn sm ghost" id="diag-home-btn">Sorunu teşhis et</button> <button class="btn sm ghost" data-goto="settings">Ayarlara git</button></div>`);
    } else if (b.error) {
      const keyBtn = b.error_kind === "auth" ? ` <button class="btn sm" data-goto="settings">Anahtarları düzenle</button>` : "";
      alerts.push(`<div class="callout warn"><strong>Uyarı:</strong> ${esc(b.error)}${keyBtn} <button class="btn sm ghost" id="diag-home-btn">Sorunu teşhis et</button></div>`);
    }
    if (b.halt) alerts.push(`<div class="callout danger"><strong>Bot kendini durdurdu.</strong><p>${esc(b.halt)} Ayarları gözden geçirip otomatik işlemi yeniden açabilirsiniz.</p></div>`);
    else if (b.guard) alerts.push(`<div class="callout warn">${esc(b.guard)}</div>`);
    if (b.mode === "live" && b.auto_trade) alerts.push(`<div class="callout info">Gerçek hesabınızda otomatik işlem açık. Bot, stop-loss emirleriyle her pozisyonun riskini sınırlar.</div>`);
    if (state.external_positions && state.external_positions.length) {
      const list = state.external_positions.map((p) => `${base(p.symbol)} ${p.side}`).join(", ");
      alerts.push(`<div class="callout info">Hesabınızda botun açmadığı pozisyonlar var (${esc(list)}). Bot bunlara dokunmaz ve bu coinlerde işlem açmaz.</div>`);
    }
    const diagBox = $("#diag-home");
    const keepDiag = diagBox ? diagBox.innerHTML : "";
    $("#alerts").innerHTML = alerts.join("") + (b.connected && !b.error ? "" : `<div id="diag-home">${keepDiag}</div>`);
    const db = $("#diag-home-btn");
    if (db) db.onclick = () => runDiagnose("#diag-home", db);
    const rb = $("#reconnect-btn");
    if (rb) rb.onclick = () => act(rb, () => api("/api/bot/reconnect", { method: "POST" }), "Bağlantı yenilendi");

    const hs = $("#hero-status");
    const trouble = !b.connected || !!b.error;
    const troubleText = b.error_kind === "auth" ? "AÇIK — ama Binance API anahtarını reddediyor" : "AÇIK — ama Binance'e ulaşılamıyor";
    hs.textContent = b.auto_trade
      ? (trouble ? troubleText : "AÇIK — bot kendi işlem yapıyor")
      : "KAPALI — yalnızca izliyor";
    hs.className = `hero-status ${b.auto_trade && !trouble ? "on" : ""}`;
    hs.style.color = b.auto_trade && trouble ? "var(--warn)" : "";
    $("#hero-sub").textContent = b.status_text;
    const tg = $("#auto-toggle");
    if (document.activeElement !== tg) tg.checked = !!b.auto_trade;
    tg.disabled = !b.connected;

    const a = state.account || {};
    const st = state.stats || { all: {}, today: {} };
    const pos = state.positions || [];
    const kpis = [
      ["Toplam bakiye", money(a.equity), "USDT", a.unrealized ? `Açık K/Z ${money(a.unrealized, true)}` : "Vadeli hesap", ""],
      ["Kullanılabilir", money(a.available), "USDT", "Yeni işlem için", ""],
      ["Bugün", money(st.today.pnl, true), "USDT", `${st.today.trades || 0} işlem kapandı`, cls(st.today.pnl)],
      ["Toplam K/Z", money(st.all.pnl, true), "USDT", `${st.all.trades || 0} işlem`, cls(st.all.pnl)],
      ["Kazanma oranı", st.all.trades ? pct(st.all.win_rate, false, 0) : "—", "", st.all.trades ? `${st.all.wins} kazanç / ${st.all.losses} kayıp` : "Henüz işlem yok", ""],
      ["Açık pozisyon", `${pos.length} / ${state.settings.max_open_positions}`, "", `${state.settings.leverage}x kaldıraç, %${String(state.settings.risk_per_trade_pct).replace(".", ",")} risk`, ""],
    ];
    $("#kpis").innerHTML = kpis
      .map(([l, v, u, s, c]) => `<div class="kpi"><div class="k-label">${esc(l)}</div><div class="k-value ${c}">${esc(v)}${u ? ` <small>${esc(u)}</small>` : ""}</div><div class="k-sub">${esc(s)}</div></div>`)
      .join("");

    lineChart($("#equity-chart"), state.equity_curve, { empty: "Bot çalıştıkça bakiye grafiği burada oluşur." });
    $("#market-overview").innerHTML = marketOverviewHtml(state.market);
    renderPositions();
    $("#recent-trades").innerHTML = tradesHtml((state.trades || []).slice(0, 5), "Henüz kapanmış işlem yok.");
  }

  $("#auto-toggle").addEventListener("change", async (ev) => {
    const on = ev.target.checked;
    if (on && state && state.bot.mode === "live") {
      const ok = await confirmBox("Gerçek parayla otomatik işlem", "Bot, Binance vadeli hesabınızda kendi kendine pozisyon açıp kapatacak. Her işlemde stop-loss kullanılır ama kayıp yaşanabilir. Başlatılsın mı?", "Başlat");
      if (!ok) { ev.target.checked = false; return; }
    }
    ev.target.disabled = true;
    try {
      await api("/api/bot/auto", { method: "POST", body: { on } });
      toast(on ? "Otomatik işlem açıldı. Bot uygun fırsatları bekliyor." : "Otomatik işlem kapatıldı. Açık pozisyonların stopları yerinde.", "ok");
      await refresh();
    } catch (e) {
      ev.target.checked = !on;
      toast(e.message, "err");
    } finally {
      ev.target.disabled = false;
    }
  });

  $("#close-all-btn").addEventListener("click", async (ev) => {
    if (!state || !(state.positions || []).length) return toast("Açık pozisyon yok");
    const ok = await confirmBox("Tüm pozisyonları kapat", "Botun açtığı tüm pozisyonlar piyasa fiyatından kapatılacak ve otomatik işlem durdurulacak.", "Hepsini kapat");
    if (!ok) return undefined;
    return act(ev.target, () => api("/api/positions/close-all", { method: "POST", body: { stop: true } }), "Pozisyonlar kapatıldı");
  });

  function renderPositions() {
    const pos = state.positions || [];
    $("#pos-count").textContent = pos.length;
    $("#close-all-btn").classList.toggle("hidden", !pos.length);
    if (!pos.length) {
      const msg = state.bot.auto_trade
        ? "Bot şu an pozisyonda değil; kriterlere uyan bir fırsat bekliyor. Bu normaldir — iyi bir giriş için bazen saatlerce, bazen günlerce beklenir."
        : "Otomatik işlemi açtığınızda bot uygun fırsatlarda burada pozisyon açar.";
      $("#positions").innerHTML = `<div class="empty"><b>Açık pozisyon yok</b>${esc(msg)}</div>`;
    } else {
      $("#positions").innerHTML = pos.map((p) => {
        const long = p.side === "LONG";
        const stopTxt = p.stop_price ? price(p.stop_price) : "—";
        const stopNote = p.breakeven ? " · başabaş" : p.stop_price !== p.initial_stop ? " · iz sürüyor" : "";
        const tpTxt = p.take_profit ? price(p.take_profit) : "İz süren stop";
        const r = p.r_multiple;
        return `<div class="pos">
          <div class="pos-head">
            <div class="sym" data-detail="${esc(p.symbol)}" style="cursor:pointer">${esc(base(p.symbol))}<span class="badge ${long ? "long" : "short"}">${long ? "LONG" : "SHORT"} ${p.leverage}x</span></div>
            <div class="pos-pnl ${cls(p.pnl)}">${p.pnl === null ? "—" : usdt(p.pnl, true)}<small>${pct(p.pnl_pct)} teminat · ${r === null ? "—" : (r >= 0 ? "+" : minus) + Math.abs(r).toFixed(2).replace(".", ",") + "R"}</small></div>
          </div>
          <div class="pos-grid">
            <div><span>Giriş</span>${price(p.entry_price)}</div>
            <div><span>Anlık (mark)</span>${price(p.mark_price)}</div>
            <div><span>Stop-loss</span>${stopTxt}<small class="muted">${stopNote}</small></div>
            <div><span>Hedef</span>${tpTxt}</div>
          </div>
          <div class="pos-foot"><span>${ago(p.entry_time)} açıldı · ${esc(p.qty)} adet · teminat ${money(p.margin)} USDT</span><button class="btn sm danger" data-close-id="${p.id}">Kapat</button></div>
        </div>`;
      }).join("");
    }
    const ext = state.external_positions || [];
    $("#external").innerHTML = ext.length
      ? `<h4>Botun yönetmediği pozisyonlar</h4>${ext.map((p) => `<div class="row"><div class="r-title">${esc(base(p.symbol))} <span class="badge flat">${esc(p.side)}</span></div><div class="r-right ${cls(p.pnl)}">${usdt(p.pnl, true)}</div></div>`).join("")}`
      : "";
  }

  document.addEventListener("click", async (ev) => {
    const c = ev.target.closest("[data-close-id]");
    if (c) {
      const ok = await confirmBox("Pozisyonu kapat", "Bu pozisyon piyasa fiyatından hemen kapatılacak.", "Kapat");
      if (ok) act(c, () => api(`/api/positions/${c.dataset.closeId}/close`, { method: "POST" }), "Pozisyon kapatıldı");
      return;
    }
    const d = ev.target.closest("[data-detail]");
    if (d) openDetail(d.dataset.detail);
  });

  function tradesHtml(trades, emptyMsg) {
    if (!trades.length) return `<div class="empty"><b>Kayıt yok</b>${esc(emptyMsg)}</div>`;
    return `<div class="list">${trades.map((t) => {
      const long = t.side === "LONG";
      return `<div class="row click" data-detail="${esc(t.symbol)}">
        <div class="r-title">${esc(base(t.symbol))}<span class="badge ${long ? "long" : "short"}">${long ? "LONG" : "SHORT"}</span></div>
        <div class="r-right ${cls(t.pnl)}">${usdt(t.pnl, true)}</div>
        <div class="r-sub"><span>${price(t.entry_price)} → ${price(t.exit_price)}</span><span>${esc(t.exit_reason || "")}</span><span>${t.exit_time ? dur(t.exit_time - t.entry_time) : ""}</span><span>${dt(t.exit_time)}</span></div>
      </div>`;
    }).join("")}</div>`;
  }

  function marketOverviewHtml(m) {
    if (!m) return `<div class="empty"><b>Henüz tarama yok</b>Bot ilk taramayı birkaç saniye içinde yapar.</div>`;
    const t = m.total || 1;
    const btc = m.btc;
    const dirBadge = (d) => (d === "LONG" ? `<span class="badge long">↑ Yükseliş</span>` : d === "SHORT" ? `<span class="badge short">↓ Düşüş</span>` : `<span class="badge flat">— Yatay</span>`);
    return `<div class="mood"><strong class="${m.tone === "up" ? "up" : m.tone === "down" ? "down" : ""}">${esc(m.mood)}</strong><span class="tiny muted">${m.total} coin · ${m.signals} sinyal · ort. 24s ${pct(m.avg_change)}</span></div>
      <div class="breadth"><span style="width:${(m.up / t) * 100}%;background:var(--up)"></span><span style="width:${(m.flat / t) * 100}%;background:var(--faint)"></span><span style="width:${(m.down / t) * 100}%;background:var(--down)"></span></div>
      <div class="legend"><span><i style="background:var(--up)"></i>Yükselişte ${m.up}</span><span><i style="background:var(--faint)"></i>Yatay ${m.flat}</span><span><i style="background:var(--down)"></i>Düşüşte ${m.down}</span></div>
      ${btc ? `<div class="btc-line" data-detail="BTCUSDT" style="cursor:pointer"><strong>Bitcoin</strong>${dirBadge(btc.direction)}<span>${price(btc.price)}</span><span class="${cls(btc.change_24h)}">${pct(btc.change_24h)}</span></div>` : ""}`;
  }

  /* --------------------------------------------------------------- piyasa */
  function renderMarket() {
    const b = state.bot;
    $("#scan-info").textContent = b.last_scan
      ? `Son tarama ${hm(b.last_scan)} (${ago(b.last_scan)}) · Sonraki ${until(b.next_scan)}${b.busy ? " · taranıyor…" : ""}`
      : b.busy ? "Taranıyor…" : "Henüz tarama yapılmadı";
    $("#market-overview-2").innerHTML = state.market ? `<div class="callout">${marketOverviewHtml(state.market)}</div>` : "";
    let rows = state.scanner || [];
    if (marketFilter === "signal") rows = rows.filter((r) => r.signal);
    else if (marketFilter === "LONG" || marketFilter === "SHORT") rows = rows.filter((r) => r.direction === marketFilter);
    if (!rows.length) {
      $("#scanner").innerHTML = `<div class="empty"><b>${(state.scanner || []).length ? "Bu filtrede coin yok" : "Henüz veri yok"}</b>${marketFilter === "signal" ? "Şu an giriş sinyali veren coin yok; bot her mum kapanışında yeniden bakar." : ""}</div>`;
      return;
    }
    $("#scanner").innerHTML = rows.map((r, i) => {
      const dir = r.direction === "LONG" ? `<span class="badge long">↑ Yükseliş</span>` : r.direction === "SHORT" ? `<span class="badge short">↓ Düşüş</span>` : `<span class="badge flat">— Yatay</span>`;
      const note = (r.notes || []).filter((n) => n !== "GİRİŞ SİNYALİ").join(" · ");
      return `<div class="scan-row" data-detail="${esc(r.symbol)}">
        <div class="rank">${i + 1}</div>
        <div class="sym">${esc(base(r.symbol))} ${dir}${r.signal ? `<span class="badge signal">SİNYAL</span>` : ""}</div>
        <div class="score"><div class="bar"><span style="width:${Math.min(100, r.score)}%"></span></div><b>${Math.round(r.score)}</b></div>
        <div class="price">${price(r.price)}<small class="${cls(r.change_24h)}">${pct(r.change_24h)}</small></div>
        <div class="note">${esc(note || (r.signal ? "Giriş koşulları sağlandı" : ""))}</div>
      </div>`;
    }).join("");
  }
  $$("#market-filter .chip").forEach((c) => c.addEventListener("click", () => {
    marketFilter = c.dataset.f;
    $$("#market-filter .chip").forEach((x) => x.classList.toggle("active", x === c));
    renderMarket();
  }));
  $("#scan-btn").addEventListener("click", (ev) => act(ev.target, () => api("/api/bot/scan", { method: "POST" }), "Tarama başlatıldı"));

  /* ---------------------------------------------------------- coin detayı */
  async function openDetail(sym) {
    detailSymbol = sym;
    detailData = null;
    $("#sheet-title").textContent = `${base(sym)} / USDT`;
    $("#sheet-content").innerHTML = `<div class="empty"><b>Analiz yükleniyor…</b>Grafik ve göstergeler hesaplanıyor.</div>`;
    $("#sheet").classList.remove("hidden");
    document.body.style.overflow = "hidden";
    try {
      const d = await api(`/api/analysis/${encodeURIComponent(sym)}`);
      if (detailSymbol !== sym) return;
      detailData = d;
      renderDetail();
    } catch (e) {
      $("#sheet-content").innerHTML = `<div class="callout danger">${esc(e.message)}</div>`;
    }
  }
  function closeDetail() {
    detailSymbol = null;
    detailData = null;
    $("#sheet").classList.add("hidden");
    document.body.style.overflow = "";
  }
  $$("#sheet [data-close]").forEach((el) => el.addEventListener("click", closeDetail));
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDetail(); });

  function renderDetail() {
    const d = detailData;
    if (!d) return;
    const a = d.analysis;
    const dir = a.direction === "LONG" ? `<span class="badge long">↑ Yükseliş trendi</span>` : a.direction === "SHORT" ? `<span class="badge short">↓ Düşüş trendi</span>` : `<span class="badge flat">— Trend yok</span>`;
    const t = d.trade;
    const stats = [
      ["Fiyat", price(d.mark || a.price)],
      ["24s değişim", pct(a.change_24h)],
      ["Trend skoru", `${Math.round(a.score)}/100`],
      ["ADX (güç)", a.adx === null ? "—" : Math.round(a.adx)],
      ["RSI", a.rsi === null ? "—" : Math.round(a.rsi)],
      ["Oynaklık (ATR)", a.atr_pct === null ? "—" : pct(a.atr_pct, false)],
      ["Fonlama", pct(a.funding_rate, true, 3)],
    ];
    $("#sheet-content").innerHTML = `
      <div class="chips">${dir}${a.signal ? `<span class="badge signal">GİRİŞ SİNYALİ</span>` : ""}<span class="badge flat">Trend: ${esc(tfLabel(d.trend_timeframe))} · Giriş: ${esc(tfLabel(d.timeframe))}</span></div>
      <div class="table-like">${stats.map(([k, v]) => `<div class="stat"><span>${esc(k)}</span><b class="${k === "24s değişim" ? cls(a.change_24h) : ""}">${esc(v)}</b></div>`).join("")}</div>
      <div class="candle-wrap"><canvas id="candles"></canvas></div>
      <div class="chart-legend"><span><i style="background:var(--accent)"></i>EMA21</span><span><i style="background:var(--up)"></i>Kırılım (yukarı)</span><span><i style="background:var(--down)"></i>Kırılım (aşağı)</span>${t ? `<span><i style="background:var(--warn)"></i>Giriş / stop</span>` : ""}</div>
      ${t ? `<div class="callout info"><strong>Açık pozisyon:</strong> ${t.side} · giriş ${price(t.entry_price)} · stop ${price(t.stop_price)}${t.take_profit ? ` · hedef ${price(t.take_profit)}` : " · iz süren stop"}</div>` : ""}
      <h4>Bot ne düşünüyor?</h4>
      <ul class="explain">${(d.explanation || []).map((x) => `<li class="${esc(x.tone)}">${esc(x.text)}</li>`).join("")}</ul>`;
    requestAnimationFrame(() => drawCandles($("#candles"), d));
  }
  window.addEventListener("resize", () => {
    if (detailData) drawCandles($("#candles"), detailData);
    if (state && tab === "home") lineChart($("#equity-chart"), state.equity_curve, { empty: "Bot çalıştıkça bakiye grafiği burada oluşur." });
  });

  function drawCandles(canvas, d) {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = rect.width;
    const H = rect.height;
    const padR = 70;
    const padB = 22;
    const padT = 10;
    const cs = d.candles || [];
    if (!cs.length) return;
    const up = cssVar("--up");
    const down = cssVar("--down");
    const muted = cssVar("--faint");
    const border = cssVar("--border");
    const accent = cssVar("--accent");
    const warn = cssVar("--warn");
    let lo = Infinity;
    let hi = -Infinity;
    cs.forEach((c) => { lo = Math.min(lo, c[3]); hi = Math.max(hi, c[2]); });
    const lv = d.levels || {};
    const extras = [lv.breakout_high, lv.breakout_low];
    if (d.trade) extras.push(d.trade.entry_price, d.trade.stop_price, d.trade.take_profit);
    extras.forEach((v) => { if (v && v > lo * 0.85 && v < hi * 1.15) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    const span = hi - lo || hi * 0.01 || 1;
    lo -= span * 0.04;
    hi += span * 0.04;
    const y = (p) => padT + ((hi - p) / (hi - lo)) * (H - padT - padB);
    const n = cs.length;
    const cw = (W - padR) / n;
    const x = (i) => i * cw + cw / 2;
    ctx.font = "11px " + getComputedStyle(document.body).fontFamily;
    ctx.textBaseline = "middle";
    // ızgara (son fiyat etiketine çakışan fiyat yazıları atlanır)
    const lastY = y(cs[cs.length - 1][4]);
    for (let k = 0; k <= 4; k++) {
      const p = lo + ((hi - lo) * k) / 4;
      const yy = Math.round(y(p)) + 0.5;
      ctx.strokeStyle = border;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
      if (Math.abs(yy - lastY) > 16) {
        ctx.fillStyle = muted;
        ctx.fillText(price(p), W - padR + 6, yy);
      }
    }
    // zaman etiketleri
    ctx.textBaseline = "alphabetic";
    for (let k = 0; k < 4; k++) {
      const i = Math.floor((n - 1) * (k / 3));
      const label = new Date(cs[i][0]).toLocaleString("tr-TR", { day: "2-digit", month: "short", hour: d.timeframe.endsWith("d") ? undefined : "2-digit", minute: d.timeframe.endsWith("d") ? undefined : "2-digit" });
      ctx.fillStyle = muted;
      ctx.textAlign = k === 0 ? "left" : k === 3 ? "right" : "center";
      ctx.fillText(label, Math.min(Math.max(x(i), 2), W - padR - 2), H - 5);
    }
    ctx.textAlign = "left";
    // mumlar
    const bw = Math.max(1, Math.min(14, cw * 0.62));
    cs.forEach((c, i) => {
      const [, o, h, l, cl] = c;
      const col = cl >= o ? up : down;
      ctx.strokeStyle = col;
      ctx.fillStyle = col;
      ctx.lineWidth = 1;
      const xx = Math.round(x(i)) + 0.5;
      ctx.beginPath(); ctx.moveTo(xx, y(h)); ctx.lineTo(xx, y(l)); ctx.stroke();
      const top = y(Math.max(o, cl));
      const hgt = Math.max(1, Math.abs(y(o) - y(cl)));
      ctx.fillRect(xx - bw / 2, top, bw, hgt);
    });
    // EMA
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    let started = false;
    (d.ema || []).forEach((v, i) => {
      if (v === null || v === undefined) return;
      if (!started) { ctx.moveTo(x(i), y(v)); started = true; } else ctx.lineTo(x(i), y(v));
    });
    ctx.stroke();
    // seviyeler
    const hline = (v, col, dash, label) => {
      if (!v || v < lo || v > hi) return;
      const yy = Math.round(y(v)) + 0.5;
      ctx.save();
      ctx.strokeStyle = col;
      ctx.globalAlpha = 0.85;
      ctx.setLineDash(dash);
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
      ctx.restore();
      if (label) {
        ctx.fillStyle = col;
        ctx.textBaseline = "bottom";
        ctx.fillText(label, 6, yy - 2);
        ctx.textBaseline = "middle";
      }
    };
    hline(lv.breakout_high, up, [5, 4], "kırılım ↑");
    hline(lv.breakout_low, down, [5, 4], "kırılım ↓");
    if (d.trade) {
      hline(d.trade.entry_price, warn, [], "giriş");
      hline(d.trade.stop_price, down, [2, 3], "stop");
      if (d.trade.take_profit) hline(d.trade.take_profit, up, [2, 3], "hedef");
    }
    // son fiyat etiketi
    const last = cs[n - 1][4];
    const ly = y(last);
    ctx.fillStyle = last >= cs[n - 1][1] ? up : down;
    ctx.fillRect(W - padR + 2, ly - 9, padR - 4, 18);
    ctx.fillStyle = "#fff";
    ctx.textBaseline = "middle";
    ctx.fillText(price(last), W - padR + 6, ly);
  }

  /* ------------------------------------------------------- çizgi grafiği */
  function lineChart(el, pts, opts = {}) {
    if (!el) return;
    if (!pts || pts.length < 2) {
      el.innerHTML = `<div class="empty"><b>Henüz veri yok</b>${esc(opts.empty || "")}</div>`;
      return;
    }
    const W = el.clientWidth || 600;
    const H = el.clientHeight || 220;
    const padR = 64;
    const padT = 10;
    const padB = 22;
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    let mn = Math.min(...ys);
    let mx = Math.max(...ys);
    if (mx - mn < 1e-9) { mx += 1; mn -= 1; }
    const pad = (mx - mn) * 0.08;
    mn -= pad; mx += pad;
    const x0 = xs[0];
    const x1 = xs[xs.length - 1];
    const X = (t) => ((t - x0) / (x1 - x0 || 1)) * (W - padR);
    const Y = (v) => padT + ((mx - v) / (mx - mn)) * (H - padT - padB);
    const color = ys[ys.length - 1] >= ys[0] ? cssVar("--up") : cssVar("--down");
    const line = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
    const area = `${line} L${X(x1).toFixed(1)},${H - padB} L${X(x0).toFixed(1)},${H - padB} Z`;
    const gid = `g${Math.random().toString(36).slice(2, 8)}`;
    const labels = [mx - pad, (mx + mn) / 2, mn + pad]
      .map((v) => `<text x="${W - padR + 8}" y="${Y(v).toFixed(1)}" fill="${cssVar("--faint")}" font-size="11" dominant-baseline="middle">${esc(money(v))}</text>`)
      .join("");
    const grid = [mx - pad, (mx + mn) / 2, mn + pad]
      .map((v) => `<line x1="0" x2="${W - padR}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="${cssVar("--border")}" stroke-dasharray="3 4"/>`)
      .join("");
    const fmtD = (t) => new Date(t).toLocaleDateString("tr-TR", { day: "2-digit", month: "short" });
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Bakiye grafiği">
      <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".28"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
      ${grid}<path d="${area}" fill="url(#${gid})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>
      ${labels}
      <text x="0" y="${H - 4}" fill="${cssVar("--faint")}" font-size="11">${esc(fmtD(x0))}</text>
      <text x="${W - padR}" y="${H - 4}" fill="${cssVar("--faint")}" font-size="11" text-anchor="end">${esc(fmtD(x1))}</text>
      <circle id="${gid}c" r="4" fill="${color}" stroke="#fff" stroke-width="1.5" style="display:none"/>
    </svg><div class="tip hidden"></div>`;
    const svg = el.querySelector("svg");
    const tip = el.querySelector(".tip");
    const dotEl = el.querySelector(`#${gid}c`);
    svg.addEventListener("pointermove", (ev) => {
      const r = svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * W;
      let best = 0;
      let bd = Infinity;
      pts.forEach((p, i) => { const dd = Math.abs(X(p[0]) - px); if (dd < bd) { bd = dd; best = i; } });
      const p = pts[best];
      dotEl.setAttribute("cx", X(p[0]));
      dotEl.setAttribute("cy", Y(p[1]));
      dotEl.style.display = "";
      tip.classList.remove("hidden");
      tip.style.left = `${(X(p[0]) / W) * r.width}px`;
      tip.style.top = `${(Y(p[1]) / H) * r.height}px`;
      tip.textContent = `${dt(p[0])} · ${usdt(p[1])}`;
    });
    svg.addEventListener("pointerleave", () => { tip.classList.add("hidden"); dotEl.style.display = "none"; });
  }

  /* -------------------------------------------------------------- işlemler */
  function renderTrades() {
    const s = state.stats.all;
    const items = [
      ["Toplam işlem", s.trades || 0, ""],
      ["Kazanma oranı", s.trades ? pct(s.win_rate, false, 0) : "—", ""],
      ["Toplam K/Z", usdt(s.pnl, true), cls(s.pnl)],
      ["Kâr faktörü", s.profit_factor ? s.profit_factor.toFixed(2).replace(".", ",") : "—", ""],
      ["Ort. kazanç", s.wins ? usdt(s.avg_win, true) : "—", "up"],
      ["Ort. kayıp", s.losses ? usdt(s.avg_loss, true) : "—", "down"],
    ];
    $("#trade-stats").innerHTML = items.map(([l, v, c]) => `<div class="kpi"><div class="k-label">${esc(l)}</div><div class="k-value ${c}">${esc(v)}</div></div>`).join("");
    $("#trade-list").innerHTML = tradesHtml(state.trades || [], "Kapanan işlemler burada listelenir.");
    $("#logs").innerHTML = (state.logs || []).length
      ? state.logs.map((l) => `<div class="log ${esc(l.level)}"><time>${esc(new Date(l.ts).toLocaleString("tr-TR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }))}</time><span>${esc(l.message)}</span></div>`).join("")
      : `<div class="empty">Günlük boş</div>`;
  }

  /* ----------------------------------------------------------------- test */
  function renderBacktest() {
    const bt = state.backtest || { status: "idle" };
    const running = bt.status === "running";
    $("#bt-run").disabled = running;
    $("#bt-progress").classList.toggle("hidden", !running);
    if (running) {
      $("#bt-bar").style.width = `${Math.round((bt.progress || 0) * 100)}%`;
      $("#bt-msg").textContent = bt.message || "";
    }
    if (bt.status === "error") {
      $("#bt-result").innerHTML = `<div class="callout danger">${esc(bt.message)}</div>`;
      return;
    }
    if (bt.status !== "done" || !bt.result) {
      if (!running) $("#bt-result").innerHTML = `<div class="card"><div class="empty"><b>Henüz test yapılmadı</b>Süre ve coin sayısını seçip “Testi başlat”a basın. Test birkaç dakika sürebilir.</div></div>`;
      return;
    }
    if ($("#bt-result").dataset.key === JSON.stringify([bt.result.end_balance, bt.result.trades, bt.result.days])) return;
    const r = bt.result;
    $("#bt-result").dataset.key = JSON.stringify([r.end_balance, r.trades, r.days]);
    const kp = [
      ["Getiri", pct(r.return_pct), cls(r.return_pct), `${money(r.end_balance - r.start_balance, true)} USDT`],
      ["En büyük düşüş", pct(-r.max_drawdown_pct, true, 1), "down", "Zirveden en derin düşüş"],
      ["İşlem", String(r.trades), "", `Long ${r.long_trades} · Short ${r.short_trades}`],
      ["Kazanma oranı", pct(r.win_rate, false, 0), "", `${r.wins} kazanç`],
      ["Kâr faktörü", r.profit_factor ? r.profit_factor.toFixed(2).replace(".", ",") : "—", "", "1'in üstü kârlı"],
      ["Ortalama R", (r.avg_r >= 0 ? "+" : minus) + Math.abs(r.avg_r).toFixed(2).replace(".", ","), cls(r.avg_r), "İşlem başına risk katı"],
    ];
    const per = Object.entries(r.per_symbol || {});
    const best = per.filter(([, v]) => v > 0).slice(0, 5);
    const worst = per.filter(([, v]) => v < 0).sort((x, y) => x[1] - y[1]).slice(0, 5);
    $("#bt-result").innerHTML = `
      <div class="callout ${r.return_pct >= 0 ? "ok" : "danger"}"><strong>${String(r.days).replace(".", ",")} günde ${money(r.start_balance)} → ${money(r.end_balance)} USDT (${pct(r.return_pct)})</strong>
        <p class="tiny">İşlem başı %${String(state.settings.risk_per_trade_pct).replace(".", ",")} risk, ${state.settings.leverage}x kaldıraç, ${r.symbols.length} coin. Komisyon: ${money(r.total_fees)} USDT. Üst üste en fazla ${r.max_losing_streak} kayıp.${r.halted ? " Test sırasında en büyük düşüş limiti aşıldı ve bot durdu." : ""}</p></div>
      <div class="kpis">${kp.map(([l, v, c, sub]) => `<div class="kpi"><div class="k-label">${esc(l)}</div><div class="k-value ${c}">${esc(v)}</div><div class="k-sub">${esc(sub)}</div></div>`).join("")}</div>
      <div class="card"><div class="card-head"><h3>Test bakiye eğrisi</h3></div><div class="chart" id="bt-chart"></div></div>
      <div class="grid-2">
        <div class="card"><div class="card-head"><h3>Çıkış nedenleri</h3></div><div class="tag-list">${Object.entries(r.exit_reasons || {}).map(([k, v]) => `<span class="tag">${esc(k)}: <b>${v}</b></span>`).join("")}</div>
          <h4>En kârlı coinler</h4>${best.length ? best.map(([k, v]) => `<div class="row"><div class="r-title">${esc(base(k))}</div><div class="r-right ${cls(v)}">${usdt(v, true)}</div></div>`).join("") : `<p class="tiny muted">Bu dönemde kâr eden coin olmadı.</p>`}
          ${worst.length ? `<h4>En zararlı coinler</h4>${worst.map(([k, v]) => `<div class="row"><div class="r-title">${esc(base(k))}</div><div class="r-right ${cls(v)}">${usdt(v, true)}</div></div>`).join("")}` : ""}
        </div>
        <div class="card"><div class="card-head"><h3>Önemli notlar</h3></div><ul class="explain">
          <li class="warn">Geçmiş sonuçlar geleceği garanti etmez. Piyasa koşulları değişir.</li>
          <li class="flat">Fonlama ücretleri teste dahil değildir; komisyon (%0,05) ve kayma dahildir.</li>
          <li class="flat">Coinler bugünkü hacme göre seçildiği için sonuçlar biraz iyimser olabilir.</li>
          <li class="flat">Riski artırmak kazancı değil, en çok kayıpları büyütür.</li>
        </ul></div>
      </div>
      <div class="card"><div class="card-head"><h3>Test işlemleri (son 200)</h3></div>${tradesHtml((r.trade_list || []).map((t) => ({ ...t, exit_reason: t.reason })), "")}</div>`;
    lineChart($("#bt-chart"), r.equity_curve, {});
  }
  $("#bt-run").addEventListener("click", (ev) => act(ev.target, () => api("/api/backtest", { method: "POST", body: { days: +$("#bt-days").value, symbols: +$("#bt-symbols").value } }), "Test başladı"));

  /* -------------------------------------------------------------- ayarlar */
  const MODE_DESC = {
    paper: "Sanal: Gerçek Binance fiyatlarıyla, sanal bakiyeyle işlem yapar. Para riske girmez. Başlangıç için önerilir.",
    testnet: "Testnet: Binance'in deneme ortamında, deneme parasıyla gerçek emir akışını dener. Testnet API anahtarı gerekir.",
    live: "Gerçek: Binance vadeli hesabınızda gerçek parayla otomatik işlem yapar. Gerçek hesap API anahtarı gerekir.",
  };
  const TF = [["15m", "15 dakika"], ["30m", "30 dakika"], ["1h", "1 saat"], ["2h", "2 saat"], ["4h", "4 saat"], ["1d", "1 gün"]];
  const FIELDS = [
    { g: "Risk yönetimi" },
    { k: "risk_per_trade_pct", l: "İşlem başına risk", u: "%", step: 0.1, h: "Stop olursa bakiyenin en fazla bu kadarı kaybedilir. %1 önerilir." },
    { k: "leverage", l: "Kaldıraç", u: "x", step: 1, h: "Riski değil, kullanılan teminatı belirler. Stop çok uzaksa bot kaldıracı kendisi düşürür." },
    { k: "max_open_positions", l: "En fazla açık pozisyon", step: 1, h: "Aynı anda açılabilecek pozisyon sayısı." },
    { k: "max_position_margin_pct", l: "Pozisyon başına en fazla teminat", u: "%", step: 1, h: "Tek pozisyona ayrılabilecek en yüksek bakiye oranı." },
    { k: "daily_loss_limit_pct", l: "Günlük zarar limiti", u: "%", step: 0.5, h: "Gün içinde bu kadar kayıpta yeni işlem açılmaz (UTC gece yarısı sıfırlanır)." },
    { k: "max_drawdown_pct", l: "En büyük düşüş limiti", u: "%", step: 1, h: "Bakiye zirveden bu kadar düşerse bot kendini durdurur." },
    { k: "margin_type", l: "Marjin tipi", type: "select", opts: [["ISOLATED", "İzole (önerilen)"], ["CROSSED", "Çapraz"]], h: "İzole: her pozisyonun riski kendi teminatıyla sınırlı." },
    { g: "Strateji" },
    { k: "trend_timeframe", l: "Trend zaman dilimi", type: "select", opts: TF, h: "Ana trend bu grafikte belirlenir (önerilen: 1 gün)." },
    { k: "entry_timeframe", l: "Giriş zaman dilimi", type: "select", opts: TF, h: "Giriş anı bu grafikte aranır (önerilen: 4 saat)." },
    { k: "entry_mode", l: "Giriş tipi", type: "select", opts: [["breakout", "Kırılım (önerilen)"], ["pullback", "Geri çekilme"]], h: "Kırılım: son 20 mumun zirvesi/dibi aşılınca girer." },
    { k: "stop_atr_mult", l: "Stop mesafesi", u: "×ATR", step: 0.1, h: "Stop, ortalama oynaklığın (ATR) bu katı kadar uzağa konur." },
    { k: "trailing_atr_mult", l: "İz süren stop", u: "×ATR", step: 0.1, h: "Fiyat lehe gittikçe stop arkadan gelir. 0 = kapalı." },
    { k: "take_profit_r", l: "Sabit kâr hedefi", u: "R", step: 0.5, h: "Riskin katı olarak hedef. 0 = yok (iz süren stop kârı korur)." },
    { k: "breakeven_at_r", l: "Başabaşa çek", u: "R", step: 0.5, h: "Kâr bu seviyeye gelince stop giriş fiyatına çekilir. 0 = kapalı." },
    { k: "max_hold_hours", l: "En uzun tutma süresi", u: "saat", step: 1, h: "0 = sınırsız." },
    { k: "min_score", l: "En düşük trend skoru", step: 1, h: "Bu skorun altındaki coinlerde işlem açılmaz." },
    { k: "cooldown_minutes", l: "Zarar sonrası bekleme", u: "dk", step: 5, h: "Bir coinde zarar edilince o coin bu süre boyunca atlanır." },
    { k: "max_funding_rate_pct", l: "En yüksek fonlama oranı", u: "%", step: 0.01, h: "Fonlama bu değerden yüksekse o yönde girilmez." },
    { g: "Coin seçimi" },
    { k: "universe_size", l: "Taranacak coin sayısı", step: 1, h: "Hacme göre en büyük N coin taranır." },
    { k: "min_quote_volume_usdt", l: "En az 24 saatlik hacim", u: "M USDT", step: 1, scale: 1e6, h: "Düşük hacimli coinler atlanır." },
    { k: "allow_long", l: "Long (yükselişe) işlem", type: "bool" },
    { k: "allow_short", l: "Short (düşüşe) işlem", type: "bool" },
    { k: "symbol_whitelist", l: "Yalnızca bu coinler", type: "list", h: "Boş bırakırsanız bot en likit coinleri kendisi seçer. Örn: BTC, ETH, SOL" },
    { k: "symbol_blacklist", l: "Hariç tutulan coinler", type: "list", h: "Bu coinlerde asla işlem açılmaz." },
    { g: "Sanal hesap" },
    { k: "paper_start_balance", l: "Sanal başlangıç bakiyesi", u: "USDT", step: 100, h: "Sanal hesabı sıfırladığınızda kullanılır." },
  ];
  const RECOMMENDED = {
    risk_per_trade_pct: 1, leverage: 5, max_open_positions: 3, max_position_margin_pct: 30, daily_loss_limit_pct: 5,
    max_drawdown_pct: 25, margin_type: "ISOLATED", trend_timeframe: "1d", entry_timeframe: "4h", entry_mode: "breakout",
    stop_atr_mult: 2.5, trailing_atr_mult: 3, take_profit_r: 0, breakeven_at_r: 0, max_hold_hours: 0, min_score: 50,
    cooldown_minutes: 60, max_funding_rate_pct: 0.05, universe_size: 20, min_quote_volume_usdt: 50000000,
    allow_long: true, allow_short: true,
  };
  const PRESETS = {
    careful: { risk_per_trade_pct: 0.5, leverage: 3, max_open_positions: 2, daily_loss_limit_pct: 3, max_drawdown_pct: 15 },
    balanced: { risk_per_trade_pct: 1, leverage: 5, max_open_positions: 3, daily_loss_limit_pct: 5, max_drawdown_pct: 25 },
    aggressive: { risk_per_trade_pct: 2, leverage: 5, max_open_positions: 4, daily_loss_limit_pct: 8, max_drawdown_pct: 35 },
  };

  function buildSettingsForm() {
    const bounds = (state && state.bounds) || {};
    let html = "";
    let open = false;
    FIELDS.forEach((f) => {
      if (f.g) {
        if (open) html += "</div>";
        html += `<div class="group-title">${esc(f.g)}</div><div class="fields-grid">`;
        open = true;
        return;
      }
      const id = `f-${f.k}`;
      if (f.type === "bool") {
        html += `<label class="row-toggle"><span><strong>${esc(f.l)}</strong></span><span class="switch"><input type="checkbox" id="${id}" data-k="${f.k}"><span class="slider"></span></span></label>`;
      } else if (f.type === "select") {
        html += `<label class="field"><span>${esc(f.l)}</span><select id="${id}" data-k="${f.k}">${f.opts.map(([v, t]) => `<option value="${v}">${esc(t)}</option>`).join("")}</select>${f.h ? `<small>${esc(f.h)}</small>` : ""}</label>`;
      } else if (f.type === "list") {
        html += `<label class="field"><span>${esc(f.l)}</span><textarea id="${id}" data-k="${f.k}" rows="2" spellcheck="false"></textarea>${f.h ? `<small>${esc(f.h)}</small>` : ""}</label>`;
      } else {
        const b = bounds[f.k];
        const sc = f.scale || 1;
        const attrs = b ? `min="${b[0] / sc}" max="${b[1] / sc}"` : "";
        html += `<label class="field"><span>${esc(f.l)}</span><div class="input-unit"><input type="number" inputmode="decimal" id="${id}" data-k="${f.k}" step="${f.step || "any"}" ${attrs}>${f.u ? `<em>${esc(f.u)}</em>` : ""}</div>${f.h ? `<small>${esc(f.h)}</small>` : ""}</label>`;
      }
    });
    if (open) html += "</div>";
    $("#settings-fields").innerHTML = html;
    $$("#settings-fields [data-k]").forEach((el) => el.addEventListener("input", () => { settingsDirty = true; }));
  }

  function fillSettings(s) {
    FIELDS.forEach((f) => {
      if (!f.k) return;
      const el = $(`#f-${f.k}`);
      if (!el) return;
      const v = s[f.k];
      if (f.type === "bool") el.checked = !!v;
      else if (f.type === "list") el.value = (v || []).map(base).join(", ");
      else if (f.type === "select") el.value = v;
      else el.value = v === null || v === undefined ? "" : +(v / (f.scale || 1)).toFixed(6);
    });
    settingsDirty = false;
  }

  function collectSettings() {
    const out = {};
    FIELDS.forEach((f) => {
      if (!f.k) return;
      const el = $(`#f-${f.k}`);
      if (!el) return;
      if (f.type === "bool") out[f.k] = el.checked;
      else if (f.type === "list") out[f.k] = el.value;
      else if (f.type === "select") out[f.k] = el.value;
      else {
        if (el.value === "") throw new Error(`${f.l} boş olamaz`);
        out[f.k] = parseFloat(String(el.value).replace(",", ".")) * (f.scale || 1);
      }
    });
    return out;
  }

  function renderSettings() {
    const s = state.settings;
    if (!$("#settings-fields").children.length) buildSettingsForm();
    const editing = $("#settings-form").contains(document.activeElement);
    if (!settingsDirty && (!settingsFilled || !editing)) fillSettings(s);
    settingsFilled = true;
    // mod
    if (!selectedMode) selectedMode = s.mode;
    $$("#mode-seg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === selectedMode));
    $("#mode-desc").textContent = MODE_DESC[selectedMode] + (selectedMode === s.mode ? " (şu an bu modda)" : "");
    $("#live-confirm").classList.toggle("hidden", !(selectedMode === "live" && s.mode !== "live"));
    $("#mode-save").disabled = selectedMode === s.mode;
    // anahtarlar
    const k = state.keys[keysMode];
    $$("#keys-seg button").forEach((b) => b.classList.toggle("active", b.dataset.k === keysMode));
    $("#keys-status").textContent = k && k.set ? `Kayıtlı anahtar: ${k.api_key} (Secret gizli tutulur)` : "Henüz anahtar girilmedi.";
    // hazır ayarlar
    const match = Object.entries(PRESETS).find(([, p]) => Object.entries(p).every(([kk, v]) => +s[kk] === +v));
    $$("#presets button").forEach((b) => b.classList.toggle("active", !!match && b.dataset.preset === match[0]));
    $("#paper-card").classList.toggle("hidden", s.mode !== "paper");
    // masaüstü
    const d = state.desktop;
    $("#desktop-card").classList.toggle("hidden", !d);
    if (d) {
      $("#opt-autostart").checked = !!d.autostart;
      $("#opt-autostart").disabled = !d.autostart_supported;
      $("#opt-awake").checked = !!d.keep_awake;
      $("#opt-notify").checked = !!d.notifications;
    }
    // şifre
    $("#pw-old-wrap").classList.toggle("hidden", !state.has_password);
    $("#pw-title").textContent = state.has_password ? "Şifreyi değiştir" : "Telefon girişi için şifre belirleyin";
    $("#logout-btn").classList.toggle("hidden", isDesktopWindow);
    if (!$("#theme-select")) {
      const sel = document.createElement("label");
      sel.className = "field";
      sel.innerHTML = `<span>Tema</span><select id="theme-select"><option value="dark">Koyu</option><option value="light">Açık</option><option value="system">Sistem</option></select>`;
      $("#logout-btn").closest(".card").insertBefore(sel, $("#logout-btn").closest(".actions"));
      $("#theme-select").value = store.get("canbot_theme") || "dark";
      $("#theme-select").addEventListener("change", (ev) => { store.set("canbot_theme", ev.target.value); applyTheme(ev.target.value); render(); });
    }
  }

  $$("#mode-seg button").forEach((b) => b.addEventListener("click", () => { selectedMode = b.dataset.mode; renderSettings(); }));
  $("#mode-save").addEventListener("click", async (ev) => {
    const body = { mode: selectedMode };
    if (selectedMode === "live") body.confirm = $("#live-confirm-input").value.trim();
    const r = await act(ev.target, () => api("/api/settings", { method: "PUT", body }), "Mod değiştirildi. Otomatik işlem güvenlik için kapatıldı.");
    if (r) { $("#live-confirm-input").value = ""; settingsDirty = false; }
  });
  $$("#keys-seg button").forEach((b) => b.addEventListener("click", () => {
    keysMode = b.dataset.k;
    $("#api-key").value = "";
    $("#api-secret").value = "";
    $("#keys-result").innerHTML = "";
    renderSettings();
  }));
  $("#keys-save").addEventListener("click", async (ev) => {
    const key = $("#api-key").value.trim();
    const secret = $("#api-secret").value.trim();
    if (!key || !secret) return toast("API Key ve Secret Key girin", "err");
    const r = await act(ev.target, () => api("/api/keys", { method: "PUT", body: { mode: keysMode, api_key: key, api_secret: secret } }), "Anahtarlar kaydedildi");
    if (r) {
      $("#api-key").value = "";
      $("#api-secret").value = "";
      testKeys(ev.target);
    }
    return undefined;
  });
  async function testKeys(btn) {
    $("#keys-result").innerHTML = `<p class="tiny muted">Binance'e bağlanılıyor…</p>`;
    try {
      const r = await api("/api/keys/test", { method: "POST", body: { mode: keysMode } });
      const warn = (r.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
      $("#keys-result").innerHTML = `<div class="callout ${r.ok ? (warn ? "warn" : "ok") : "danger"}"><strong>${esc(r.message)}</strong>${warn ? `<ul>${warn}</ul>` : ""}</div>`;
    } catch (e) {
      $("#keys-result").innerHTML = `<div class="callout danger">${esc(e.message)}</div>`;
    }
    if (btn) btn.disabled = false;
  }
  $("#keys-test").addEventListener("click", (ev) => testKeys(ev.target));
  $("#keys-diag").addEventListener("click", (ev) => runDiagnose("#keys-result", ev.target));
  $("#keys-clear").addEventListener("click", async (ev) => {
    const ok = await confirmBox("Anahtarları sil", "Bu moda ait API anahtarları bu bilgisayardan silinecek.", "Sil");
    if (ok) act(ev.target, () => api("/api/keys", { method: "PUT", body: { mode: keysMode, api_key: "", api_secret: "" } }), "Anahtarlar silindi");
  });
  $$("#presets button").forEach((b) => b.addEventListener("click", () => act(b, () => api("/api/settings", { method: "PUT", body: PRESETS[b.dataset.preset] }), "Risk seviyesi uygulandı").then(() => { settingsDirty = false; })));
  $("#settings-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    let body;
    try { body = collectSettings(); } catch (e) { toast(e.message, "err"); return; }
    const r = await act(ev.submitter, () => api("/api/settings", { method: "PUT", body }), "Ayarlar kaydedildi");
    if (r) { settingsDirty = false; fillSettings(r.settings); }
  });
  $("#defaults-btn").addEventListener("click", async (ev) => {
    const ok = await confirmBox("Önerilen ayarlar", "Risk ve strateji ayarları, testlerde kullanılan önerilen değerlere döndürülecek.", "Uygula", false);
    if (ok) {
      const r = await act(ev.target, () => api("/api/settings", { method: "PUT", body: RECOMMENDED }), "Önerilen ayarlar uygulandı");
      if (r) { settingsDirty = false; fillSettings(r.settings); }
    }
  });
  $("#paper-reset").addEventListener("click", async (ev) => {
    const ok = await confirmBox("Sanal hesabı sıfırla", "Sanal pozisyonlar kapatılır, sanal bakiye ve sanal işlem geçmişi silinir.", "Sıfırla");
    if (ok) act(ev.target, () => api("/api/paper/reset", { method: "POST" }), "Sanal hesap sıfırlandı");
  });
  ["opt-autostart", "opt-awake", "opt-notify"].forEach((id) => $(`#${id}`).addEventListener("change", (ev) => {
    const body = { autostart: $("#opt-autostart").checked, keep_awake: $("#opt-awake").checked, notifications: $("#opt-notify").checked };
    act(ev.target, () => api("/api/desktop", { method: "PUT", body }), "Kaydedildi");
  }));
  $("#pw-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const r = await act(ev.submitter, () => api("/api/auth/password", { method: "POST", body: { old_password: $("#pw-old").value, new_password: $("#pw-new").value } }), "Şifre kaydedildi");
    if (r && r.token) { token = r.token; store.set(TOKEN_KEY, token); $("#pw-old").value = ""; $("#pw-new").value = ""; }
  });
  $("#tg-save").addEventListener("click", (ev) => act(ev.target, () => api("/api/telegram", { method: "PUT", body: { token: $("#tg-token").value, chat_id: $("#tg-chat").value } }), "Telegram bilgileri kaydedildi"));
  $("#tg-test").addEventListener("click", async (ev) => {
    const r = await act(ev.target, () => api("/api/telegram/test", { method: "POST" }));
    if (r) toast(r.message, r.ok ? "ok" : "err");
  });
  $("#logout-btn").addEventListener("click", () => logout());

  async function loadConnect() {
    if (connectLoaded) return;
    try {
      const c = await api("/api/connect");
      $("#lan-url").textContent = c.url;
      $("#qr").innerHTML = c.qr_svg || "";
      connectLoaded = true;
    } catch { /* sonra tekrar denenir */ }
  }

  /* ------------------------------------------------------- ilk kurulum */
  const wz = { step: 0, mode: "paper", preset: "balanced", keysOk: false, liveOk: false };
  function maybeWizard() {
    if (wizardShown || !state || state.settings.wizard_done) return;
    wizardShown = true;
    wz.step = 0;
    renderWizard();
    $("#wizard").classList.remove("hidden");
  }
  function wzSteps() {
    return `<div class="wz-steps">${[0, 1, 2, 3].map((i) => `<span class="${i <= wz.step ? "on" : ""}"></span>`).join("")}</div>`;
  }
  function renderWizard() {
    const c = $("#wizard-card");
    if (wz.step === 0) {
      c.innerHTML = `${wzSteps()}<h3 id="wz-title">CanBot'a hoş geldiniz 👋</h3>
        <p class="muted">CanBot, Binance vadeli işlemlerde sizin yerinize piyasayı tarar, güçlü trendi olan coinleri bulur ve kurallara uyan fırsatlarda kendi kendine işlem açıp kapatır.</p>
        <ul class="explain">
          <li class="up">Her işleme borsada stop-loss koyar; bilgisayar kapansa bile koruma Binance'te durur.</li>
          <li class="up">Günlük zarar ve en büyük düşüş limitine gelince kendini durdurur.</li>
          <li class="warn">Kâr garantisi yoktur. Kaldıraçlı işlem kayıp riski taşır; kaybetmeyi göze alamayacağınız parayla kullanmayın.</li>
        </ul>
        <div class="actions end"><button class="btn primary" id="wz-next">Başlayalım</button></div>`;
      $("#wz-next").onclick = () => { wz.step = 1; renderWizard(); };
    } else if (wz.step === 1) {
      c.innerHTML = `${wzSteps()}<h3 id="wz-title">Nasıl başlamak istersiniz?</h3>
        <div class="wz-choice">
          <button data-m="paper" class="${wz.mode === "paper" ? "active" : ""}"><strong>Sanal hesapla dene (önerilen)</strong><span>Gerçek fiyatlarla, ${money(state.settings.paper_start_balance)} USDT sanal bakiye. Para riske girmez.</span></button>
          <button data-m="live" class="${wz.mode === "live" ? "active" : ""}"><strong>Binance hesabımı bağla</strong><span>Gerçek parayla işlem. API anahtarı gerekir.</span></button>
        </div>
        <div id="wz-live" class="${wz.mode === "live" ? "" : "hidden"}">
          <label class="field"><span>API Key</span><input id="wz-key" type="text" autocomplete="off" spellcheck="false"></label>
          <label class="field"><span>Secret Key</span><input id="wz-secret" type="password" autocomplete="off" spellcheck="false"></label>
          <p class="tiny muted">Binance → profil → Hesap → API Yönetimi → API Oluştur (Sistem tarafından oluşturulan). “Yalnızca güvenilir IP'lere erişimi kısıtla”yı seçip IP adresinizi ekleyin (zorunlu), sonra “Okuma” ve “Vadeli İşlemler” izinlerini açın; <b>para çekme iznini açmayın</b>. Ayrıntılı adımlar: Ayarlar → Binance API anahtarları.</p>
          <div class="actions"><button class="btn" id="wz-test">Test et ve kaydet</button></div>
          <div id="wz-keys-result"></div>
          <label class="row-toggle"><span><strong>Riskleri anladım</strong><small>Bot gerçek hesabımda otomatik işlem yapabilir; kayıp yaşayabilirim.</small></span><span class="switch"><input type="checkbox" id="wz-live-ok" ${wz.liveOk ? "checked" : ""}><span class="slider"></span></span></label>
        </div>
        <div class="actions end"><button class="btn ghost" id="wz-back">Geri</button><button class="btn primary" id="wz-next">Devam</button></div>`;
      $$(".wz-choice button", c).forEach((b) => (b.onclick = () => { wz.mode = b.dataset.m; renderWizard(); }));
      $("#wz-back").onclick = () => { wz.step = 0; renderWizard(); };
      const lo = $("#wz-live-ok");
      if (lo) lo.onchange = () => { wz.liveOk = lo.checked; };
      const tb = $("#wz-test");
      if (tb) tb.onclick = async () => {
        const key = $("#wz-key").value.trim();
        const secret = $("#wz-secret").value.trim();
        if (!key || !secret) { toast("API Key ve Secret Key girin", "err"); return; }
        tb.disabled = true;
        $("#wz-keys-result").innerHTML = `<p class="tiny muted">Binance'e bağlanılıyor…</p>`;
        try {
          await api("/api/keys", { method: "PUT", body: { mode: "live", api_key: key, api_secret: secret } });
          const r = await api("/api/keys/test", { method: "POST", body: { mode: "live" } });
          wz.keysOk = !!r.ok;
          const warn = (r.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
          $("#wz-keys-result").innerHTML = `<div class="callout ${r.ok ? (warn ? "warn" : "ok") : "danger"}"><strong>${esc(r.message)}</strong>${warn ? `<ul>${warn}</ul>` : ""}${r.ok ? "" : `<p><button class="btn sm" id="wz-diag">Bağlantı sorununu teşhis et</button></p><div id="wz-diag-out"></div>`}</div>`;
          const wd = $("#wz-diag");
          if (wd) wd.onclick = () => runDiagnose("#wz-diag-out", wd);
        } catch (e) {
          $("#wz-keys-result").innerHTML = `<div class="callout danger">${esc(e.message)}</div>`;
        } finally { tb.disabled = false; }
      };
      $("#wz-next").onclick = () => {
        if (wz.mode === "live" && !wz.keysOk) return toast("Önce API anahtarlarını test edip kaydedin", "err");
        if (wz.mode === "live" && !wz.liveOk) return toast("Devam etmek için riskleri onaylayın", "err");
        wz.step = 2; renderWizard(); return undefined;
      };
    } else if (wz.step === 2) {
      c.innerHTML = `${wzSteps()}<h3 id="wz-title">Risk seviyesi</h3><p class="muted">İşlem başına, stop olursa bakiyenizin ne kadarını riske atmak istersiniz?</p>
        <div class="wz-choice">
          <button data-p="careful" class="${wz.preset === "careful" ? "active" : ""}"><strong>Temkinli</strong><span>%0,5 risk · 3x · en fazla 2 pozisyon</span></button>
          <button data-p="balanced" class="${wz.preset === "balanced" ? "active" : ""}"><strong>Dengeli (önerilen)</strong><span>%1 risk · 5x · en fazla 3 pozisyon</span></button>
          <button data-p="aggressive" class="${wz.preset === "aggressive" ? "active" : ""}"><strong>Atak</strong><span>%2 risk · 5x · en fazla 4 pozisyon — düşüşler daha derin olur</span></button>
        </div>
        <div class="actions end"><button class="btn ghost" id="wz-back">Geri</button><button class="btn primary" id="wz-next">Devam</button></div>`;
      $$(".wz-choice button", c).forEach((b) => (b.onclick = () => { wz.preset = b.dataset.p; renderWizard(); }));
      $("#wz-back").onclick = () => { wz.step = 1; renderWizard(); };
      $("#wz-next").onclick = () => { wz.step = 3; renderWizard(); };
    } else {
      c.innerHTML = `${wzSteps()}<h3 id="wz-title">Hazırsınız 🎉</h3>
        <p class="muted">${wz.mode === "live" ? "Bot gerçek Binance hesabınıza bağlanacak." : "Bot sanal hesapla çalışacak; para riske girmez."} Otomatik işlem açıkken bot her ${esc(tfLabel(state.settings.entry_timeframe))} mum kapanışında piyasayı tarar ve uygun fırsatta kendi işlem açar. Siz çalışırken de program saatin yanındaki simgede çalışmaya devam eder.</p>
        <div class="actions end"><button class="btn ghost" id="wz-watch">Şimdilik sadece izle</button><button class="btn primary" id="wz-start">Otomatik işlemi başlat</button></div>`;
      const finish = async (start, btn) => {
        btn.disabled = true;
        try {
          const body = { ...PRESETS[wz.preset], wizard_done: true };
          if (wz.mode !== state.settings.mode) {
            body.mode = wz.mode;
            if (wz.mode === "live") body.confirm = "GERÇEK PARA";
          }
          await api("/api/settings", { method: "PUT", body });
          if (start) await api("/api/bot/auto", { method: "POST", body: { on: true } });
          $("#wizard").classList.add("hidden");
          toast(start ? "Otomatik işlem başladı" : "Kurulum tamamlandı", "ok");
          await refresh();
        } catch (e) {
          toast(e.message, "err");
          btn.disabled = false;
        }
      };
      $("#wz-start").onclick = (ev) => finish(true, ev.target);
      $("#wz-watch").onclick = (ev) => finish(false, ev.target);
    }
  }

  /* ------------------------------------------------------------- başlat */
  buildTabbar();
  boot();
})();
