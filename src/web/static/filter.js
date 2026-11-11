(function () {
  const STORAGE_KEY = "analyticsProjectFilter";
  const bar = document.getElementById("projectFilterBar");
  const nameNode = document.getElementById("projectFilterName");
  const clearButton = document.getElementById("projectFilterClear");
  if (!bar || !nameNode || !clearButton) return;

  let silent = false;

  function current() {
    try {
      return sessionStorage.getItem(STORAGE_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function save(name) {
    try {
      if (name) sessionStorage.setItem(STORAGE_KEY, name);
      else sessionStorage.removeItem(STORAGE_KEY);
    } catch (error) {
      /* приватный режим: фильтр живёт только до перезагрузки */
    }
  }

  function projectsOf(row) {
    return (row.dataset.projects || "").split("||").filter(Boolean);
  }

  function refreshCounts() {
    document.querySelectorAll("[id$='Count']").forEach(function (count) {
      const card = count.closest(".card");
      if (!card) return;
      const rows = card.querySelectorAll("tbody tr[data-projects]");
      if (!rows.length || !current()) return;
      let visible = 0;
      rows.forEach(function (row) {
        if (!row.hidden && !row.classList.contains("project-filtered")) visible += 1;
      });
      count.textContent = visible + " из " + rows.length;
    });
  }

  function syncMissSelect(name) {
    const select = document.getElementById("missProject");
    if (!select) return;
    const has = Array.from(select.options).some(function (option) { return option.value === name; });
    const next = name && has ? name : "";
    if (select.value === next) return;
    silent = true;
    select.value = next;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    silent = false;
  }

  function apply() {
    const name = current();
    document.querySelectorAll("tr[data-projects]").forEach(function (row) {
      const match = !name || projectsOf(row).indexOf(name) !== -1;
      row.classList.toggle("project-filtered", !match);
    });
    document.querySelectorAll(".filter-hit").forEach(function (button) {
      button.classList.toggle("is-on", button.dataset.filter === name);
    });
    nameNode.textContent = name;
    bar.hidden = !name;
    refreshCounts();
  }

  function restorePageFilters() {
    silent = true;
    document.querySelectorAll(".rsya-all-filters input, .rsya-all-filters select").forEach(function (field) {
      const eventName = field.tagName === "SELECT" || field.type === "checkbox" ? "change" : "input";
      field.dispatchEvent(new Event(eventName, { bubbles: true }));
    });
    silent = false;
  }

  document.addEventListener("click", function (event) {
    const hit = event.target.closest(".filter-hit");
    if (!hit) return;
    event.preventDefault();
    event.stopPropagation();
    const name = hit.dataset.filter || "";
    if (!name) return;
    save(current() === name ? "" : name);
    syncMissSelect(current());
    if (!current()) restorePageFilters();
    apply();
  });

  document.addEventListener("change", function (event) {
    if (silent || event.target.id !== "missProject") return;
    save(event.target.value || "");
    apply();
  });

  document.addEventListener("input", function (event) {
    if (silent || !event.target.closest(".rsya-all-filters")) return;
    apply();
  });

  document.addEventListener("change", function (event) {
    if (silent || event.target.id === "missProject" || !event.target.closest(".rsya-all-filters")) return;
    apply();
  });

  clearButton.addEventListener("click", function () {
    save("");
    syncMissSelect("");
    restorePageFilters();
    apply();
  });

  syncMissSelect(current());
  apply();
})();
