/* ============================================================
   BrainBuddy — Theme Toggle Logic
   Default: Dark Mode
   Storage: localStorage key "brainbuddy-theme"
   ============================================================ */

(function () {
  const STORAGE_KEY = "brainbuddy-theme";

  // Apply theme immediately to prevent flash (FOUC)
  function applyTheme(theme) {
    if (theme === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function getSavedTheme() {
    try {
      return localStorage.getItem(STORAGE_KEY) || "dark";
    } catch (e) {
      return "dark";
    }
  }

  function saveTheme(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {
      // localStorage unavailable — fail silently
    }
  }

  // Apply saved theme immediately (runs before DOMContentLoaded)
  applyTheme(getSavedTheme());

  // Toggle handler
  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    const next = current === "light" ? "dark" : "light";
    applyTheme(next);
    saveTheme(next);
  }

  // Inject toggle button into navigation after DOM loads
  function injectToggleButton() {
    // Avoid duplicates
    if (document.getElementById("theme-toggle-btn")) return;

    const btn = document.createElement("button");
    btn.id = "theme-toggle-btn";
    btn.type = "button";
    btn.title = "Toggle Light/Dark Mode";
    btn.setAttribute("aria-label", "Toggle Light/Dark Mode");
    btn.innerHTML = '<span class="icon-sun">☀️</span><span class="icon-moon">🌙</span>';
    btn.addEventListener("click", toggleTheme);

    // Find a good place to insert — look for nav, header, or logout link
    const nav = document.querySelector("nav");
    const logoutLink =
      document.querySelector('a[href="/logout"]') ||
      document.querySelector('a[href*="logout"]');

    if (logoutLink && logoutLink.parentNode) {
      // Insert just before the Logout link
      logoutLink.parentNode.insertBefore(btn, logoutLink);
    } else if (nav) {
      // Append to the nav bar
      nav.appendChild(btn);
    } else {
      // Fallback: fixed position in top-right
      btn.style.position = "fixed";
      btn.style.top = "12px";
      btn.style.right = "12px";
      btn.style.zIndex = "9999";
      document.body.appendChild(btn);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectToggleButton);
  } else {
    injectToggleButton();
  }
})();