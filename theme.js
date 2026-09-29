/* tally-stick.fyi theme: dark unless this browser picked light. Loaded in <head> so the page never flashes the wrong one.
   Stores one word in localStorage ("light" or "dark"); nothing else, and nothing leaves the browser. */
(function () {
  var d = document.documentElement, key = "tally-theme", t = null;
  try { t = localStorage.getItem(key); } catch (e) {}
  d.setAttribute("data-theme", t === "light" ? "light" : "dark");
  d.classList.add("js");
  function wire() {
    // on a narrow screen the stick scrolls sideways: bring the page's own notch into view
    var here = document.querySelector('nav.notches [aria-current="page"]'), nav = here && here.parentNode;
    if (here && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = here.offsetLeft - (nav.clientWidth - here.offsetWidth) / 2;
    var b = document.querySelector("[data-theme-toggle]");
    if (!b) return;
    var paint = function () {
      var light = d.getAttribute("data-theme") === "light";
      b.textContent = light ? "Dark" : "Light";
      b.setAttribute("aria-label", "Switch to the " + (light ? "dark" : "light") + " theme");
    };
    paint();
    b.addEventListener("click", function () {
      var next = d.getAttribute("data-theme") === "light" ? "dark" : "light";
      d.setAttribute("data-theme", next);
      try { localStorage.setItem(key, next); } catch (e) {}
      paint();
      document.dispatchEvent(new Event("themechange"));
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();
})();
