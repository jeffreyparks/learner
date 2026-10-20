// Page behavior: the study screen and the card form.
import { mountEditor, highlightBlock } from "./editor.js";

const isTyping = () => {
  const el = document.activeElement;
  return !!el && (["TEXTAREA", "INPUT", "SELECT"].includes(el.tagName) || el.isContentEditable);
};

// Study screen: write → submit → reference appears at once, the LLM's verdict follows → accept or override.
// Keys: Ctrl/Cmd+Enter (or Space outside the answer box) submit, Alt+H hint, Alt+A show answer, Alt+T ask the tutor,
// then Enter accepts the verdict, 1 marks right, 2 marks wrong (after Show answer, Enter moves on).
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
  const showAnswerNote = document.getElementById("show-answer-note");
  const started = performance.now();
  const hints = [];
  let submitted = false;
  let verdictReady = false;
  let revealed = false;
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

  // Lock the answer box and show the reference answer. Returns false if it was already shown.
  function reveal() {
    if (submitted) return false;
    submitted = true;
    if (editor) editor.setReadOnly(true);
    else {
      input.readOnly = true;
      input.blur();
    }
    answerPanel.querySelectorAll("pre > code[class^='language-']").forEach((code) => highlightBlock(code.parentElement, format, dialect));
    answerPanel.hidden = false;
    submitRow.hidden = true;
    showAnswerNote.hidden = true;
    answerPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return true;
  }

  // "Show answer": study the reference without grading; it's recorded as `revealed`, which leaves the score alone.
  function showAnswer() {
    if (!reveal()) return;
    revealed = true;
    document.getElementById("grade-row").hidden = true;
    document.getElementById("revealed-row").hidden = false;
  }

  async function submitAnswer() {
    if (!reveal()) return;

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
    form.elements.max_hint_level.value = Math.max(hints.length, Number(form.elements.max_hint_level.value));
    document.getElementById("hints").hidden = false;
    document.getElementById("hint-list").append(el("li", "", reply.hint));
    hintButton.disabled = hints.length >= maxHints;
    if (hintButton.disabled) hintStatus.textContent = "That's the last hint.";
    (editor || input).focus();
  }

  // "Ask the tutor": a small chat about this card. The server keeps no chat; we send it all each time.
  // Asking before the answer is on screen counts as a hint, since the tutor may point you in a direction.
  const tutorBox = document.getElementById("tutor");
  const tutorText = document.getElementById("tutor-text");
  const tutorLog = document.getElementById("tutor-log");
  const tutorStatus = document.getElementById("tutor-status");
  const tutorSend = document.getElementById("tutor-send");
  const chat = [];

  async function askTutor() {
    const text = tutorText.value.trim();
    if (!text || tutorSend.disabled) return;
    chat.push({ role: "learner", text });
    tutorLog.append(el("p", "tutor-msg learner", text));
    tutorText.value = "";
    tutorSend.disabled = true;
    tutorStatus.textContent = "Thinking…";
    if (!submitted && Number(form.elements.max_hint_level.value) < 1) form.elements.max_hint_level.value = 1;
    const reply = await post("/ask", { card_id: Number(cardId), answer: answerText(), answer_shown: submitted, messages: chat });
    tutorSend.disabled = false;
    if (reply.error) {
      chat.pop(); // let them resend
      tutorLog.lastChild.remove();
      tutorText.value = text;
      tutorStatus.textContent = `The tutor couldn't answer (${reply.error})`;
      return;
    }
    tutorStatus.textContent = "";
    chat.push({ role: "tutor", text: reply.reply });
    const bubble = el("div", "tutor-msg tutor markdown");
    bubble.innerHTML = reply.html; // rendered server-side from Markdown with raw HTML disabled
    tutorLog.append(bubble);
    bubble.scrollIntoView({ behavior: "smooth", block: "nearest" });
    tutorText.focus();
  }

  function openTutor() {
    if (!tutorBox) return;
    tutorBox.open = true;
    tutorText.focus();
  }

  tutorSend?.addEventListener("click", askTutor);
  tutorText?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      askTutor();
    }
  });
  tutorBox?.addEventListener("toggle", () => {
    if (tutorBox.open) tutorText.focus();
  });

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
  document.getElementById("show-answer").addEventListener("click", showAnswer);
  form.addEventListener("submit", (e) => {
    if (marked) return e.preventDefault(); // one answer per card, even if a key auto-repeats
    marked = true;
    form.elements.duration_ms.value = Math.round(performance.now() - started);
  });

  document.addEventListener("keydown", (e) => {
    if (tutorBox?.contains(e.target)) return; // typing in the chat: don't treat 1, 2 or Enter as grading keys
    if (e.altKey && e.code === "KeyT" && tutorBox) {
      e.preventDefault();
      openTutor();
      return;
    }
    if (!submitted) {
      if ((e.key === "Enter" && (e.ctrlKey || e.metaKey)) || (e.key === " " && !isTyping())) {
        e.preventDefault();
        submitAnswer();
      } else if (e.altKey && e.code === "KeyH") {
        e.preventDefault();
        askForHint();
      } else if (e.altKey && e.code === "KeyA") {
        e.preventDefault();
        showAnswer();
      }
    } else if (revealed) {
      if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        mark("next-card");
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

// Images in a card's Markdown (DESIGN.md §12.1): paste, drop, or "Add image" on any textarea marked
// data-images. The image is stored on the server and its Markdown inserted at the cursor; thumbnails
// below the box show which images the text uses.
function initImageTools(textarea) {
  const tools = document.createElement("div");
  tools.className = "image-tools";
  const button = Object.assign(document.createElement("button"), { type: "button", textContent: "Add image" });
  const picker = Object.assign(document.createElement("input"), { type: "file", accept: "image/jpeg,image/png,image/gif,image/webp", hidden: true });
  const status = Object.assign(document.createElement("span"), { className: "muted small" });
  const thumbs = Object.assign(document.createElement("div"), { className: "thumbs" });
  tools.append(button, picker, status, thumbs);
  textarea.after(tools);

  function showThumbs() {
    const urls = [...new Set(textarea.value.match(/\/media\/[0-9a-f]{64}/g) || [])];
    thumbs.replaceChildren(...urls.map((src) => Object.assign(document.createElement("img"), { src, alt: "" })));
  }

  async function upload(file) {
    const name = (file.name || "image").replace(/\.[a-z0-9]+$/i, "").replace(/[-_]+/g, " ");
    const alt = window.prompt("Describe the image in a few words (the AI grader and screen readers use this):", name === "image" ? "" : name);
    if (alt === null) return; // cancelled
    const data = new FormData();
    data.append("file", file);
    data.append("alt", alt);
    status.textContent = "Adding image…";
    const reply = await fetch("/media", { method: "POST", body: data }).then((r) => r.json()).catch(() => ({ error: "Couldn't reach the server." }));
    status.textContent = "";
    if (reply.error) {
      status.textContent = reply.error;
      return;
    }
    const at = textarea.selectionStart ?? textarea.value.length;
    const before = textarea.value.slice(0, at), after = textarea.value.slice(at);
    const insert = (before && !before.endsWith("\n") ? "\n\n" : "") + reply.markdown + "\n";
    textarea.value = before + insert + after;
    textarea.selectionStart = textarea.selectionEnd = at + insert.length;
    textarea.focus();
    showThumbs();
  }

  const images = (list) => [...(list || [])].filter((f) => f.type.startsWith("image/"));
  button.addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => { images(picker.files).forEach(upload); picker.value = ""; });
  textarea.addEventListener("paste", (e) => {
    const files = images(e.clipboardData?.files);
    if (files.length) { e.preventDefault(); files.forEach(upload); }
  });
  textarea.addEventListener("dragover", (e) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); });
  textarea.addEventListener("drop", (e) => {
    const files = images(e.dataTransfer?.files);
    if (files.length) { e.preventDefault(); files.forEach(upload); }
  });
  textarea.addEventListener("input", showThumbs);
  showThumbs();
  return tools;
}

