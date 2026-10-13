import { useEffect, useState } from "react"

export type Theme = "system" | "light" | "dark"
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    try { const stored = localStorage.getItem("kaiforward.theme"); return stored === "light" || stored === "dark" ? stored : "system" } catch { return "system" }
  })
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)")
    const apply = () => document.documentElement.classList.toggle("dark", theme === "dark" || (theme === "system" && media.matches))
    apply()
    media.addEventListener("change", apply)
    try { localStorage.setItem("kaiforward.theme", theme) } catch { /* Theme still works without storage. */ }
    return () => media.removeEventListener("change", apply)
  }, [theme])
  return { theme, setTheme }
}
