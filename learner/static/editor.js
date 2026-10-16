// CodeMirror glue: turns a <textarea> into a code editor with live syntax checking, and
// highlights read-only code blocks. CodeMirror itself comes from vendor/codemirror.js
// (built from tools/codemirror; see its package.json).
import {
  EditorView, EditorState, Compartment, Prec, keymap, indentWithTab, basicSetup,
  syntaxHighlighting, defaultHighlightStyle, linter, lintGutter, oneDark,
  python, sql, MySQL, PostgreSQL, SQLite, MSSQL, StandardSQL,
} from "./vendor/codemirror.js";

const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;

// Our SQL dialect names (models.SQL_DIALECTS) → CodeMirror's, for highlighting and keyword completion.
const SQL_DIALECTS = { mysql: MySQL, postgres: PostgreSQL, sqlite: SQLite, tsql: MSSQL };

function language(format, dialect) {
  if (format === "python") return python();
  if (format === "sql") return sql({ dialect: SQL_DIALECTS[dialect] || StandardSQL });
  return [];
}

const editorTheme = EditorView.theme({
  "&": { fontSize: "0.92rem", border: "1px solid var(--border)", borderRadius: "6px", overflow: "hidden" },
  "&.cm-focused": { outline: "2px solid var(--accent)", outlineOffset: "-1px" },
  ".cm-scroller": { fontFamily: "var(--mono)", lineHeight: "1.5" },
  ".cm-content, .cm-gutter": { minHeight: "9em" },
});

const blockTheme = EditorView.theme({
  "&": { fontSize: "0.92rem", borderRadius: "var(--radius)", padding: "8px 4px" },
  ".cm-scroller": { fontFamily: "var(--mono)", lineHeight: "1.5" },
});

// Ask the server to check the code ~500 ms after typing stops (checkers.py does the work).
function syntaxChecker(format, dialect) {
  return linter(async (view) => {
    const doc = view.state.doc;
    let diagnostics;
    try {
      const response = await fetch("/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ format, dialect, code: doc.toString() }),
      });
      if (!response.ok) return [];
      ({ diagnostics } = await response.json());
    } catch {
      return []; // checking is a convenience; never get in the way of typing
    }
    const pos = (line, col) => {
      const l = doc.line(Math.min(Math.max(line, 1), doc.lines));
      return Math.min(l.from + Math.max(col, 1) - 1, l.to);
    };
    return diagnostics.map((d) => {
      const from = pos(d.line, d.col);
      const to = Math.max(from, pos(d.end_line, d.end_col));
      return { from, to, severity: d.severity, message: d.message };
    });
  }, { delay: 500 });
}

// Replace `textarea` with an editor. The textarea stays in the form (hidden) and is kept in sync,
// so submitting the form works as before. `onSubmit` is bound to Ctrl/Cmd+Enter.
export function mountEditor(textarea, { format, dialect, onSubmit } = {}) {
  const editable = new Compartment();
  const extensions = [
    basicSetup,
    keymap.of([indentWithTab]),
    language(format, dialect),
    syntaxChecker(format, dialect),
    lintGutter(),
    editorTheme,
    editable.of(EditorView.editable.of(true)),
    EditorView.updateListener.of((update) => {
      if (update.docChanged) textarea.value = update.state.doc.toString();
    }),
  ];
  if (dark) extensions.push(oneDark);
  if (onSubmit) {
    extensions.push(Prec.highest(keymap.of([{ key: "Mod-Enter", run: () => { onSubmit(); return true; } }])));
  }
  const view = new EditorView({ doc: textarea.value, extensions });
  textarea.after(view.dom);
  textarea.hidden = true;
  textarea.required = false; // a hidden required field would block submit; the server validates

  return {
    focus: () => view.focus(),
    setReadOnly(readOnly) {
      view.dispatch({ effects: editable.reconfigure(EditorView.editable.of(!readOnly)) });
      if (readOnly) view.contentDOM.blur();
    },
    destroy() {
      view.destroy();
      textarea.hidden = false;
    },
  };
}

// Swap a <pre><code> block for a read-only, syntax-highlighted view.
export function highlightBlock(pre, format, dialect) {
  const code = pre.textContent.replace(/\n$/, "");
  const view = new EditorView({
    doc: code,
    extensions: [
      language(format, dialect),
      dark ? oneDark : syntaxHighlighting(defaultHighlightStyle),
      blockTheme,
      EditorView.editable.of(false),
      EditorState.readOnly.of(true),
    ],
  });
  pre.replaceWith(view.dom);
}
