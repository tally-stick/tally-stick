/* tally-stick.fyi, shared by every page: the light/dark choice and the header search.
   Loaded in <head> so the page never flashes the wrong theme. Stores one word in localStorage ("light" or "dark");
   nothing else, and nothing leaves the browser. */
(function () {
  var d = document.documentElement, key = "tally-theme", t = null;
  try { t = localStorage.getItem(key); } catch (e) {}
  d.setAttribute("data-theme", t === "light" ? "light" : "dark");
  d.classList.add("js");
  function wire() {
    // on a narrow screen the menu scrolls sideways: bring the page's own notch into view
    var here = document.querySelector('nav.notches [aria-current="page"], nav.notches a.on'), nav = here && here.parentNode;
    if (here && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = here.offsetLeft - (nav.clientWidth - here.offsetWidth) / 2;
    // search: every page sends it to the home page's reader, which opens a citizen by handle or searches posts
    var f = document.querySelector("[data-site-search]");
    if (f) f.addEventListener("submit", function (e) {
      e.preventDefault();
      var q = f.elements.q.value.trim();
      if (!q) return;
      var to = "#/find/" + encodeURIComponent(q);
      if (location.pathname === "/" || location.pathname === "/index.html") location.hash = to;
      else location.href = "https://tally-stick.fyi/" + to;
    });
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
