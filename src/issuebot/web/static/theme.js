/*
 * issuebot dashboard: the light/dark choice.
 *
 * This file is loaded synchronously from <head>, ahead of the body, so the stamp is on
 * <html> before the first paint and there is no flash of the light theme. It cannot be an
 * inline script: the CSP has no 'unsafe-inline'.
 *
 * The stored choice is one of "light" or "dark", and it is the only thing that puts
 * data-theme on <html>. Storing nothing leaves the attribute off and the media query in
 * app.css follows the operating system, live. So the toggle only ever deals with two
 * states, and an operator who has never touched it keeps tracking their OS.
 */
(function () {
  "use strict";

  var KEY = "issuebot-theme";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  // The toggle is useless without this script, so app.css keeps it hidden until we mark
  // the document. Set it here rather than on DOMContentLoaded: the button then arrives in
  // its final state instead of appearing a moment after the header.
  root.classList.add("js");

  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch (error) {
      // storage can be disabled or full; the OS preference still works
      return null;
    }
  }

  // What the page is actually displaying. The applied attribute comes first, so that a
  // click still works when localStorage cannot be written: the choice would not be
  // remembered across pages, but the toggle keeps toggling.
  function showing() {
    var applied = root.dataset.theme;
    if (applied === "light" || applied === "dark") {
      return applied;
    }
    var choice = stored();
    if (choice) {
      return choice;
    }
    return media && media.matches ? "dark" : "light";
  }

  // Stamped in the head, before the body exists, so the first paint is already right.
  var initial = stored();
  if (initial) {
    root.dataset.theme = initial;
  }

  function announce(theme) {
    var button = document.querySelector(".theme-toggle");
    if (button) {
      // The name describes the action, so the button must not also expose a pressed
      // state: a screen reader would say "switch to light mode, pressed", which is
      // nonsense. Two ARIA patterns, and this is the one an icon toggle wants.
      var label = "Switch to " + (theme === "dark" ? "light" : "dark") + " mode";
      button.setAttribute("title", label);
      button.setAttribute("aria-label", label);
    }
    // the charts paint on a canvas, so CSS cannot restyle them: app.js listens for this
    document.dispatchEvent(new CustomEvent("issuebot:themechange", { detail: { theme: theme } }));
  }

  document.addEventListener("DOMContentLoaded", function () {
    announce(showing());
    var button = document.querySelector(".theme-toggle");
    if (!button) {
      return;
    }
    button.addEventListener("click", function () {
      var next = showing() === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(KEY, next);
      } catch (error) {
        // it will not be remembered, but the stamp below still switches this page
      }
      root.dataset.theme = next;
      announce(next);
    });
  });

  // The OS is in charge only while nothing is stored and nothing has been stamped, so
  // follow it as it changes; a theme the operator picked must not be overridden.
  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (!stored() && !root.dataset.theme) {
        announce(showing());
      }
    });
  }
})();
