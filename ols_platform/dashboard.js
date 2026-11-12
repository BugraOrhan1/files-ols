const statusLabels = {
  source_files: "Bronbestanden",
  indexed_source_bytes: "Geïndexeerde bronbytes",
  projects: "Projecten",
  complete_projects: "Volledig gelezen",
  versions_found: "Gevonden versies",
  binary_slots_enumerated: "Binaire slots",
  binary_slots_available: "Slots met data (ongevalideerd)",
  unlabeled_binary_slots: "Ongelabelde slots",
  map_definitions: "Kaartdefinities",
  axes_found: "Assen",
  cal_blocks_found: "CAL-blokken",
  projects_needing_reference_validation: "Referentiecontrole nodig",
  duplicate_file_paths: "Dubbele inhoud",
  latest_failed_files: "Laatste import mislukt",
  latest_blocked_files: "Geblokkeerd",
  import_errors: "Importfouten (historie)",
  import_warnings: "Waarschuwingen (historie)"
};

let currentSearchOffset = 0;
let currentSearchParams = new URLSearchParams();

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return response.json();
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

async function loadStatus() {
  const target = document.getElementById("status");
  target.replaceChildren(node("p", "muted", "Status laden…"));
  try {
    const status = await getJson("/api/status");
    const cards = Object.entries(status).map(([key, value]) => {
      const card = node("div", "card");
      card.append(node("strong", "", Number(value).toLocaleString("nl-NL")));
      card.append(node("span", "", statusLabels[key] || key));
      return card;
    });
    target.replaceChildren(...cards);
  } catch (error) {
    target.replaceChildren(node("p", "notice", `Status kon niet worden geladen: ${error.message}`));
  }
}

async function loadInventory() {
  const target = document.getElementById("inventory-overview");
  target.replaceChildren(node("p", "muted", "Inventaris laden…"));
  try {
    const data = await getJson("/api/inventory");
    const run = data.latest_scan;
    if (!run) {
      target.replaceChildren(node("p", "muted", "Nog geen inventarisscan uitgevoerd."));
      return;
    }
    const report = run.report;
    const values = [
      ["Gevonden bestanden", report.scan.files],
      ["OLS-projecten", report.scan.ols_projects],
      ["Totale omvang (bytes)", report.scan.total_bytes],
      ["Hashbevestigde indexbestanden", report.indexed_comparison.full_hash_match],
      ["Niet-geïndexeerde OLS", report.indexed_comparison.not_indexed_ols_files],
      ["Exacte duplicaten", report.duplicates.exact_duplicate_files_beyond_first],
      ["Lees-/hashfouten", report.scan.unreadable_entries + report.indexed_comparison.hash_errors]
    ];
    const cards = values.map(([label, value]) => {
      const card = node("div", "card");
      card.append(node("strong", "", Number(value).toLocaleString("nl-NL")));
      card.append(node("span", "", label));
      return card;
    });
    const scannedRoots = report.roots.map((root) => root.path).join(" | ");
    const summary = node(
      "p",
      "muted",
      `Scan ${run.id} — ${run.created_at}. Mappen: ${scannedRoots}`
    );
    target.replaceChildren(summary, ...cards);
  } catch (error) {
    target.replaceChildren(
      node("p", "notice", `Inventaris kon niet worden geladen: ${error.message}`)
    );
  }
}