// Card form: the reference answer becomes a code editor whenever the format is a code format,
// using the selected collection's SQL dialect. Image tools are offered on text, not code.
function initCardForm(form) {
  const textarea = document.getElementById("reference-answer");
  const formatSelect = form.elements.answer_format;
  const collectionSelect = form.elements.collection_id;
  let editor = null;

  const imageTools = new Map([...form.querySelectorAll("textarea[data-images]")].map((t) => [t, initImageTools(t)]));

  function refresh() {
    editor?.destroy();
    editor = null;
    const format = formatSelect.value;
    imageTools.get(textarea).hidden = format !== "text";
    if (format === "text") return;
    const dialect = collectionSelect.selectedOptions[0]?.dataset.dialect;
    editor = mountEditor(textarea, { format, dialect });
  }

  // The Tier dropdown lists the chosen collection's real tiers, plus "+ New tier at the end".
  const tierSelect = document.getElementById("tier-select");
  const tierChoices = JSON.parse(document.getElementById("tier-choices")?.textContent || "{}");
  function refreshTiers() {
    const current = tierSelect.value;
    const options = (tierChoices[collectionSelect.value] || []).map(([value, label]) => new Option(label, value));
    options.push(new Option("+ New tier at the end", tierSelect.dataset.new));
    tierSelect.replaceChildren(...options);
    tierSelect.value = [...tierSelect.options].some((o) => o.value === current) ? current : tierSelect.options[0].value;
  }
  collectionSelect.addEventListener("change", refreshTiers);

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
