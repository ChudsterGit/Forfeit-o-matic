(() => {
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let pid = store.get("pid"), name = store.get("name"), code = store.get("code");
  let state = null, poll = null, raf = null;
  let run = null; // { t0 (performance.now), duration } while our counter is running
  let stopping = false;

  async function api(path, body) {
    const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(data.error || "Request failed"), { status: r.status });
    return data;
  }

  function show(view) {
    for (const v of ["view-register", "view-lobby", "view-game"]) $(v).classList.toggle("hidden", v !== view);
    $("whoami").textContent = name ? "Playing as " + name : "";
  }

  // ---------- registration / lobby
  async function boot() {
    if (pid) {
      try { const me = await api("/api/me", { pid }); name = me.name; store.set("name", name); }
      catch (e) { if (e.status === 404) { pid = null; store.del("pid"); } }
    }
    if (!pid) return show("view-register");
    if (code) return enterGame(code);
    show("view-lobby");
  }

  $("register-btn").onclick = async () => {
    $("register-err").textContent = "";
    try {
      const r = await api("/api/register", { name: $("name-input").value, pid });
      pid = r.pid; name = r.name; store.set("pid", pid); store.set("name", name);
      show("view-lobby");
    } catch (e) { $("register-err").textContent = e.message; }
  };
  $("name-input").onkeydown = (e) => { if (e.key === "Enter") $("register-btn").click(); };

  $("create-btn").onclick = async () => {
    try { enterGame((await api("/api/create", { pid })).code); }
    catch (e) { $("lobby-err").textContent = e.message; }
  };
  $("join-btn").onclick = async () => {
    try { enterGame((await api("/api/join", { pid, code: $("code-input").value })).code); }
    catch (e) { $("lobby-err").textContent = e.message; }
  };
  $("code-input").onkeydown = (e) => { if (e.key === "Enter") $("join-btn").click(); };

  function leaveGame() {
    code = null; store.del("code"); state = null;
    clearInterval(poll); cancelAnimationFrame(raf); run = null;
    show("view-lobby");
  }

  async function enterGame(c) {
    code = c; store.set("code", c);
    show("view-game");
    showTab("play");
    $("game-code").textContent = c;
    clearInterval(poll);
    try { await refresh(); } catch (e) { return leaveGame(); }
    poll = setInterval(() => refresh().catch(() => {}), 1000);
    loadHiscores();
  }

  // ---------- state sync
  async function refresh() {
    let s;
    try { s = await api(`/api/game/${code}?pid=${pid}`); }
    catch (e) { if (e.status === 403 || e.status === 404) leaveGame(); throw e; }
    const roundChanged = state && state.round !== s.round;
    const justFinalized = state && !state.finalized && s.finalized;
    state = s;
    if (roundChanged) { run = null; cancelAnimationFrame(raf); stopping = false; }
    // Resume a counter that was running (e.g. page reload)
    if (s.me && s.me.status === "running" && !run) startAnimation(s.me.duration, s.me.elapsed);
    render();
    if (justFinalized || roundChanged) loadHiscores();
  }

  function render() {
    const s = state, me = s.me;
    $("game-code").textContent = s.code;
    $("round-no").textContent = s.round;
    const iAmLead = s.lead_id === pid;
    $("lead-badge").classList.toggle("hidden", !iAmLead);
    $("lead-controls").classList.toggle("hidden", !iAmLead);
    $("progress").textContent = `${s.done}/${s.total} done`;

    // play button + counter
    const btn = $("play-btn"), msg = $("play-msg");
    if (me.status === "waiting") {
      btn.textContent = s.finalized ? "ROUND OVER" : "START"; btn.className = "bigbtn start";
      btn.disabled = s.finalized;
      msg.textContent = s.finalized ? "Wait for the lead to reset." : "One go only. Stop it on 1000!";
      if (!run) setCounter(1);
    } else if (me.status === "running") {
      btn.textContent = "STOP"; btn.className = "bigbtn stop"; btn.disabled = false; msg.textContent = "";
    } else {
      btn.textContent = "DONE"; btn.className = "bigbtn start"; btn.disabled = true;
      setCounter(me.score);
      const d = Math.abs(me.score - 1000);
      msg.textContent = d === 0 ? "BANG ON 1000!" : `${d} ${me.score > 1000 ? "over" : "under"} 1000`;
    }

    // banner
    const b = $("banner");
    b.className = "banner hidden"; b.innerHTML = "";
    if (s.next_code && !iAmLead) {
      b.className = "banner";
      b.innerHTML = `The lead started a new game: <b class="code">${esc(s.next_code)}</b><br><button id="join-next">Join it</button>`;
      $("join-next").onclick = async () => {
        try { await api("/api/join", { pid, code: s.next_code }); enterGame(s.next_code); } catch (e) { alert(e.message); }
      };
    } else if (s.finalized && s.winner) {
      const nm = (ids) => s.players.filter((p) => ids.includes(p.id)).map((p) => esc(p.name)).join(", ");
      const iWon = s.winner.includes(pid), iLost = s.loser.includes(pid);
      b.className = "banner " + (iWon ? "win" : iLost ? "lose" : "");
      b.innerHTML = `🏆 Winner: ${nm(s.winner)}<br>💀 Loser: ${nm(s.loser)}`;
    } else if (s.finalized) {
      b.className = "banner"; b.textContent = "Round finished.";
    }

    // scoreboard
    const order = { done: 0, running: 1, waiting: 2 };
    const ps = [...s.players].sort((a, b) =>
      order[a.status] - order[b.status] || (a.diff ?? 0) - (b.diff ?? 0));
    $("score-list").innerHTML = ps.map((p) => {
      const tags = [];
      if (p.is_lead) tags.push('<span class="tag lead">👑 lead</span>');
      if (s.winner && s.winner.includes(p.id)) tags.push(`<span class="tag win">${s.finalized ? "WINNER" : "closest"}</span>`);
      if (s.loser && s.loser.includes(p.id)) tags.push(`<span class="tag lose">${s.finalized ? "LOSER" : "furthest"}</span>`);
      if (p.status === "running") tags.push('<span class="tag run">playing…</span>');
      const right = p.status === "done"
        ? `<span class="score">${p.score}</span><span class="diff">${p.diff === 0 ? "perfect" : (p.score > 1000 ? "+" : "-") + p.diff}</span>`
        : `<span class="muted">${p.status === "waiting" ? "not played" : ""}</span>`;
      return `<li class="${p.id === pid ? "me" : ""}"><span class="name">${esc(p.name)}</span>${tags.join(" ")}${right}</li>`;
    }).join("");
  }

  async function loadHiscores() {
    try {
      const rows = await api("/api/leaderboard");
      $("hi-table").querySelector("tbody").innerHTML = rows.length
        ? rows.map((r, i) => `<tr class="${r.id === pid ? "me" : ""}"><td>${i + 1}</td><td>${esc(r.name)}</td><td>${r.wins}</td><td>${r.losses}</td><td>${r.rounds}</td><td>${r.best}</td><td>${r.avg}</td></tr>`).join("")
        : '<tr><td colspan="7" class="muted">No finished rounds yet.</td></tr>';
    } catch {}
  }

  // ---------- counter
  function setCounter(v) {
    const el = $("counter");
    el.textContent = v;
    el.classList.toggle("over", v > 1000);
  }
  const valueAt = (t, d) => Math.max(1, Math.min(2000, Math.round(1 + 999 * t / d)));

  function startAnimation(duration, elapsed) {
    run = { t0: performance.now() - elapsed * 1000, duration };
    const tick = () => {
      if (!run) return;
      const v = valueAt((performance.now() - run.t0) / 1000, run.duration);
      setCounter(v);
      if (v >= 2000) return finish(2000);
      raf = requestAnimationFrame(tick);
    };
    tick();
  }

  async function finish(value) {
    if (stopping || !run) return;
    stopping = true;
    cancelAnimationFrame(raf);
    run = null;
    setCounter(value);
    try { state = await api("/api/stop", { pid, code, value }); render(); loadHiscores(); }
    catch (e) { $("play-msg").textContent = e.message; }
    stopping = false;
  }

  // pointerdown rather than click: reacts the instant the finger lands
  $("play-btn").addEventListener("pointerdown", async (e) => {
    e.preventDefault();
    if (!state || !state.me) return;
    if (state.me.status === "running" && run) {
      const v = valueAt((performance.now() - run.t0) / 1000, run.duration);
      return finish(v);
    }
    if (state.me.status === "waiting" && !state.finalized) {
      try {
        const r = await api("/api/start", { pid, code });
        state.me.status = "running";
        startAnimation(r.duration, 0);
        render();
      } catch (e2) { $("play-msg").textContent = e2.message; refresh().catch(() => {}); }
    }
  });

  // ---------- lead controls
  async function reset(mode, confirmMsg) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    try {
      const r = await api("/api/reset", { pid, code, mode });
      if (mode === "new") return enterGame(r.code);
      await refresh();
    } catch (e) { alert(e.message); }
  }
  $("finish-btn").onclick = () => reset("finish", "End the round now? Anyone who hasn't played won't count.");
  $("reset-scores-btn").onclick = () => reset("scores", "Reset all scores and play another round with the same players?");
  $("new-game-btn").onclick = () => reset("new", "Start a brand new game? Everyone will need to join with the new code.");

  // ---------- mobile tabs
  function showTab(t) {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
    for (const p of ["play", "scores", "hiscores"]) $("pane-" + p).classList.toggle("show", p === t);
    if (t === "hiscores") loadHiscores();
  }
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

  boot();
})();