async function loadMultiSourceInventory() {
  const overview = document.getElementById("multi-source-overview");
  const sourcesTarget = document.getElementById("inventory-sources");
  const extensionsTarget = document.getElementById("inventory-extensions");
  const duplicatesTarget = document.getElementById("inventory-duplicate-candidates");
  const benchmarkTarget = document.getElementById("hash-benchmark");
  try {
    const run = await getJson("/api/multi-source-inventory");
    if (!run) {
      overview.replaceChildren(node(
        "p", "muted", "Nog geen Phase 7 multi-source inventarisatie uitgevoerd."
      ));
      sourcesTarget.replaceChildren();
      extensionsTarget.replaceChildren();
      duplicatesTarget.replaceChildren();
      benchmarkTarget.replaceChildren();
      return;
    }
    const report = run.report;
    const totals = report.totals;
    const deduplication = report.database_deduplication_summary || {};
    const cards = [
      ["Scanstatus", report.status],
      ["Bestandspaden", totals.found_paths],
      ["OLS-paden", totals.ols_paths],
      ["Fysieke bytes", totals.physical_bytes],
      ["OLS fysieke bytes", totals.ols_physical_bytes],
      ["Bekende locaties", totals.known_locations],
      ["Nieuwe locaties", totals.new_locations],
      ["Gewijzigde locaties", totals.changed_locations],
      ["OLS-hash nog nodig", totals.unhashed_ols_locations],
      ["Niet geïndexeerde OLS", totals.unindexed_ols_locations],
      ["Hash-bevestigde unieke inhoud (bekend)", deduplication.unique_ols_contents || 0],
      ["Exacte dubbele locaties (gehashte inhoud)", deduplication.exact_duplicate_locations || 0],
      ["Niet bereikbare/onbekende bronnen", totals.inaccessible_sources],
      ["Onvolledige directories", totals.incomplete_directories],
      ["Scanerrors", totals.scan_errors],
      ["Metadata-duplicaatkandidaten", totals.possible_duplicate_groups_by_filename_and_size]
    ].map(([label, value]) => {
      const card = node("div", "card");
      card.append(node("strong", "", typeof value === "number"
        ? value.toLocaleString("nl-NL") : String(value)));
      card.append(node("span", "", label));
      return card;
    });
    overview.replaceChildren(
      node("p", "muted", `Inventarisrun ${run.id} · ${run.created_at} · ` +
        `${report.all_configured_sources_complete ? "alle geconfigureerde bronnen compleet" :
          "gedeeltelijk; ontbrekende bronnen zijn niet als leeg geteld"}`),
      node("p", "notice", "Het aantal hash-bevestigde unieke inhoudsbestanden is voorlopig: " +
        "de volledige dataset is niet gehasht. Metadata-kandidaten blijven onbevestigd."),
      ...cards
    );
    sourcesTarget.replaceChildren(...report.sources.map((source) => {
      const card = node("div", "card");
      card.append(node("strong", "", source.name));
      card.append(node("span", "", `${source.path || "Pad niet geconfigureerd"} · ${source.status}`));
      if (source.inventory) {
        const data = source.inventory;
        card.append(node(
          "span",
          "muted",
          `${data.found_paths.toLocaleString("nl-NL")} paden · ` +
            `${data.ols_paths.toLocaleString("nl-NL")} OLS · ` +
            `${data.physical_bytes.toLocaleString("nl-NL")} fysieke bytes · ` +
            `${data.ols_physical_bytes.toLocaleString("nl-NL")} OLS-bytes`
        ));
        card.append(node(
          "span",
          "muted",
          `${data.new_locations} nieuw · ${data.known_locations} bekend · ` +
            `${data.changed_locations} gewijzigd · ${data.unindexed_ols_locations} OLS niet geïndexeerd · ` +
            `${data.directories_complete}/${data.directories_total} directories compleet · ` +
            `${data.errors} fouten`
        ));
      } else if (source.last_error) {
        card.append(node("span", "notice", source.last_error));
      } else if (source.previously_recorded_locations) {
        card.append(node(
          "span", "notice",
          `Niet opnieuw gescand; laatst bekende locaties: ${source.previously_recorded_locations}.`
        ));
      }
      return card;
    }));
    extensionsTarget.replaceChildren(...Object.entries(report.extension_totals).map(
      ([extension, data]) => {
        const card = node("div", "card");
        card.append(node("strong", "", `.${extension}`));
        card.append(node("span", "", `${data.files.toLocaleString("nl-NL")} bestanden · ` +
          `${data.bytes.toLocaleString("nl-NL")} bytes`));
        return card;
      }
    ));
    duplicatesTarget.replaceChildren(...report.possible_duplicate_groups.map((group) => {
      const card = node("div", "card");
      card.append(node("strong", "", `${group.filename} · ${group.size_bytes} bytes`));
      card.append(node("span", "notice",
        `${group.locations_count} vindplaatsen; kandidaat op naam/grootte, geen exact duplicaatbewijs.`));
      for (const location of group.locations.slice(0, 10)) {
        card.append(node(
          "span", "muted",
          `${location.source_root || `Source ${location.source_id}`} · ${location.path}`
        ));
      }
      return card;
    }));
    const benchmark = report.hash_benchmark;
    const benchmarkCards = [
      ["Hashstatus", benchmark.status],
      ["Gehasht", benchmark.files_hashed || 0],
      ["Werkelijk gelezen bytes", benchmark.bytes_read || 0],
      ["Gemiddeld MiB/s", benchmark.average_mib_per_second || 0],
      ["Geschatte seconden voor fysieke OLS", 
        benchmark.full_dataset_estimate?.estimated_seconds_at_sample_rate ?? "onbekend"]
    ].map(([label, value]) => {
      const card = node("div", "card");
      card.append(node("strong", "", typeof value === "number"
        ? value.toLocaleString("nl-NL") : String(value)));
      card.append(node("span", "", label));
      return card;
    });
    benchmarkTarget.replaceChildren(
      ...benchmarkCards,
      node("p", "muted", benchmark.full_dataset_estimate?.uncertainty ||
        benchmark.reason || benchmark.note || "")
    );
  } catch (error) {
    overview.replaceChildren(node(
      "p", "notice", `Multi-source inventaris kon niet worden geladen: ${error.message}`
    ));
  }
}

async function loadInventoryLocations() {
  const target = document.getElementById("inventory-locations");
  try {
    const locations = await getJson("/api/inventory/locations?limit=100");
    if (!locations.length) {
      target.replaceChildren();
      const row = node("tr");
      row.append(node("td", "muted", "Nog geen bestandslocaties geïnventariseerd."));
      row.firstChild.colSpan = 7;
      target.append(row);
      return;
    }
    target.replaceChildren(...locations.map((item) => {
      const row = node("tr");
      row.append(node("td", "", item.source_root || `Source ${item.source_id}`));
      row.append(node("td", "", item.path));
      row.append(node("td", "", item.extension || "(geen)"));
      row.append(node("td", "", Number(item.size_bytes).toLocaleString("nl-NL")));
      row.append(node("td", "", item.last_seen_state));
      row.append(node("td", "", item.is_indexed ? "Ja" : "Nee"));
      row.append(node("td", "", item.needs_hash ? "Ja" : "Nee"));
      return row;
    }));
  } catch (error) {
    target.replaceChildren();
    const row = node("tr");
    row.append(node("td", "notice", `Locaties niet beschikbaar: ${error.message}`));
    row.firstChild.colSpan = 7;
    target.append(row);
  }
}

