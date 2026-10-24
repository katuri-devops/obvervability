// The page's one script.
// - The colour theme: applied at once, before the page is drawn.
// - The forms: checked in the browser with the rules main.py writes into each
//   field's data-* attributes, so the browser and the server check exactly the
//   same rules. The server checks them again on every submit, whatever this
//   script did. The amount field takes numbers only.
// - Comforts: progress through the form, a live summary, the
//   description's character count, what happens while a claim is decided, the
//   claims made in this browser, and copying and printing on the status page.
(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // Storage in this browser. It can be missing or refuse (private windows):
  // everything works without it.
  // ---------------------------------------------------------------------

  function load(key, fallback) {
    try {
      var text = window.localStorage.getItem(key);
      return text === null ? fallback : JSON.parse(text);
    } catch (error) {
      return fallback;
    }
  }

  function save(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      // Not saved: the page carries on.
    }
  }

  // ---------------------------------------------------------------------
  // The colour theme: "system" follows the computer; "light" and "dark" are
  // chosen with the switch in the title bar. Applied now, before drawing.
  // ---------------------------------------------------------------------

  var THEME_KEY = "aiiqinsure.theme";
  var THEMES = ["system", "light", "dark"];
  var THEME_NAMES = { system: "System", light: "Light", dark: "Dark" };

  function chosenTheme() {
    var theme = load(THEME_KEY, "system");
    return THEMES.indexOf(theme) === -1 ? "system" : theme;
  }

  function applyTheme(theme) {
    if (theme === "system") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", theme);
    }
  }

  applyTheme(chosenTheme());

  function themeSwitch(button) {
    var label = button.querySelector("[data-theme-label]");
    function show(theme) {
      button.dataset.choice = theme;
      label.textContent = THEME_NAMES[theme];
      button.setAttribute("aria-label", "Colour theme: " + THEME_NAMES[theme] + ". Change the theme");
      button.title = "Colour theme: " + THEME_NAMES[theme];
    }
    show(chosenTheme());
    button.hidden = false;
    button.addEventListener("click", function () {
      var next = THEMES[(THEMES.indexOf(chosenTheme()) + 1) % THEMES.length];
      save(THEME_KEY, next);
      applyTheme(next);
      show(next);
    });
  }

  // ---------------------------------------------------------------------
  // The rules of the forms
  // ---------------------------------------------------------------------

  // True if the field's value follows its rule. main.py's follows_rule() checks the same things.
  function follows(field) {
    var rule = field.dataset;
    var value = field.value;
    if (rule.pattern !== undefined && !new RegExp("^(?:" + rule.pattern + ")$").test(value)) return false;
    // Count characters as Python does, so that an emoji counts once.
    var length = Array.from(value).length;
    if (rule.minLength !== undefined && length < Number(rule.minLength)) return false;
    if (rule.maxLength !== undefined && length > Number(rule.maxLength)) return false;
    if (rule.date !== undefined) {
      // A day that exists: 2026-02-30 would become 2026-03-02, and is refused.
      var day = new Date(value + "T00:00:00Z");
      if (isNaN(day.getTime()) || day.toISOString().slice(0, 10) !== value) return false;
      if (rule.notAfter !== undefined && value > rule.notAfter) return false;
    }
    if (rule.moreThan !== undefined) {
      var amount = Number(value);
      if (!(amount > Number(rule.moreThan)) || amount > Number(rule.max)) return false;
    }
    return true;
  }

  function errorElement(field) {
    return document.getElementById(field.id + "-error");
  }

  function showError(field) {
    errorElement(field).textContent = field.dataset.message;
    field.setAttribute("aria-invalid", "true");
  }

  function clearError(field) {
    errorElement(field).textContent = "";
    field.removeAttribute("aria-invalid");
  }

  // The tick at the right of a field that is filled in and right.
  function markField(field) {
    var control = field.closest(".control");
    if (control !== null) control.classList.toggle("is-valid", field.value !== "" && follows(field));
  }

  // The submit button shows that the claim is on its way, and a second click
  // does not send it twice. The button itself is never disabled.
  function setBusy(form, busy) {
    var button = form.querySelector("[data-busy-label]");
    var decision = form.querySelector("[data-decision]");
    if (busy) {
      form.dataset.sending = "yes";
    } else {
      delete form.dataset.sending;
    }
    if (decision !== null) decision.hidden = !busy;
    if (button === null) return;
    var label = button.querySelector(".button-label");
    if (button.dataset.label === undefined) button.dataset.label = label.textContent;
    label.textContent = busy ? button.dataset.busyLabel : button.dataset.label;
    button.classList.toggle("is-busy", busy);
    if (busy) {
      button.setAttribute("aria-busy", "true");
    } else {
      button.removeAttribute("aria-busy");
    }
  }

  // Submitting checks every field. If any is wrong, nothing is sent, every
  // error appears beside its field, and the focus moves to the first one.
  // The submit button stays enabled.
  function checkForm(event) {
    var form = event.target;
    if (form.dataset.sending === "yes") {
      event.preventDefault();
      return;
    }
    var first = null;
    form.querySelectorAll("[data-message]").forEach(function (field) {
      if (follows(field)) {
        clearError(field);
      } else {
        showError(field);
        if (first === null) first = field;
      }
    });
    if (first !== null) {
      event.preventDefault();
      first.focus();
      return;
    }
    setBusy(form, true);
  }

  // An error disappears as soon as its field is corrected.
  function recheck(event) {
    var field = event.target;
    if (field.getAttribute("aria-invalid") === "true" && follows(field)) clearError(field);
    if (field.dataset && field.dataset.message !== undefined) markField(field);
  }

  // ---------------------------------------------------------------------
  // The amount field: numbers only
  // ---------------------------------------------------------------------

  // Keeps digits and the first decimal point; drops everything else.
  function onlyDigitsAndOnePoint(text) {
    var seenPoint = false;
    return Array.from(text).filter(function (character) {
      if (character >= "0" && character <= "9") return true;
      if (character === "." && !seenPoint) {
        seenPoint = true;
        return true;
      }
      return false;
    }).join("");
  }

  // Digits and one decimal point, at most data-decimals decimals, never above
  // data-max. A wrong character is blocked as it is typed; pasted or dropped
  // text is filtered; a change that still breaks the rule is undone.
  function amountField(field) {
    var decimals = Number(field.dataset.decimals);
    var max = Number(field.dataset.max);
    var partial = new RegExp("^[0-9]*(\\.[0-9]{0," + decimals + "})?$");
    function allowed(value) {
      return partial.test(value) && !(Number(value) > max);
    }
    var lastGood = allowed(field.value) ? field.value : "";

    // Typing: refuse a key that would make the value wrong, before it appears.
    field.addEventListener("beforeinput", function (event) {
      if (event.inputType !== "insertText" || event.data === null) return;  // pasting and dropping: below
      // Some phone keyboards offer only a comma: it is taken as the decimal point.
      var typed = event.data === "," ? "." : event.data;
      var start = field.selectionStart;
      var end = field.selectionEnd;
      var next = field.value.slice(0, start) + typed + field.value.slice(end);
      if (!allowed(next)) {
        event.preventDefault();
      } else if (typed !== event.data) {
        event.preventDefault();
        field.setRangeText(typed, start, end, "end");
        field.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });

    // Everything else that changes the value: pasting, dropping, autofill.
    field.addEventListener("input", function () {
      if (!allowed(field.value)) {
        var filtered = onlyDigitsAndOnePoint(field.value);
        field.value = allowed(filtered) ? filtered : lastGood;
      }
      lastGood = field.value;
    });
  }

  // "34 / 1000" under the description, as the user types.
  function counter(element) {
    var field = document.getElementById(element.dataset.counterFor);
    var max = Number(field.dataset.maxLength);
    function update() {
      var length = Array.from(field.value).length;
      element.textContent = length + " / " + max;
      element.classList.toggle("is-near", length > max * 0.9);
    }
    field.addEventListener("input", update);
    update();
  }

  // ---------------------------------------------------------------------
  // The claim page's comforts
  // ---------------------------------------------------------------------

  // How many of the form's sections are complete, as a bar and a tick per section.
  function progress(box, form) {
    var sections = form.querySelectorAll("[data-section]");
    var count = box.querySelector("[data-progress-count]");
    var fillBar = box.querySelector("[data-progress-fill]");
    function update() {
      var done = 0;
      sections.forEach(function (section) {
        var complete = Array.prototype.every.call(section.querySelectorAll("[data-message]"), follows);
        section.classList.toggle("is-complete", complete);
        if (complete) done += 1;
      });
      count.textContent = String(done);
      fillBar.style.setProperty("--progress", (done / sections.length) * 100 + "%");
      box.classList.toggle("is-complete", done === sections.length);
    }
    box.hidden = false;
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    update();
  }

  function readableDay(value) {
    var day = new Date(value + "T00:00:00Z");
    if (isNaN(day.getTime())) return value;
    return day.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  }

  // Your claim so far: the form's values beside it, as they are typed.
  function summary(card, form) {
    var rows = card.querySelectorAll("[data-summary-for]");
    function update() {
      rows.forEach(function (row) {
        var field = form.querySelector("#" + row.dataset.summaryFor);
        var value = field.value.trim();
        var shown = "Not entered yet";
        if (value !== "") {
          if (field.id === "incident_date") shown = readableDay(value);
          else if (field.id === "description") shown = value.length > 90 ? value.slice(0, 90) + "…" : value;
          else if (field.id === "amount_claimed" && follows(field)) shown = Number(value).toFixed(2);
          else shown = value;
        }
        row.querySelector("dd").textContent = shown;
        row.classList.toggle("is-filled", value !== "");
        row.classList.toggle("is-wrong", value !== "" && !follows(field));
      });
    }
    card.hidden = false;
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    update();
  }

  // ---------------------------------------------------------------------
  // The claims made in this browser
  // ---------------------------------------------------------------------

  var RECENT_KEY = "aiiqinsure.recentClaims";
  var CLAIM_ID = /^CLM-[0-9]{6,}$/;
  var STATUS_TONES = { PAID: "success", UNDER_REVIEW: "warning", REJECTED: "danger", PAYMENT_FAILED: "danger", APPROVED: "info" };

  // On a status page: remember this claim, newest first, at most six.
  function rememberClaim(element) {
    var claim = {
      id: element.dataset.claimId,
      status: element.dataset.status,
      name: element.dataset.statusName,
      amount: element.dataset.amount,
      updated: element.dataset.updated
    };
    var list = load(RECENT_KEY, []);
    if (!Array.isArray(list)) list = [];
    list = list.filter(function (item) { return item && item.id !== claim.id; });
    list.unshift(claim);
    save(RECENT_KEY, list.slice(0, 6));
  }

  // On the claim page: the remembered claims, each a link to its status page.
  function showRecent(box) {
    var list = load(RECENT_KEY, []);
    if (!Array.isArray(list)) return;
    var items = box.querySelector("[data-recent-list]");
    list.forEach(function (claim) {
      if (!claim || !CLAIM_ID.test(String(claim.id))) return;  // only well-formed claim IDs become links
      var link = document.createElement("a");
      link.className = "recent-item";
      link.href = "/claims/" + claim.id;

      var id = document.createElement("span");
      id.className = "recent-id";
      id.textContent = claim.id;

      var when = document.createElement("span");
      when.className = "recent-when";
      var moment = new Date(claim.updated);
      when.textContent = (isNaN(moment.getTime()) ? "" : moment.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + " · ")
        + (claim.amount ? String(claim.amount) : "");

      var badge = document.createElement("span");
      badge.className = "recent-badge tone-" + (STATUS_TONES[claim.status] || "info");
      badge.textContent = String(claim.name || claim.status || "");

      link.append(id, badge, when);
      var item = document.createElement("li");
      item.append(link);
      items.append(item);
    });
    box.hidden = items.children.length === 0;
  }

  // ---------------------------------------------------------------------
  // The status page: copy the claim ID, and print the page
  // ---------------------------------------------------------------------

  function copyButton(button) {
    if (!navigator.clipboard) return;  // stays hidden where copying is not possible
    var label = button.querySelector(".button-label");
    var text = label.textContent;
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(button.dataset.copy).then(function () {
        label.textContent = "Copied";
        button.classList.add("is-done");
        setTimeout(function () {
          label.textContent = text;
          button.classList.remove("is-done");
        }, 2000);
      });
    });
  }

  function printButton(button) {
    button.hidden = false;
    button.addEventListener("click", function () {
      window.print();
    });
  }

  // ---------------------------------------------------------------------
  // Everything that needs the page itself
  // ---------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-theme-switch]").forEach(themeSwitch);

    document.querySelectorAll("form[novalidate]").forEach(function (form) {
      form.addEventListener("submit", checkForm);
      form.addEventListener("input", recheck);
      form.addEventListener("change", recheck);
      form.querySelectorAll("[data-message]").forEach(markField);
    });
    document.querySelectorAll("input[data-more-than]").forEach(amountField);
    document.querySelectorAll("[data-counter-for]").forEach(counter);

    var claimForm = document.getElementById("claim-form");
    if (claimForm !== null) {
      document.querySelectorAll("[data-progress]").forEach(function (box) { progress(box, claimForm); });
      document.querySelectorAll("[data-summary]").forEach(function (card) { summary(card, claimForm); });
    }

    document.querySelectorAll("[data-remember-claim]").forEach(rememberClaim);
    document.querySelectorAll("[data-recent]").forEach(showRecent);
    document.querySelectorAll("[data-copy]").forEach(copyButton);
    document.querySelectorAll("[data-print]").forEach(printButton);
  });

  // Coming back with the browser's Back button shows the page as it was left:
  // the submit button must not stay busy.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-sending]").forEach(function (form) {
      setBusy(form, false);
    });
  });
})();
