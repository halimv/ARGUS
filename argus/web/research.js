"use strict";

// The complete research application owns its tools and conversations. This
// controller only connects its selected project to the pipeline request form.
window.argusResearch = (() => {
  const byId = (id) => document.getElementById(id);
  const frame = byId("researchFrame");
  const selector = byId("lanmProject");
  const status = byId("researchStatus");
  const allowedViews = new Set(["chat", "build", "candidates", "database", "tools"]);
  const state = { ready: false, busy: false, busyReason: "turn", projectId: null, projectName: "", view: "chat", pending: null, newPipeline: false };
  let refreshVersion = 0;
  let loadingTimer;

  function showStatus(message) {
    status.textContent = message || (state.projectName
      ? `${state.projectName} · ${state.busy ? (state.busyReason === "dialog" ? "Research dialog open" : "Research agent working") : "Shared with the pipeline project selector"}`
      : "Choose or create a research project below.");
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
    showStatus("Connecting to the research workspace…");
    try {
      const response = await fetch("/api/lanm-workspace");
      if (!response.ok) throw new Error("The research service is unavailable. Check the ARGUS server output.");
      const info = await response.json();
      if (!info.available) throw new Error("The research service is not running.");
      // The iframe always stays on this ARGUS origin, never a returned remote URL.
      frame.src = "/research/?embed=argus";
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
    if (!message || message.type !== "lanm:research-state" || message.ready !== true
        || typeof message.busy !== "boolean" || !allowedViews.has(message.view)
        || (message.projectId !== null && typeof message.projectId !== "string")) return;
    const previousProject = state.projectId;
    state.ready = true;
    state.busy = message.busy;
    state.busyReason = message.busyReason === "dialog" ? "dialog" : "turn";
    state.projectId = message.projectId;
    state.projectName = typeof message.projectName === "string" ? message.projectName : "";
    state.view = message.view;
    clearTimeout(loadingTimer);
    byId("researchUnavailable").classList.add("hidden");
    selector.disabled = state.busy;
    selector.title = state.busy ? (state.busyReason === "dialog" ? "Close the research dialog before switching projects." : "Finish or stop the research turn before switching projects.") : "";
    if (previousProject !== null && previousProject !== state.projectId) state.newPipeline = false;
    if (!state.newPipeline) selector.value = state.projectId || "";
    showStatus(state.newPipeline ? "The pipeline will create a new project. Research below remains in its current project." : undefined);
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
    if (state.newPipeline) showStatus("The pipeline will create a new project. Research below remains in its current project.");
    else selectProject(selector.value);
  });
  byId("refreshProjects").addEventListener("click", () => refreshProjects().catch((error) => showStatus(error.message)));
  byId("inspectResearchProject").addEventListener("click", () => {
    if (selector.value) sendSelection(selector.value);
    reveal();
  });
  byId("researchWorkspaceLink").addEventListener("click", (event) => { event.preventDefault(); reveal(); });
  byId("expandResearch").addEventListener("click", () => {
    const expanded = byId("researchWorkspace").classList.toggle("expanded");
    byId("expandResearch").setAttribute("aria-expanded", String(expanded));
    byId("expandResearch").textContent = expanded ? "Restore size" : "Expand research";
    reveal();
  });
  byId("retryResearch").addEventListener("click", connect);
  connect();
  return { selectProject, refreshProjects };
})();