async function loadSourceScanProgress() {
  const target = document.getElementById("source-scan-progress");
  try {
    const sources = await getJson("/api/source-scan-progress");
    target.replaceChildren(...sources.map((source) => {
      const card = node("div", "card");
      card.append(node("strong", "", source.name));
      card.append(node(
        "span",
        "",
        `${source.path || "Pad niet geconfigureerd"} · bron ${source.source_status || "unconfigured"} · ` +
          `scan ${source.scan_status || "not_started"}`
      ));
      if (source.progress) {
        const progress = source.progress;
        card.append(node(
          "span",
          "muted",
          `${Number(progress.found_paths || 0).toLocaleString("nl-NL")} paden · ` +
            `${Number(progress.ols_paths || 0).toLocaleString("nl-NL")} OLS · ` +
            `${Number(progress.directories_complete || 0).toLocaleString("nl-NL")}/` +
            `${Number(progress.directories_total || 0).toLocaleString("nl-NL")} directories compleet · ` +
            `${Number(progress.errors || 0).toLocaleString("nl-NL")} fouten`
        ));
      } else if (source.last_error) {
        card.append(node("span", "notice", source.last_error));
      }
      return card;
    }));
  } catch (error) {
    target.replaceChildren(node(
      "p", "notice", `Scanvoortgang kon niet worden geladen: ${error.message}`
    ));
  }
}

async function loadHashing() {
  const summaryTarget = document.getElementById("hashing-summary");
  const sourcesTarget = document.getElementById("hashing-sources");
  const duplicatesTarget = document.getElementById("hashing-duplicates");
  try {
    const report = await getJson("/api/hashing");
    const totals = report.totals;
    const states = report.queue_statuses;
    const worker = report.worker;
    const cards = [
      ["OLS-locaties", totals.ols_locations],
      ["Volledig gehasht", totals.hashed],
      ["Wachtend / onbevestigd", totals.pending],
      ["Totale voortgang %", worker.progress_percent],
      ["Continue worker", worker.status],
      ["Actieve readers", `${worker.active_workers} / ${worker.workers}`],
      ["Actueel gelezen bytes", worker.bytes_read],
      ["Gemiddelde sinds start MiB/s",
        worker.performance.average_mib_per_second],
      ["Sinds start hashes / uur", worker.performance.files_per_hour],
      ["Recente hashes / uur", report.recent_hash_performance.files_per_hour],
      ["Bytes in actieve bestanden", worker.active_bytes_read],
      ["Voltooide batches", worker.batches_completed],
      ["Workerfouten", worker.error_count],
      ["Retry-taken", states.RETRY_PENDING || 0],
      ["Hashing", states.HASHING || 0],
      ["Mislukt", (states.HASH_FAILED || 0) + (states.FILE_CHANGED || 0)],
      ["Bron tijdelijk onbereikbaar", states.SOURCE_UNAVAILABLE || 0],
      ["Unieke gehashte inhoud", report.deduplication.unique_ols_contents],
      ["Bevestigde dubbele locaties", report.deduplication.exact_duplicate_locations],
      ["OLS fysieke bytes", totals.physical_bytes],
      ["Unieke gehashte bytes", report.deduplication.unique_ols_bytes],
      ["Te herlezen bytes", totals.pending_physical_bytes],
      ["Recente snelheid MiB/s", report.recent_hash_performance.mib_per_second],
      ["Recente meetvenster seconden",
        report.recent_hash_performance.window_seconds],
      ["Aantal recente gehashte bestanden",
        report.recent_file_sizes.successful_hashes],
      ["Gemiddelde bestandsgrootte bytes",
        report.recent_file_sizes.mean_bytes ?? "onbekend"],
      ["Mediaan bestandsgrootte bytes",
        report.recent_file_sizes.median_bytes ?? "onbekend"],
      ["P90 bestandsgrootte bytes",
        report.recent_file_sizes.p90_bytes ?? "onbekend"],
      ["Kleinste / grootste bestand bytes",
        report.recent_file_sizes.min_bytes === null
          ? "onbekend"
          : `${report.recent_file_sizes.min_bytes} / ${report.recent_file_sizes.max_bytes}`],
      ["Leestijd (incl. SMB/cache) laatste runs seconden",
        report.recent_hash_timing_breakdown.read_call_seconds ?? "nog niet gemeten"],
      ["Databasewrites laatste runs seconden",
        report.recent_hash_timing_breakdown.database_write_seconds ?? "nog niet gemeten"],
      ["Resterende schatting seconden", totals.remaining_seconds_at_recent_rate ?? "onbekend"]
    ].map(([label, value]) => {
      const card = node("div", "card");
      card.append(node("strong", "", typeof value === "number"
        ? value.toLocaleString("nl-NL") : String(value)));
      card.append(node("span", "", label));
      return card;
    });
    summaryTarget.replaceChildren(
      node("p", "muted", `Queue: ${report.control.status}` +
        `${report.control.run_id ? ` · run ${report.control.run_id}` : ""} · ` +
        `${report.deduplication.partially_processed_projects} projecten gedeeltelijk geparsed`),
      node("p", "muted",
        `Worker sinds ${worker.started_at || "niet gestart"} · heartbeat ` +
        `${worker.heartbeat_epoch ? new Date(worker.heartbeat_epoch * 1000).toLocaleString("nl-NL") : "—"}`),
      node("p", "notice", "Tijdschattingen zijn indicatief; netwerkbelasting, bestandsgrootte en retries kunnen ze sterk wijzigen."),
      node("p", "muted",
        `Recente doorvoer gebruikt ${report.recent_hash_performance.runs} voltooide batches ` +
        `over ${Number(report.recent_hash_performance.window_seconds || 0).toLocaleString("nl-NL")} sec. ` +
        "Leestijd omvat SMB-wachttijd of cache; afzonderlijke reader-tijden kunnen overlappen."),
      node("p", "notice", "Unieke inhoud betreft alleen volledig gehashte OLS-paden. " +
        "Niet-gehashte locaties zijn niet als unieke projecten geteld."),
      ...cards
    );
    sourcesTarget.replaceChildren(...report.by_source.map((source) => {
      const card = node("div", "card");
      card.append(node("strong", "", source.source_name || `Source ${source.source_id}`));
      card.append(node("span", "", source.source_path));
      card.append(node("span", "muted",
        `${Number(source.hashed).toLocaleString("nl-NL")} / ` +
        `${Number(source.ols_locations).toLocaleString("nl-NL")} hashes · ` +
        `${Number(source.pending).toLocaleString("nl-NL")} resterend · ` +
        `${Number(source.physical_bytes).toLocaleString("nl-NL")} fysieke bytes · ` +
        `${Number(source.mib_per_second || 0).toLocaleString("nl-NL")} MiB/s over het recente worker-venster`));
      return card;
    }));
    duplicatesTarget.replaceChildren(...report.exact_duplicate_groups.map((group) => {
      const card = node("div", "card");
      card.append(node("strong", "", group.sha256));
      card.append(node("span", "", `${group.locations_count} fysieke kopieën · ` +
        `${Number(group.size_bytes).toLocaleString("nl-NL")} unieke bytes · ` +
        `${Number(group.duplicate_bytes_saved_from_reprocessing).toLocaleString("nl-NL")} ` +
        "bytes aan dubbel parserwerk vermeden"));
      for (const location of group.locations.slice(0, 10)) {
        card.append(node("span", "muted",
          `${location.source_root || "Bron"} · ${location.path}`));
      }
      return card;
    }));
    if (!report.exact_duplicate_groups.length) {
      duplicatesTarget.replaceChildren(node(
        "p", "muted", "Nog geen volledig gehashte exacte duplicaatgroepen gevonden."
      ));
    }
  } catch (error) {
    summaryTarget.replaceChildren(node(
      "p", "notice", `Hashqueue kon niet worden geladen: ${error.message}`
    ));
  }
}

