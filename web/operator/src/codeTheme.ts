import { githubLightInit, githubLightStyle } from "@uiw/codemirror-theme-github";

// CSS variables update an already mounted editor when any ancestor toggles .dark.
// Retain Github's exact light syntax palette and map the same tags in dark mode.
export function createCodeTheme(background = "var(--color-panel)") {
  return githubLightInit({
    settings: {
      background,
      foreground: "var(--color-code-foreground)",
      caret: "var(--color-acid)",
      selection: "var(--color-code-selection)",
      selectionMatch: "var(--color-code-selection)",
      gutterBackground: background,
      gutterForeground: "var(--color-code-gutter-foreground)",
    },
    styles: githubLightStyle.map((style, index) => ({
      ...style,
      ...(style.color ? { color: `var(--color-code-${index})` } : {}),
      ...(style.backgroundColor ? { backgroundColor: "var(--color-error-surface)" } : {}),
    })),
  });
}
