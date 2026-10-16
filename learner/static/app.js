// Page behavior: the study screen and the card form.
import { mountEditor, highlightBlock } from "./editor.js";

const isTyping = () => {
  const el = document.activeElement;
  return !!el && (["TEXTAREA", "INPUT", "SELECT"].includes(el.tagName) || el.isContentEditable);
};

// Study screen: write → submit → reference appears at once, the LLM's verdict follows → accept or override.
// Keys: Ctrl/Cmd+Enter (or Space outside the answer box) submit, Alt+H hint,
// then Enter accepts the verdict, 1 marks right, 2 marks wrong.
function initStudy(study) {
  const { cardId, format, dialect } = study.dataset;
  const llm = study.dataset.llm === "on";
  const maxHints = Number(study.dataset.maxHints);
  const form = document.getElementById("answer-form");
  const input = document.getElementById("answer-input");
  const answerPanel = document.getElementById("answer");
  const submitRow = document.getElementById("submit-row");
  const verdictBox = document.getElementById("verdict");
  const acceptButton = document.getElementById("accept");
  const question = document.getElementById("grade-question");
  const hintButton = document.getElementById("hint-button");
  const hintStatus = document.getElementById("hint-status");
  const started = performance.now();
  const hints = [];
  let submitted = false;
  let verdictReady = false;
  let editor = null;

  const answerText = () => input.value;
  const post = (url, body) =>
    fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then((r) => (r.ok ? r.json() : { error: `The server returned ${r.status}.` }))
      .catch(() => ({ error: "Couldn't reach the server." }));

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showVerdict(grade) {
    verdictBox.replaceChildren();
    if (grade.error) {
      verdictBox.className = "verdict unavailable";
      verdictBox.append(el("p", "", `Couldn't grade this one (${grade.error}) Mark it yourself.`));
      return;
    }
    const labels = { correct: "Correct", partially_correct: "Partly right", incorrect: "Not quite" };
    verdictBox.className = `verdict ${grade.verdict}`;
    verdictBox.append(el("p", "verdict-label", labels[grade.verdict] + (grade.equivalent_approach ? " · different approach, same result" : "")));
    verdictBox.append(el("p", "", grade.feedback));
    if (grade.rubric_misses.length) {
      const missed = el("ul", "missed");
      grade.rubric_misses.forEach((m) => missed.append(el("li", "", m)));
      verdictBox.append(el("p", "small muted", "Missed:"), missed);
    }
    form.elements.llm_verdict.value = grade.verdict;
    form.elements.llm_feedback.value = grade.feedback;
    acceptButton.value = grade.right ? "right" : "wrong";
    acceptButton.hidden = false;
    question.textContent = "Disagree?";
    document.getElementById("mark-right").firstChild.textContent = "Mark right ";
    document.getElementById("mark-wrong").firstChild.textContent = "Mark wrong ";
    verdictReady = true;
  }

  async function submitAnswer() {
    if (submitted) return;
    submitted = true;
    if (editor) editor.setReadOnly(true);
    else {
      input.readOnly = true;
      input.blur();
    }
    answerPanel.querySelectorAll("pre > code[class^='language-']").forEach((code) => highlightBlock(code.parentElement, format, dialect));
    answerPanel.hidden = false;
    submitRow.hidden = true;
    answerPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });

    if (!llm) return;
    if (!answerText().trim()) {
      verdictBox.hidden = false;
      verdictBox.className = "verdict unavailable";
      verdictBox.replaceChildren(el("p", "", "Nothing written, so there's nothing to grade. Mark it yourself."));
      return;
    }
    verdictBox.hidden = false;
    verdictBox.className = "verdict pending";
    verdictBox.replaceChildren(el("p", "", "Grading… (you can mark it yourself without waiting)"));
    const grade = await post("/grade", { card_id: Number(cardId), answer: answerText() });
    if (document.body.contains(form)) showVerdict(grade);
  }

  async function askForHint() {
    if (!hintButton || submitted || hints.length >= maxHints || hintButton.disabled) return;
    hintButton.disabled = true;
    hintStatus.textContent = "Thinking of a hint…";
    const reply = await post("/hint", { card_id: Number(cardId), answer: answerText(), level: hints.length + 1, earlier: hints });
    hintStatus.textContent = "";
    if (reply.error) {
      hintStatus.textContent = `No hint this time (${reply.error})`;
      hintButton.disabled = false;
      return;
    }
    hints.push(reply.hint);
    form.elements.max_hint_level.value = hints.length;
    document.getElementById("hints").hidden = false;
    document.getElementById("hint-list").append(el("li", "", reply.hint));
    hintButton.disabled = hints.length >= maxHints;
    if (hintButton.disabled) hintStatus.textContent = "That's the last hint.";
    (editor || input).focus();
  }

  let marked = false;
  function mark(buttonId) {
    if (!marked) form.requestSubmit(document.getElementById(buttonId));
  }

  if (input.dataset.editor) {
    editor = mountEditor(input, { format: input.dataset.editor, dialect, onSubmit: submitAnswer });
    editor.focus();
  }

  document.getElementById("submit-answer").addEventListener("click", submitAnswer);
  hintButton?.addEventListener("click", askForHint);
  form.addEventListener("submit", (e) => {
    if (marked) return e.preventDefault(); // one answer per card, even if a key auto-repeats
    marked = true;
    form.elements.duration_ms.value = Math.round(performance.now() - started);
  });

  document.addEventListener("keydown", (e) => {
    if (!submitted) {
      if ((e.key === "Enter" && (e.ctrlKey || e.metaKey)) || (e.key === " " && !isTyping())) {
        e.preventDefault();
        submitAnswer();
      } else if (e.altKey && e.code === "KeyH") {
        e.preventDefault();
        askForHint();
      }
    } else if (!e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === "Enter" && verdictReady) {
        e.preventDefault();
        mark("accept");
      }
      if (e.key === "1") mark("mark-right");
      if (e.key === "2") mark("mark-wrong");
    }
  });
}

// Card form: the reference answer becomes a code editor whenever the format is a code format,
// using the selected collection's SQL dialect.
function initCardForm(form) {
  const textarea = document.getElementById("reference-answer");
  const formatSelect = form.elements.answer_format;
  const collectionSelect = form.elements.collection_id;
  let editor = null;

  function refresh() {
    editor?.destroy();
    editor = null;
    const format = formatSelect.value;
    if (format === "text") return;
    const dialect = collectionSelect.selectedOptions[0]?.dataset.dialect;
    editor = mountEditor(textarea, { format, dialect });
  }

  const newCollectionField = document.getElementById("new-collection-field");
  collectionSelect.addEventListener("change", () => {
    newCollectionField.hidden = collectionSelect.value !== "new";
    if (!newCollectionField.hidden) newCollectionField.querySelector("input").focus();
  });
  formatSelect.addEventListener("change", refresh);
  collectionSelect.addEventListener("change", refresh);
  refresh();
}

const study = document.getElementById("study");
if (study) initStudy(study);
const cardForm = document.getElementById("card-form");
if (cardForm) initCardForm(cardForm);
