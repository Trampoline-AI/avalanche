import { python } from "@codemirror/lang-python";
import { EditorState } from "@codemirror/state";
import { githubLightInit } from "@uiw/codemirror-theme-github";
import { EditorView } from "@codemirror/view";
import { useEffect, useRef } from "react";

interface PythonSourceProps {
  source: string;
}

const sourceColorTheme = githubLightInit({
  settings: { background: "var(--color-canvas)" },
});

const sourceLayout = EditorView.theme({
  "&": {
    width: "max-content",
    minWidth: "calc(100% - 8px)",
    minHeight: "calc(100% - 12px)",
    marginTop: "4px",
    marginRight: "8px",
    marginBottom: "8px",
    borderRadius: "5px",
  },
  ".cm-content": {
    fontFamily: "var(--font-mono)",
    fontSize: "10px",
    lineHeight: "1.6",
    padding: "12px 0 0 12px",
  },
  ".cm-scroller": { fontFamily: "var(--font-mono)", overflow: "visible" },
});

export function PythonSource({ source }: PythonSourceProps) {
  const parent = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (parent.current === null) return;
    const view = new EditorView({
      parent: parent.current,
      state: EditorState.create({
        doc: source,
        extensions: [
          python(),
          sourceColorTheme,
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          EditorView.contentAttributes.of({ "aria-label": "Source code", tabindex: "0" }),
          sourceLayout,
        ],
      }),
    });
    return () => view.destroy();
  }, [source]);

  return <div className="python-source h-full min-h-0 min-w-0 overflow-auto" ref={parent} />;
}