async function loadSources() {
  const target = document.getElementById("sources");
  target.replaceChildren(node("p", "muted", "Bronnen laden…"));
  try {
    const data = await getJson("/api/source-roots");
    const dedupTarget = document.getElementById("source-dedup");
    const dedup = data.deduplication;
    dedupTarget.replaceChildren(...[
      ["Fysieke OLS-bytes (laatste scans)", dedup.physical_ols_bytes],
      ["Unieke OLS-bytes met geverifieerde hash", dedup.unique_ols_bytes],
      ["Unieke inhoud met hash bevestigd", dedup.unique_ols_contents],
      ["OLS-locaties zonder inhoudshash", dedup.unhashed_ols_locations],
      ["Hash-geverifieerde OLS-locaties", dedup.hashed_ols_locations],
      ["Exacte dubbele extra locaties", dedup.exact_duplicate_locations],
      ["Inhoud op meerdere bronnen", dedup.contents_in_multiple_locations],
      ["Gedeeltelijk verwerkte projecten", dedup.partially_processed_projects]
    ].map(([label, value]) => {
      const card = node("div", "card");
      card.append(node("strong", "", Number(value || 0).toLocaleString("nl-NL")));
      card.append(node("span", "", label));
      return card;
    }));
    const duplicatesTarget = document.getElementById("duplicate-locations");
    const duplicateCards = data.duplicate_locations.map((group) => {
      const card = node("div", "card");
      card.append(node("strong", "", `Exacte inhoud · ${group.sha256}`));
      card.append(node("span", "", `${group.size_bytes} bytes`));
      for (const location of group.locations) {
        card.append(node(
          "span",
          "muted",
          `${location.source_root || "Niet aan een benoemde root gekoppeld"} · ` +
            `${location.source_root_path} · ${location.path}`
        ));
      }
      return card;
    });
    duplicatesTarget.replaceChildren(...duplicateCards);
    const cards = data.roots.map((source) => {
      const card = node("div", "card");
      card.append(node("strong", "", source.name));
      card.append(node(
        "span",
        "",
        source.path || "Pad niet geconfigureerd — er is geen scan uitgevoerd."
      ));
      card.append(node(
        "span",
        "",
        `${source.source_status || source.slot_status} · ` +
          `${Number(source.found_files || 0).toLocaleString("nl-NL")} bestanden · ` +
          `${Number(source.total_size_bytes || 0).toLocaleString("nl-NL")} totale bytes · ` +
        `${Number(source.queued_files || 0).toLocaleString("nl-NL")} wachtend · ` +
        `${Number(source.queued_bytes || 0).toLocaleString("nl-NL")} bytes te indexeren · ` +
        `${Number(source.failed_files || 0).toLocaleString("nl-NL")} fouten`
      ));
      card.append(node("span", "muted",
        `${Number(source.ols_locations || 0).toLocaleString("nl-NL")} fysieke OLS-locaties · ` +
        `${Number(source.ols_physical_bytes || 0).toLocaleString("nl-NL")} OLS-bytes · ` +
        `${Number(source.indexed_files || 0).toLocaleString("nl-NL")} geïndexeerd · ` +
        `laatste volledige scan: ${source.last_scan_at || "geen"}`
      ));
      if (source.latest_scan) {
        const scan = source.latest_scan;
        card.append(node(
          "span",
          "muted",
          `Inventaris ${scan.status}: ${Number(scan.found_paths || 0).toLocaleString("nl-NL")} paden · ` +
            `${Number(scan.ols_paths || 0).toLocaleString("nl-NL")} OLS · ` +
            `${Number(scan.directories_complete || 0).toLocaleString("nl-NL")}/` +
            `${Number(scan.directories_total || 0).toLocaleString("nl-NL")} directories compleet · ` +
            `${Number(scan.errors || 0).toLocaleString("nl-NL")} fouten`
        ));
        if (scan.errors) {
          card.append(node("span", "notice", `${scan.errors} scanproblemen; zie scanrapport/CLI.`));
        }
      }
      if (source.source_id) {
        const actions = node("div", "source-actions");
        const test = node("button", "secondary", "Toegang testen");
        test.type = "button";
        test.addEventListener("click", async () => {
          await sourceOperation(`/api/sources/${source.source_id}/test`, {});
        });
        const scan = node("button", "secondary", "Dry-run scannen");
        scan.type = "button";
        scan.addEventListener("click", async () => {
          await sourceOperation(`/api/sources/${source.source_id}/scan`, {});
        });
        actions.append(test, scan);
        card.append(actions);
      }
      if (source.last_error) card.append(node("span", "notice", source.last_error));
      return card;
    });
    target.replaceChildren(...cards);
  } catch (error) {
    target.replaceChildren(node("p", "notice", `Bronnen konden niet worden geladen: ${error.message}`));
  }
}

