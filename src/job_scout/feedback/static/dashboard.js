// The dashboard's behaviour: search, filters and sorting, expandable rows, the vote dialog,
// and the applied and "no longer available" checkboxes. Job text comes from the page's cells and data-* attributes and is only
// ever set as textContent; a row's details come from /details/<job_id>, HTML this server
// escaped like the page itself.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const status = document.getElementById("status");
  const showStatus = (message) => {
    status.textContent = message;
    status.hidden = !message;
  };
  const jobRow = (element) => element.closest("tr.job");

  // --- expandable rows: details are fetched the first time a row opens -------------------
  const detailsOf = (row) => {
    const next = row.nextElementSibling;
    return next && next.classList.contains("details") ? next : null;
  };

  const loadDetails = async (row, details) => {
    const cell = details.cells[0];
    try {
      const response = await fetch(`/details/${row.dataset.job}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      cell.innerHTML = await response.text();
    } catch (error) {
      cell.textContent = `Couldn't load the details: ${error.message}`;
      details.dataset.failed = "1"; // opened again: try again
    }
  };

  const setExpanded = (row, open) => {
    row.querySelector(".expand").setAttribute("aria-expanded", String(open));
    let details = detailsOf(row);
    if (details && open && details.dataset.failed) {
      details.remove();
      details = null;
    }
    if (!details) {
      if (!open) return;
      details = document.createElement("tr");
      details.className = "details";
      const cell = details.insertCell();
      cell.colSpan = row.cells.length;
      cell.textContent = "Loading…";
      row.after(details);
      loadDetails(row, details);
    }
    details.hidden = !open;
  };

  document.addEventListener("click", (event) => {
    const row = jobRow(event.target);
    if (!row || event.target.closest("a, input, button.vote")) return;
    setExpanded(row, row.querySelector(".expand").getAttribute("aria-expanded") !== "true");
  });

  // --- search, filters and sorting --------------------------------------------------------
  // The server renders the page with the filters and sorts in this cookie already applied, so
  // it never shows every row first; the search isn't remembered.
  const cookieName = document.body.dataset.filtersCookie;
  const search = document.getElementById("search");
  const selects = [...document.querySelectorAll("select[data-filter]")];
  const clear = document.getElementById("clear-filters");
  const tables = [...document.querySelectorAll("table[data-section]")];
  const currentFilters = () =>
    Object.fromEntries(selects.map((select) => [select.dataset.filter, select.value]));
  const unavailableBox = (row) => row.querySelector("input[data-unavailable]");

  const matches = (row, query, filter) => {
    if (query && !row.dataset.search.includes(query)) return false;
    if (filter.watchlist && (row.dataset.watchlist === "1") !== (filter.watchlist === "yes")) {
      return false;
    }
    const vote = row.querySelector("button.vote").dataset.vote;
    if (filter.feedback === "none" ? vote : filter.feedback && vote !== filter.feedback) {
      return false;
    }
    const gone = unavailableBox(row).checked;
    if (filter.available && gone === (filter.available === "yes")) return false;
    const applied = row.querySelector('input[type="checkbox"][data-key]').checked;
    return !filter.applied || applied === (filter.applied === "yes");
  };

  const refresh = () => {
    const query = search.value.trim().toLowerCase();
    const filter = currentFilters();
    selects.forEach((select) => select.classList.toggle("active", Boolean(select.value)));
    clear.hidden = selects.every((select) => select.value === select.dataset.default);
    document.querySelectorAll("details.section").forEach((section) => {
      let shown = 0;
      section.querySelectorAll("tr.job").forEach((row) => {
        const match = matches(row, query, filter);
        if (!match && !row.hidden) setExpanded(row, false);
        row.hidden = !match;
        if (match) shown += 1;
      });
      section.querySelector(".count").textContent = String(shown);
      // Filtering leaves sections as you left them: its count says what a closed one holds.
      section.querySelector(".empty").hidden = shown > 0;
    });
  };

  const saveState = () => {
    const state = new URLSearchParams(currentFilters());
    tables.forEach((table) => state.set(`sort_${table.dataset.section}`, table.dataset.sort));
    const value = encodeURIComponent(state.toString());
    document.cookie = `${cookieName}=${value}; Max-Age=31536000; Path=/; SameSite=Strict`;
  };

  search.addEventListener("input", refresh);
  selects.forEach((select) =>
    select.addEventListener("change", () => {
      saveState();
      refresh();
    }),
  );
  clear.addEventListener("click", () => {
    selects.forEach((select) => {
      select.value = select.dataset.default;
    });
    saveState();
    refresh();
  });

  // Sorting: Score sorts best first (the server's order, kept in data-rank); Posted sorts
  // newest first, and again oldest first. Jobs without a date go last either way.
  const sortRows = (table, sort) => {
    const tbody = table.tBodies[0];
    const rank = (row) => Number(row.dataset.rank);
    const rows = [...tbody.querySelectorAll("tr.job")].sort((a, b) => rank(a) - rank(b));
    let ordered = rows;
    if (sort !== "score") {
      const direction = sort === "posted_asc" ? 1 : -1;
      const posted = (row) => Date.parse(row.dataset.posted);
      const dated = rows.filter((row) => row.dataset.posted);
      dated.sort((a, b) => direction * (posted(a) - posted(b))); // stable: ties keep score order
      ordered = [...dated, ...rows.filter((row) => !row.dataset.posted)];
    }
    ordered.forEach((row) => {
      const details = detailsOf(row); // read before moving the row away from it
      tbody.append(row);
      if (details) tbody.append(details);
    });
    table.dataset.sort = sort;
    table.querySelectorAll("button.sort").forEach((button) => {
      const th = button.closest("th");
      const active = sort.startsWith(button.dataset.sortBy);
      const ascending = sort === "posted_asc";
      if (active) th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
      button.querySelector(".arrow").textContent = active ? (ascending ? " ↑" : " ↓") : "";
    });
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button.sort");
    if (!button) return;
    const table = button.closest("table");
    const next =
      button.dataset.sortBy === "score"
        ? "score"
        : table.dataset.sort === "posted_desc"
          ? "posted_asc"
          : "posted_desc";
    sortRows(table, next);
    saveState();
  });

  // --- the vote dialog (the feedback page's form, posted to /f/<job_id>) --------------------
  const dialog = document.getElementById("vote-dialog");
  const form = dialog.querySelector("form");
  const field = (name) => dialog.querySelector(`[data-field="${name}"]`);
  const emoji = { up: "👍", down: "👎" };
  let current = null; // the clicked vote button

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button.vote");
    if (!button) return;
    const row = jobRow(button);
    current = button;
    form.action = `/f/${row.dataset.job}`;
    form.elements.s.value = row.dataset.s;
    const vote = button.dataset.vote;
    form.querySelectorAll('input[name="vote"]').forEach((radio) => {
      radio.checked = radio.value === vote;
    });
    form.elements.reason.value = button.dataset.reason || "";
    field("title").textContent = row.querySelector(".expand").textContent;
    field("company").textContent = row.querySelector(".company").textContent;
    field("rated").textContent = vote
      ? `You rated this ${emoji[vote]} on ${button.dataset.rated}.`
      : "";
    field("rated").hidden = !vote;
    field("error").hidden = true;
    dialog.showModal();
  });
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = new URLSearchParams(new FormData(form));
    let response;
    try {
      // The endpoint answers a saved vote with a redirect back to its page: that's success.
      response = await fetch(form.action, { method: "POST", body, redirect: "manual" });
    } catch {
      response = null;
    }
    if (response && (response.type === "opaqueredirect" || response.ok)) {
      const vote = body.get("vote");
      const reason = (body.get("reason") || "").trim();
      current.dataset.vote = vote;
      current.dataset.reason = reason;
      current.dataset.rated = new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD
      current.textContent = emoji[vote];
      current.title = reason || "No reason given";
      dialog.close();
      refresh(); // a feedback filter may now hide the row
      return;
    }
    let message = "Couldn't save: the server didn't answer.";
    if (response) {
      // The feedback page's own error message, read from its HTML as text.
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const error = page.querySelector(".error");
      message = error ? error.textContent : `Couldn't save (HTTP ${response.status}).`;
    }
    field("error").textContent = message;
    field("error").hidden = false;
  });

  // --- applied: one checkbox marks the whole company ----------------------------------------
  const appliedTitle = (box, date) => {
    const company = box.dataset.company;
    if (date) return `Applied to ${company || "this job"} on ${date}`;
    return company ? `Marks every ${company} job` : "Marks this job";
  };

  document.addEventListener("change", async (event) => {
    const box = event.target;
    if (!box.matches('input[type="checkbox"][data-key]')) return;
    const row = jobRow(box);
    const applied = box.checked;
    const body = new URLSearchParams({ s: row.dataset.s, applied: applied ? "1" : "0" });
    try {
      const response = await fetch(`/applied/${row.dataset.job}`, { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      document.querySelectorAll('input[type="checkbox"][data-key]').forEach((other) => {
        if (other.dataset.key !== data.key) return;
        other.checked = data.applied;
        other.title = appliedTitle(other, data.applied ? data.date : null);
      });
      showStatus("");
      refresh(); // an applied filter may now hide the company's rows
    } catch (error) {
      box.checked = !applied;
      showStatus(`Couldn't save "applied": ${error.message}`);
    }
  });

  // --- no longer available: one checkbox marks every copy of the job ------------------------
  document.addEventListener("change", async (event) => {
    const box = event.target;
    if (!box.matches("input[data-unavailable]")) return;
    const row = jobRow(box);
    const gone = box.checked;
    const body = new URLSearchParams({ s: row.dataset.s, unavailable: gone ? "1" : "0" });
    try {
      const response = await fetch(`/unavailable/${row.dataset.job}`, { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      box.checked = data.unavailable;
      box.title = data.unavailable
        ? `No longer available since ${data.date}`
        : "Mark as no longer available (every copy of this job)";
      row.classList.toggle("unavailable", data.unavailable);
      showStatus("");
      refresh(); // the availability filter may now hide the row
    } catch (error) {
      box.checked = !gone;
      showStatus(`Couldn't save "no longer available": ${error.message}`);
    }
  });
});
