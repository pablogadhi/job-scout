// The dashboard's behaviour: search, expandable rows, the vote dialog and the applied
// checkboxes. Job text comes from data-* attributes and is only ever set as textContent.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const status = document.getElementById("status");
  const showStatus = (message) => {
    status.textContent = message;
    status.hidden = !message;
  };

  // --- expandable rows ---------------------------------------------------------------------
  const setExpanded = (row, open) => {
    row.nextElementSibling.hidden = !open;
    row.querySelector(".expand").setAttribute("aria-expanded", String(open));
  };
  document.querySelectorAll("tr.job").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("a, input, button.vote")) return;
      setExpanded(row, row.nextElementSibling.hidden);
    });
  });

  // --- search ------------------------------------------------------------------------------
  const search = document.getElementById("search");
  search.addEventListener("input", () => {
    const query = search.value.trim().toLowerCase();
    document.querySelectorAll("details.section").forEach((section) => {
      let shown = 0;
      section.querySelectorAll("tr.job").forEach((row) => {
        const match = !query || row.dataset.search.includes(query);
        row.hidden = !match;
        if (!match) setExpanded(row, false);
        if (match) shown += 1;
      });
      section.querySelector(".count").textContent = String(shown);
      // A search leaves sections as you left them: its count says what a closed one holds.
      section.querySelector(".empty").hidden = shown > 0;
    });
  });

  // --- the vote dialog (the feedback page's form, posted to /f/<job_id>) --------------------
  const dialog = document.getElementById("vote-dialog");
  const form = dialog.querySelector("form");
  const field = (name) => dialog.querySelector(`[data-field="${name}"]`);
  const emoji = { up: "👍", down: "👎" };
  let current = null; // the clicked vote button

  const ratedLine = (button) =>
    button.dataset.vote
      ? `You rated this ${emoji[button.dataset.vote]} on ${button.dataset.rated}.`
      : "";

  document.querySelectorAll("button.vote").forEach((button) => {
    button.addEventListener("click", () => {
      current = button;
      form.action = `/f/${button.dataset.job}`;
      form.elements.s.value = button.dataset.s;
      form.querySelectorAll('input[name="vote"]').forEach((radio) => {
        radio.checked = radio.value === button.dataset.vote;
      });
      form.elements.reason.value = button.dataset.reason;
      field("title").textContent = button.dataset.title;
      field("company").textContent = button.dataset.company;
      field("rated").textContent = ratedLine(button);
      field("rated").hidden = !button.dataset.vote;
      field("error").hidden = true;
      dialog.showModal();
    });
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

  document.querySelectorAll('input[type="checkbox"][data-key]').forEach((box) => {
    box.addEventListener("change", async () => {
      const applied = box.checked;
      const body = new URLSearchParams({ s: box.dataset.s, applied: applied ? "1" : "0" });
      try {
        const response = await fetch(`/d/applied/${box.dataset.job}`, { method: "POST", body });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        document
          .querySelectorAll('input[type="checkbox"][data-key]')
          .forEach((other) => {
            if (other.dataset.key !== data.key) return;
            other.checked = data.applied;
            other.title = appliedTitle(other, data.applied ? data.date : null);
          });
        showStatus("");
      } catch (error) {
        box.checked = !applied;
        showStatus(`Couldn't save "applied": ${error.message}`);
      }
    });
  });
});
