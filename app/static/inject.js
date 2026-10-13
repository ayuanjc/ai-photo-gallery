(function () {
  "use strict";
  if (window.__A01_INJECTED__) return;
  window.__A01_INJECTED__ = true;

  var INJECTED_FLAG = "data-a01-admin-link";

  function findSidebarNav() {
    var candidates = document.querySelectorAll("nav a[href]");
    for (var i = 0; i < candidates.length; i++) {
      var a = candidates[i];
      var href = a.getAttribute("href") || "";
      if (/^\/(photos|explore|map|people|albums|favorites)/.test(href)) {
        var nav = a.closest("nav") || a.parentElement;
        if (nav) return nav;
      }
    }
    return null;
  }

  function buildLink(model) {
    var link = document.createElement("a");
    link.setAttribute("href", "/admin");
    link.setAttribute("target", "_blank");
    link.setAttribute("rel", "noopener");
    link.setAttribute(INJECTED_FLAG, "1");
    link.title = "AI 图库助手 · 功能开关及管理";

    if (model) {
      link.className = model.className;
      var modelSvg = model.querySelector("svg");
      if (modelSvg) {
        var svg = modelSvg.cloneNode(true);
        svg.style.opacity = "0.95";
        link.appendChild(svg);
      }
    } else {
      link.className =
        "flex gap-1 place-items-center py-2 rounded-lg hover:bg-immich-bg dark:hover:bg-immich-dark-bg hover:text-immich-primary dark:hover:text-immich-dark-primary text-gray-600 dark:text-gray-300";
    }

    var label = document.createElement("span");
    label.textContent = "AI 图库助手";
    label.style.fontSize = "0.9em";
    label.style.fontWeight = "500";
    link.appendChild(label);
    return link;
  }

  function inject() {
    if (document.querySelector("a[" + INJECTED_FLAG + "]")) return true;
    var nav = findSidebarNav();
    if (!nav) return false;
    var links = nav.querySelectorAll("a[href]");
    var model = links.length ? links[links.length - 1] : null;
    var link = buildLink(model);
    if (model && model.parentElement === nav) {
      nav.insertBefore(link, model.nextSibling);
    } else {
      nav.appendChild(link);
    }
    return true;
  }

  function start() {
    if (inject()) return;
    var observer = new MutationObserver(function () {
      if (inject()) observer.disconnect();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setTimeout(function () { observer.disconnect(); inject(); }, 10000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();