async function sourceOperation(url, payload) {
  const notice = document.getElementById("source-notice");
  notice.textContent = "Bronbewerking wordt uitgevoerd…";
  try {
    const result = await postJson(url, payload);
    notice.textContent = result.slot
      ? `${result.slot.name} ingesteld: ${result.slot.source_status}.`
      : result.files === undefined
        ? `Bronstatus: ${result.status}.`
      : `Dry-run klaar: ${result.files} bestanden, ${result.ols_files} OLS; ` +
        `${result.new_ols_files} nieuw, ${result.stat_changed_ols_files} gewijzigd op metadata, ` +
        `${result.hash_unverified_ols_files} inhoud nog niet geverifieerd.`;
    await loadSources();
  } catch (error) {
    notice.textContent = `Bronbewerking mislukt: ${error.message}`;
    await loadSources();
  }
}

async function loadCoverage() {
  const target = document.getElementById("coverage");
  target.replaceChildren(node("p", "muted", "Parserdekking laden…"));
  try {
    const report = await getJson("/api/parser-coverage?limit=1000");
    const cards = report.formats.map((format) => {
      const card = node("div", "card");
      card.append(node("strong", "", `OLS-formaat ${format.format_version}`));
      card.append(node(
        "span",
        "",
        `${format.projects} projecten · ${format.fully_processed} volledig parsergelezen · ` +
          `${format.partially_processed} gedeeltelijk · ${format.independently_validated} extern gevalideerd`
      ));
      return card;
    });
    const issues = report.current_problem_groups.map((issue) =>
      node("p", "muted", `${issue.severity}: ${issue.code} — ${issue.projects} projecten`)
    );
    if (!cards.length) cards.push(node("p", "muted", "Nog geen parserprojecten."));
    target.replaceChildren(...cards, ...issues);
  } catch (error) {
    target.replaceChildren(node("p", "notice", `Parserdekking kon niet worden geladen: ${error.message}`));
  }
}

async function loadProjects(offset = 0) {
  const target = document.getElementById("projects");
  const notice = document.getElementById("notice");
  const summary = document.getElementById("search-summary");
  const previous = document.getElementById("search-prev");
  const next = document.getElementById("search-next");
  const page = document.getElementById("search-page");
  notice.textContent = "";
  summary.textContent = "Zoekopdracht uitvoeren…";
  target.replaceChildren();
  try {
    const url = new URL("/api/project-search", window.location.origin);
    if (offset === 0) {
      currentSearchParams = new URLSearchParams(
        new FormData(document.getElementById("search-form"))
      );
      for (const [key, value] of [...currentSearchParams.entries()]) {
        if (!String(value).trim()) currentSearchParams.delete(key);
      }
    }
    for (const [key, value] of currentSearchParams) {
      url.searchParams.set(key, value);
    }
    url.searchParams.set("limit", "50");
    url.searchParams.set("offset", String(offset));
    const report = await getJson(url);
    const projects = report.results;
    currentSearchOffset = report.offset;
    if (!projects.length) {
      const row = node("tr");
      row.append(node(
        "td", "muted",
        offset ? "Geen projecten meer op deze pagina." : "Geen projecten gevonden."
      ));
      row.firstChild.colSpan = 5;
      target.append(row);
    } else {
      for (const project of projects) {
        const row = node("tr");
        const fileCell = node("td");
        const open = node("button", "path-button", project.source.path);
        open.type = "button";
        open.addEventListener("click", () => loadProject(project.project_id));
        fileCell.append(open);
        fileCell.append(node(
          "span", "search-result-meta",
          `Project ${project.project_id} · formaat ${project.format_version} · ` +
          `${Number(project.source.size_bytes).toLocaleString("nl-NL")} bytes`
        ));
        row.append(fileCell);

        const matchCell = node("td");
        const matchStatus = project.match_status || project.confidence;
        const statusClass = matchStatus === "CONFIRMED"
          ? "pill match-confirmed"
          : matchStatus === "LIKELY" ? "pill match-likely" : "pill";
        matchCell.append(node("span", statusClass, matchStatus));
        matchCell.append(node(
          "span", "search-result-meta",
          project.reference_validation === "validated"
            ? "Referentie onafhankelijk gevalideerd"
            : `Referentie: ${project.reference_validation}`
        ));
        row.append(matchCell);

        const metadata = project.metadata || {};
        const identity = [
          metadata.ecu_type && `ECU ${metadata.ecu_type}`,
          metadata.hw_number && `HW ${metadata.hw_number}`,
          metadata.sw_number && `SW ${metadata.sw_number}`,
          metadata.calibration_id && `CAL ${metadata.calibration_id}`
        ].filter(Boolean);
        row.append(node(
          "td", "", identity.join(" · ") || "Geen ECU-identificaties beschikbaar"
        ));
        row.append(node(
          "td", "",
          `${project.counts.versions} versies · ${project.counts.binary_slots} binaire slots · ` +
          `${project.counts.maps} kaarten · ${project.counts.axes} assen`
        ));
        const stateCell = node("td");
        stateCell.append(node(
          "span",
          project.parser.status === "fully_processed" ? "pill complete" : "pill",
          project.parser.status
        ));
        stateCell.append(node(
          "span", "search-result-meta",
          `${project.parser_warnings.length} parserwaarschuwingen · ` +
          `${project.parser_errors.length} parserfouten`
        ));
        row.append(stateCell);
        target.append(row);
      }
    }
    summary.textContent =
      `${report.result_count.toLocaleString("nl-NL")} resultaten op deze pagina · ` +
      `offset ${report.offset.toLocaleString("nl-NL")} · ` +
      `${(report.query_seconds * 1000).toLocaleString("nl-NL", { maximumFractionDigits: 1 })} ms · ` +
      `${report.ranker}; score is geen kans of validatie`;
    page.textContent = `Pagina ${Math.floor(report.offset / report.limit) + 1}`;
    previous.disabled = report.offset === 0;
    next.disabled = !report.has_more;
  } catch (error) {
    summary.textContent = "";
    notice.textContent = `Projecten konden niet worden geladen: ${error.message}`;
    previous.disabled = true;
    next.disabled = true;
  }
}

