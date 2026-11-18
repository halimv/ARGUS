"use strict";

// The complete research application owns its tools and conversations. This
// controller only connects its selected project to the pipeline request form.
window.argusResearch = (() => {
  const byId = (id) => document.getElementById(id);
  const frame = byId("researchFrame");
  const selector = byId("lanmProject");
  const status = byId("researchStatus");
  const allowedViews = new Set(["chat", "build", "candidates", "database", "tools"]);
  const state = { ready: false, busy: false, busyReason: "turn", projectId: null, projectName: "", view: "chat", conversationView: "chat", pending: null, newPipeline: false, expanded: false };
  let refreshVersion = 0;
  let loadingTimer;

  function showStatus(message) {
    status.textContent = message || (state.projectName
      ? `${state.projectName}${state.busy ? (state.busyReason === "dialog" ? " · Dialog open" : " · Working…") : ""}`
      : "Choose a research project above.");
    status.title = status.textContent;
  }

  async function refreshProjects() {
    const version = ++refreshVersion;
    const response = await fetch("/api/lanm-projects");
    if (!response.ok) throw new Error("Could not refresh research projects.");
    const data = await response.json();
    if (version !== refreshVersion) return;
    const selected = state.newPipeline ? "" : state.projectId || selector.value;
    selector.replaceChildren(new Option("New project for this pipeline run", ""));
    for (const project of data.projects) {
      selector.add(new Option(`${project.name} · ${project.target}/${project.competitor}`, project.id));
    }
    selector.value = selected;
  }

  function reveal() {
    byId("researchWorkspace").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }

  function sendLayout() {
    if (state.ready) frame.contentWindow.postMessage({type: "argus:research-layout", expanded: state.expanded}, location.origin);
  }

  function setExpanded(expanded) {
    state.expanded = expanded;
    const workspace = byId("researchWorkspace");
    workspace.classList.toggle("expanded", expanded);
    document.body.classList.toggle("research-expanded", expanded);
    byId("researchBackdrop").classList.toggle("hidden", !expanded);
    byId("expandResearch").setAttribute("aria-expanded", String(expanded));
    byId("expandResearch").textContent = expanded ? "Collapse ↙" : "Expand ↗";
    if (expanded) { workspace.setAttribute("role", "dialog"); workspace.setAttribute("aria-modal", "true"); }
    else { workspace.removeAttribute("role"); workspace.removeAttribute("aria-modal"); }
    for (const element of [byId("form"), document.querySelector(".right"), document.querySelector(".top")]) element.inert = expanded;
    sendLayout();
    if (!expanded) sendView(state.conversationView);
    byId("expandResearch").focus();
    if (!expanded) reveal();
  }

  function selectView(view) {
    if (!allowedViews.has(view)) return;
    if (view !== "chat" && view !== "build") setExpanded(true);
    sendView(view);
  }

  function sendView(view) {
    if (state.pending) state.pending.view = view;
    if (!state.ready) { state.pending = { projectId: state.pending?.projectId, view }; return; }
    frame.contentWindow.postMessage({type: "argus:research-select", view}, location.origin);
  }

  function sendSelection(projectId, view) {
    state.pending = null;
    if (!state.ready) { state.pending = { projectId, view }; return; }
    if (state.busy && projectId && projectId !== state.projectId) {
      state.pending = { projectId, view };
      selector.value = state.projectId || "";
      showStatus(state.busyReason === "dialog" ? "Close the research dialog to switch projects." : "The research turn is still running. Its project will change after the turn finishes.");
      return;
    }
    const message = { type: "argus:research-select" };
    if (projectId) message.projectId = projectId;
    if (allowedViews.has(view)) message.view = view;
    frame.contentWindow.postMessage(message, location.origin);
  }

  function selectProject(projectId, view) {
    state.newPipeline = false;
    sendSelection(projectId, view);
  }

  async function connect() {
    clearTimeout(loadingTimer);
    state.ready = false;
    byId("researchUnavailable").classList.add("hidden");
    frame.classList.remove("hidden");
    showStatus("Connecting to Oracle…");
    try {
      const response = await fetch("/api/lanm-workspace");
      if (!response.ok) throw new Error("The research service is unavailable. Check the ARGUS server output.");
      const info = await response.json();
      if (!info.available) throw new Error("The research service is not running.");
      // The iframe always stays on this ARGUS origin, never a returned remote URL.
      frame.src = "/research/?embed=argus&compact=1";
      loadingTimer = setTimeout(() => {
        if (!state.ready) {
          byId("researchUnavailable").classList.remove("hidden");
          showStatus("The research view is taking longer to load. Try again or open it separately.");
        }
      }, 15000);
      await refreshProjects();
    } catch (error) {
      frame.classList.add("hidden");
      byId("researchUnavailable").classList.remove("hidden");
      showStatus(error.message);
    }
  }

  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
    const message = event.data;
    if (message?.type === "lanm:research-collapse") { setExpanded(false); return; }
    if (!message || message.type !== "lanm:research-state" || message.ready !== true
        || typeof message.busy !== "boolean" || !allowedViews.has(message.view)
        || (message.projectId !== null && typeof message.projectId !== "string")) return;
    const previousProject = state.projectId;
    const firstReady = !state.ready;
    state.ready = true;
    state.busy = message.busy;
    state.busyReason = message.busyReason === "dialog" ? "dialog" : "turn";
    state.projectId = message.projectId;
    state.projectName = typeof message.projectName === "string" ? message.projectName : "";
    state.view = message.view;
    if (["chat", "build"].includes(state.view)) state.conversationView = state.view;
    for (const [id, view] of [["researchChat", "chat"], ["researchBuild", "build"]]) {
      byId(id).classList.toggle("active", state.view === view);
      byId(id).setAttribute("aria-pressed", String(state.view === view));
    }
    byId("researchBrowse").value = ["candidates", "database", "tools"].includes(state.view) ? state.view : "";
    if (firstReady) sendLayout();
    clearTimeout(loadingTimer);
    byId("researchUnavailable").classList.add("hidden");
    selector.disabled = state.busy;
    selector.title = state.busy ? (state.busyReason === "dialog" ? "Close the research dialog before switching projects." : "Finish or stop the research turn before switching projects.") : "";
    if (previousProject !== null && previousProject !== state.projectId) state.newPipeline = false;
    if (!state.newPipeline) selector.value = state.projectId || "";
    showStatus(state.newPipeline ? "The pipeline will create a new project. Oracle keeps its current project." : undefined);
    refreshProjects().catch((error) => showStatus(error.message));
    if (state.pending && !state.busy) {
      const pending = state.pending;
      state.pending = null;
      sendSelection(pending.projectId, pending.view);
    }
  });

  selector.addEventListener("change", () => {
    state.pending = null;
    state.newPipeline = !selector.value;
    if (state.newPipeline) showStatus("The pipeline will create a new project. Oracle keeps its current project.");
    else selectProject(selector.value);
  });
  byId("refreshProjects").addEventListener("click", () => refreshProjects().catch((error) => showStatus(error.message)));
  byId("inspectResearchProject").addEventListener("click", () => {
    if (selector.value) sendSelection(selector.value);
    reveal();
  });
  byId("researchWorkspaceLink").addEventListener("click", (event) => { event.preventDefault(); reveal(); });
  byId("researchChat").addEventListener("click", () => selectView("chat"));
  byId("researchBuild").addEventListener("click", () => selectView("build"));
  byId("researchBrowse").addEventListener("change", () => selectView(byId("researchBrowse").value));
  byId("expandResearch").addEventListener("click", () => setExpanded(!state.expanded));
  byId("researchBackdrop").addEventListener("click", () => setExpanded(false));
  window.addEventListener("keydown", (event) => { if (event.key === "Escape" && state.expanded) { event.preventDefault(); setExpanded(false); } });
  byId("retryResearch").addEventListener("click", connect);
  connect();
  return { selectProject, refreshProjects };
})();
