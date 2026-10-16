// Everything learner/static/editor.js needs from CodeMirror, bundled into one file so there's
// exactly one copy of @codemirror/state. Rebuild with: npm install && npm run build
export { EditorView, keymap } from "@codemirror/view";
export { indentWithTab } from "@codemirror/commands";
export { EditorState, Compartment, Prec } from "@codemirror/state";
export { syntaxHighlighting, defaultHighlightStyle } from "@codemirror/language";
export { basicSetup } from "codemirror";
export { python } from "@codemirror/lang-python";
export { sql, MySQL, PostgreSQL, SQLite, MSSQL, StandardSQL } from "@codemirror/lang-sql";
export { linter, lintGutter } from "@codemirror/lint";
export { oneDark } from "@codemirror/theme-one-dark";