async function loadProject(projectId) {
  const section = document.getElementById("detail-section");
  const title = document.getElementById("detail-title");
  const detail = document.getElementById("detail");
  section.hidden = false;
  title.textContent = `Project ${projectId}`;
  detail.textContent = "Details laden…";
  section.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const [project, comparisons] = await Promise.all([
      getJson(`/api/projects/${encodeURIComponent(projectId)}`),
      getJson(`/api/project-matches/${encodeURIComponent(projectId)}`)
    ]);
    title.textContent = project.path;
    detail.textContent = JSON.stringify({
      project_id: project.id,
      source: {
        path: project.path,
        sha256: project.sha256,
        size_bytes: project.size_bytes
      },
      metadata: project.metadata,
      parser: {
        format_version: project.format_version,
        parser_name: project.parser_name,
        parser_version: project.parser_version,
        complete: project.is_complete,
        diagnostics: project.diagnostics
      },
      versions: project.versions,
      binary_slots: project.version_slots,
      maps: project.maps,
      skipped_elements: project.skipped_elements
    }, null, 2);
    const matchesTarget = document.getElementById("similar-projects");
    if (!comparisons.matches.length) {
      matchesTarget.replaceChildren(node(
        "p", "muted",
        "Geen vergelijkbare projecten op basis van de huidige exacte hashes of ECU-identificaties. " +
        "Parser-exportvalidatie en meer projectmetadata kunnen de vergelijking verbeteren."
      ));
    } else {
      matchesTarget.replaceChildren(...comparisons.matches.map((match) => {
        const card = node("article", "card");
        card.append(node(
          "strong", "", `Project ${match.right.id} · ${match.status}`
        ));
        card.append(node("span", "", match.right.path));
        const otherMetadata = match.right.metadata || {};
        const otherIdentity = [
          otherMetadata.ecu_type && `ECU ${otherMetadata.ecu_type}`,
          otherMetadata.hw_number && `HW ${otherMetadata.hw_number}`,
          otherMetadata.sw_number && `SW ${otherMetadata.sw_number}`,
          otherMetadata.calibration_id && `CAL ${otherMetadata.calibration_id}`
        ].filter(Boolean);
        card.append(node(
          "span", "muted",
          `${otherIdentity.join(" · ") || "Geen ECU-identificaties"} · ` +
          `referentie ${match.right.reference_status} · parser ${match.right.parser_status}`
        ));
        card.append(node(
          "span", "muted",
          `SHA-256 ${match.right.sha256 || "ontbreekt"} (${match.right.hash_provenance}; ` +
          `parser-snapshot ${match.right.parser_snapshot_matches_current_content === null
            ? "niet live gecontroleerd"
            : match.right.parser_snapshot_matches_current_content ? "gelijk" : "afwijkend"}) · ` +
          `versies ${(match.right.versions || []).map((version) =>
            version.name || `slot ${version.slot_index}`
          ).join(", ") || "geen"}`
        ));
        const binarySummary = (match.right.binary_slots || []).map((slot) =>
          `slot ${slot.slot_index} ${slot.name || "(ongelabeld)"}: ` +
          `${slot.payload_sha256 ? slot.payload_sha256.slice(0, 12) : "geen payload-hash"}`
        );
        card.append(node(
          "span", "muted",
          `Parser-slots (niet onafhankelijk gevalideerd): ` +
          `${binarySummary.join("; ") || "geen"}`
        ));
        const mapSummary = (match.right.maps || []).slice(0, 5).map((map) =>
          `${map.name || "naamloos"} (${map.columns_count}×${map.rows_count}, ${map.axes_count} assen)`
        );
        card.append(node(
          "span", "muted",
          `Kaartstructuren: ${mapSummary.join("; ") || "geen parserdefinities"}`
        ));
        card.append(node("span", "muted", match.basis));
        card.append(node(
          "span", "muted",
          `Gelijke metadata: ${match.equal_metadata_fields.join(", ") || "geen"} · ` +
          `Verschillende metadata: ${match.conflicting_metadata_fields.join(", ") || "geen"} · ` +
          `${match.parser_slot_payload_matches.length} parser-slot-hashovereenkomsten · ` +
          `${match.common_map_structures.length} gelijke kaartstructuren`
        ));
        card.append(node(
          "span", "muted",
          `Ontbrekende metadata: ${match.right.missing_metadata_fields.join(", ") || "geen"} · ` +
          `Kaartwaarden betrouwbaar gedecodeerd: ${match.right.values_reliably_decoded ? "ja" : "nee"}`
        ));
        card.append(node(
          "span", "muted",
          `Onzekerheden: ${match.unresolved_uncertainties.join(" ")}`
        ));
        return card;
      }));
    }
  } catch (error) {
    detail.textContent = `Projectdetails konden niet worden geladen: ${error.message}`;
    document.getElementById("similar-projects").replaceChildren(node(
      "p", "notice", `Vergelijkbare projecten konden niet worden geladen: ${error.message}`
    ));
  }
}

