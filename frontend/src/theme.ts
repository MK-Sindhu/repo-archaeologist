/** Light/dark: follows the system until the person picks one; the choice is remembered. */

export type Theme = "light" | "dark";

export function currentTheme(): Theme {
  if (typeof document === "undefined") return "light"; // server render: the head script decides
  const set = document.documentElement.dataset.theme;
  if (set === "light" || set === "dark") return set;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function setTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("arch-theme", theme);
  } catch {
    /* not persisted; still applied for this page */
  }
}
