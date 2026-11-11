async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return response.json();
}

function element(tag, text = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

async function loadDiagnostics() {
  const table = document.getElementById("diagnostics");
  const notice = document.getElementById("notice");
  notice.textContent = "";
  table.replaceChildren();
  const url = new URL("/api/diagnostics", window.location.origin);
  const status = document.getElementById("processing-status").value;
  if (status) url.searchParams.set("status", status);
  if (document.getElementById("parser-errors").checked) {
    url.searchParams.set("parser_errors", "1");
  }
  if (document.getElementById("needs-reference").checked) {
    url.searchParams.set("needs_reference", "1");
  }
  url.searchParams.set("limit", "100");
  try {
    const rows = await getJson(url);
    if (!rows.length) {
      const row = element("tr");
      const cell = element("td", "Geen projecten voor deze filters.");
      cell.colSpan = 8;
      cell.className = "muted";
      row.append(cell);
      table.append(row);
      return;
    }
    for (const project of rows) {
      const row = element("tr");
      const pathCell = element("td");
      if (project.project_id === null || project.project_id === undefined) {
        pathCell.textContent = project.path;
      } else {
        const open = element("button", project.path);
        open.type = "button";
        open.className = "path-button";
        open.addEventListener("click", () => loadProject(project.project_id));
        pathCell.append(open);
      }
      row.append(pathCell);
      row.append(element("td", project.processing_status));
      row.append(element("td", project.format_version === undefined ? "—" : String(project.format_version)));
      row.append(element("td", `${project.named_versions ?? 0} / ${project.binary_slots ?? 0}`));
      row.append(element("td", String(project.unlabeled_binary_slots ?? 0)));
      row.append(element("td", String(project.map_count ?? 0)));
      row.append(element("td", String(project.error_count ?? 0)));
      row.append(element("td", project.reference_status || "not_validated"));
      table.append(row);
    }
  } catch (error) {
    notice.textContent = `Diagnoses konden niet worden geladen: ${error.message}`;
  }
}

async function loadExportQueue() {
  const table = document.getElementById("export-queue");
  const notice = document.getElementById("export-queue-notice");
  table.replaceChildren();
  notice.textContent = "";
  try {
    const tasks = await getJson("/api/export-queue?limit=500");
    if (!tasks.length) {
      const row = element("tr");
      const cell = element("td", "Er zijn nog geen exporttaken gegenereerd.");
      cell.colSpan = 8;
      cell.className = "muted";
      row.append(cell);
      table.append(row);
      return;
    }
    for (const task of tasks) {
      const row = element("tr");
      row.append(element("td", String(task.project_id)));
      row.append(element("td", task.priority));
      row.append(element("td", task.export_types.join(", ")));
      row.append(element("td", task.version_names.join(", ") || "—"));
      row.append(element("td", task.reason));
      row.append(element("td", task.status));
      row.append(element("td", String(task.attempts)));
      const historyCell = element("td");
      const historyButton = element("button", "Bekijken");
      historyButton.type = "button";
      historyButton.className = "secondary";
      historyButton.addEventListener("click", async () => {
        const existing = row.nextElementSibling;
        if (existing && existing.dataset.historyFor === String(task.id)) {
          existing.remove();
          return;
        }
        try {
          const history = await getJson(
            `/api/export-task-history/${encodeURIComponent(task.id)}`
          );
          const detailsRow = element("tr");
          detailsRow.dataset.historyFor = String(task.id);
          const detailsCell = element("td");
          detailsCell.colSpan = 8;
          const details = element("pre", JSON.stringify(history, null, 2));
          details.className = "detail";
          detailsCell.append(details);
          detailsRow.append(detailsCell);
          row.after(detailsRow);
        } catch (error) {
          notice.textContent = `Taakhistorie kon niet worden geladen: ${error.message}`;
        }
      });
      historyCell.append(historyButton);
      row.append(historyCell);
      table.append(row);
    }
  } catch (error) {
    notice.textContent = `Exportwachtrij kon niet worden geladen: ${error.message}`;
  }
}

let activeProjectId = null;

async function loadProject(projectId) {
  activeProjectId = projectId;
  const section = document.getElementById("detail-section");
  const title = document.getElementById("detail-title");
  const detail = document.getElementById("detail");
  section.hidden = false;
  title.textContent = `Project ${projectId}`;
  detail.textContent = "Diagnose laden…";
  section.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const project = await getJson(`/api/projects/${encodeURIComponent(projectId)}`);
    title.textContent = project.path;
    const reference = await getJson(
      `/api/reference-report/${encodeURIComponent(projectId)}`
    );
    const matchStatus = document.getElementById("version-match-status").value;
    const matches = await getJson(
      `/api/version-matches?project_id=${encodeURIComponent(projectId)}&status=${encodeURIComponent(matchStatus)}`
    );
    const packageReport = await getJson(
      `/api/reference-package/${encodeURIComponent(projectId)}`
    );
    const projectMatches = await getJson(
      `/api/project-matches/${encodeURIComponent(projectId)}`
    );
    detail.textContent = JSON.stringify(
      {
        import_diagnostics: project.diagnostics,
        reference_validation: reference,
        version_match_candidates: matches,
        reference_package: packageReport,
        similar_projects: projectMatches,
      },
      null,
      2
    );
  } catch (error) {
    detail.textContent = `Diagnose kon niet worden geladen: ${error.message}`;
  }
}

document.getElementById("refresh").addEventListener("click", loadDiagnostics);
document.getElementById("refresh-export-queue").addEventListener("click", loadExportQueue);
document.getElementById("processing-status").addEventListener("change", loadDiagnostics);
document.getElementById("parser-errors").addEventListener("change", loadDiagnostics);
document.getElementById("needs-reference").addEventListener("change", loadDiagnostics);
document.getElementById("close-detail").addEventListener("click", () => {
  document.getElementById("detail-section").hidden = true;
});
document.getElementById("version-match-status").addEventListener("change", () => {
  if (activeProjectId !== null) loadProject(activeProjectId);
});

loadDiagnostics();
loadExportQueue();