function showWorkbenchAnalysis(analysis) {
  const result = document.getElementById("ecu-analysis-result");
  const reportElement = document.getElementById("ecu-analysis-report");
  const report = analysis.report;
  const source = analysis.source;
  result.replaceChildren(
    node("strong", "pill", report.status),
    node(
      "span", "muted",
      `${source.filename} · ${Number(source.size_bytes).toLocaleString("nl-NL")} bytes · ` +
      `SHA-256 ${source.sha256}`
    ),
    node(
      "span", "muted",
      `${report.references.exact_references.length} exacte exportmatches · ` +
      `${report.references.slot_candidates.length} onzekere slotkandidaten · ` +
      `${report.references.metadata_candidates.length} metadatakandidaten`
    )
  );
  reportElement.hidden = false;
  reportElement.textContent = JSON.stringify(analysis, null, 2);
}

function showWorkbenchComparison(report) {
  const target = document.getElementById("ecu-comparison-report");
  target.hidden = false;
  target.textContent = JSON.stringify(report, null, 2);
}

async function loadWorkbenchHistory() {
  const analysisTarget = document.getElementById("ecu-analysis-history");
  const comparisonTarget = document.getElementById("ecu-comparison-history");
  const selectA = document.getElementById("ecu-compare-a");
  const selectB = document.getElementById("ecu-compare-b");
  const referenceSelect = document.getElementById("ecu-reference-select");
  try {
    const [history, references] = await Promise.all([
      getJson("/api/workbench/history?limit=100"),
      getJson("/api/workbench/references")
    ]);
    const priorA = selectA.value;
    const priorB = selectB.value;
    const options = history.analyses.map((analysis) => {
      const option = document.createElement("option");
      option.value = analysis.analysis_id;
      option.textContent =
        `${analysis.source_filename} · ${analysis.status} · ${analysis.analysis_id.slice(0, 8)}`;
      return option;
    });
    selectA.replaceChildren(...options.map((option) => option.cloneNode(true)));
    selectB.replaceChildren(...options.map((option) => option.cloneNode(true)));
    if (history.analyses.some((item) => item.analysis_id === priorA)) selectA.value = priorA;
    if (history.analyses.some((item) => item.analysis_id === priorB)) selectB.value = priorB;
    const referenceOptions = references.map((reference) => {
      const option = document.createElement("option");
      option.value = reference.artifact_id;
      option.disabled = !reference.available;
      option.textContent =
        `Project ${reference.project_id} · ${reference.version_name || "versie onbenoemd"} · ` +
        `${reference.status} · ${reference.representation} · ${reference.path.split(/[\\/]/).pop()}` +
        (reference.available ? "" : " · bestand ontbreekt");
      return option;
    });
    referenceSelect.replaceChildren(...referenceOptions);

    if (!history.analyses.length) {
      analysisTarget.replaceChildren(node("p", "muted", "Nog geen ECU-bestanden geanalyseerd."));
    } else {
      analysisTarget.replaceChildren(...history.analyses.map((analysis) => {
        const card = node("article", "card");
        card.append(node("strong", "", analysis.source_filename));
        card.append(node(
          "span", "muted",
          `${analysis.status} · ${Number(analysis.size_bytes).toLocaleString("nl-NL")} bytes · ` +
          `${analysis.sha256.slice(0, 16)}… · ${analysis.created_at}`
        ));
        const actions = node("div", "source-actions");
        const open = node("button", "secondary", "Rapport openen");
        open.type = "button";
        open.addEventListener("click", async () => {
          try {
            showWorkbenchAnalysis(
              await getJson(`/api/workbench/analyses/${encodeURIComponent(analysis.analysis_id)}`)
            );
          } catch (error) {
            document.getElementById("ecu-import-state").textContent = error.message;
          }
        });
        const rerun = node("button", "secondary", "Opnieuw analyseren");
        rerun.type = "button";
        rerun.addEventListener("click", async () => {
          rerun.disabled = true;
          try {
            const result = await postJson(
              `/api/workbench/analyses/${encodeURIComponent(analysis.analysis_id)}/reanalyze`,
              {}
            );
            showWorkbenchAnalysis(result);
            await loadWorkbenchHistory();
          } catch (error) {
            document.getElementById("ecu-import-state").textContent = error.message;
          } finally {
            rerun.disabled = false;
          }
        });
        actions.append(open, rerun);
        card.append(actions);
        return card;
      }));
    }

    if (!history.comparisons.length) {
      comparisonTarget.replaceChildren(node("p", "muted", "Nog geen binaire vergelijkingen."));
    } else {
      comparisonTarget.replaceChildren(...history.comparisons.map((report) => {
        const card = node("button", "card card-button");
        card.type = "button";
        card.append(node(
          "strong", "", `${report.analysis_a.filename} ↔ ${report.analysis_b.filename}`
        ));
        card.append(node(
          "span", "muted",
          `${report.differing_bytes.toLocaleString("nl-NL")} afwijkende bytes · ` +
          `${report.changed_ranges.length} ranges · ${report.semantic_status}`
        ));
        card.addEventListener("click", () => showWorkbenchComparison(report));
        return card;
      }));
    }
  } catch (error) {
    analysisTarget.replaceChildren(
      node("p", "notice", `Workbenchgeschiedenis niet beschikbaar: ${error.message}`)
    );
  }
}

document.getElementById("ecu-workbench-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const file = form.elements.file.files[0];
  const status = document.getElementById("ecu-import-state");
  const button = document.getElementById("ecu-import-button");
  if (!file) return;
  if (!/\.(bin|ori)$/i.test(file.name)) {
    status.textContent = "Alleen ruwe .bin- en .ori-bestanden zijn ondersteund.";
    return;
  }
  const query = new URLSearchParams();
  for (const [key, value] of new FormData(form).entries()) {
    if (key !== "file" && String(value).trim()) query.set(key, String(value).trim());
  }
  button.disabled = true;
  status.textContent =
    `Upload en streaming SHA-256 berekenen (${Number(file.size).toLocaleString("nl-NL")} bytes)…`;
  try {
    const response = await fetch(`/api/workbench/import?${query}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-File-Name": encodeURIComponent(file.name)
      },
      body: file
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    showWorkbenchAnalysis(result);
    status.textContent = "Analyse opgeslagen in de lokale Workbench-geschiedenis.";
    await loadWorkbenchHistory();
  } catch (error) {
    status.textContent = `Import/analyse mislukt: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});

document.getElementById("ecu-compare-button").addEventListener("click", async () => {
  const analysisAId = document.getElementById("ecu-compare-a").value;
  const analysisBId = document.getElementById("ecu-compare-b").value;
  const button = document.getElementById("ecu-compare-button");
  if (!analysisAId || !analysisBId) {
    document.getElementById("ecu-import-state").textContent =
      "Importeer eerst twee bestanden om te vergelijken.";
    return;
  }
  button.disabled = true;
  try {
    const report = await postJson("/api/workbench/compare", {
      analysis_a_id: analysisAId,
      analysis_b_id: analysisBId
    });
    showWorkbenchComparison(report);
    await loadWorkbenchHistory();
  } catch (error) {
    document.getElementById("ecu-import-state").textContent =
      `Vergelijking mislukt: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});

document.getElementById("ecu-reference-import").addEventListener("click", async () => {
  const artifactId = Number(document.getElementById("ecu-reference-select").value);
  const button = document.getElementById("ecu-reference-import");
  if (!artifactId) {
    document.getElementById("ecu-import-state").textContent =
      "Geen beschikbare binaire referentie geselecteerd.";
    return;
  }
  button.disabled = true;
  try {
    const analysis = await postJson("/api/workbench/reference", {
      artifact_id: artifactId
    });
    showWorkbenchAnalysis(analysis);
    document.getElementById("ecu-import-state").textContent =
      "Referentie opnieuw gehasht, veilig gearchiveerd en aan de geschiedenis toegevoegd.";
    await loadWorkbenchHistory();
  } catch (error) {
    document.getElementById("ecu-import-state").textContent =
      `Referentie kon niet worden toegevoegd: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});

document.getElementById("refresh").addEventListener("click", () => {
  loadStatus();
  loadInventory();
  loadMultiSourceInventory();
  loadInventoryLocations();
  loadSourceScanProgress();
  loadHashing();
  loadSources();
  loadCoverage();
  loadProjects(currentSearchOffset);
});
document.getElementById("refresh-sources").addEventListener("click", loadSources);
document.getElementById("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadProjects(0);
});
document.getElementById("clear-search").addEventListener("click", () => {
  document.getElementById("search-form").reset();
  loadProjects(0);
});
document.getElementById("search-prev").addEventListener("click", () => {
  loadProjects(Math.max(0, currentSearchOffset - 50));
});
document.getElementById("search-next").addEventListener("click", () => {
  loadProjects(currentSearchOffset + 50);
});
document.getElementById("source-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("source-path");
  const slot = document.getElementById("source-slot");
  await sourceOperation("/api/source-roots", {
    slot_id: Number(slot.value),
    path: input.value.trim()
  });
});
document.getElementById("close-detail").addEventListener("click", () => {
  document.getElementById("detail-section").hidden = true;
});

loadStatus();
loadInventory();
loadMultiSourceInventory();
loadInventoryLocations();
loadSourceScanProgress();
loadHashing();
loadSources();
loadCoverage();
loadProjects();
loadWorkbenchHistory();
window.setInterval(loadSourceScanProgress, 15000);
window.setInterval(loadHashing, 20000);
