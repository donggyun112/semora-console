// One catalog for the whole console. The Korean string in the source is the message id
// and English is a lookup on top of it, so the server keeps speaking Korean and nothing
// on the wire changes: the page translates what it is about to draw.
//
// ponytail: a Korean string edited in place silently falls back to Korean here. Fine at
// ~180 strings; extract real keys if the catalog ever outgrows one file.

const KEY = "semora-console:lang";

// `{0}` marks an interpolated slot. `tf` fills it in either language.
const EN = {
  // ── chrome (index.html) ──────────────────────────────────────────────────────
  "Semora 홈": "Semora home",
  "시나리오 선택": "Choose a scenario",
  "선택된 정책": "Selected policies",
  "실행": "Run",
  "정책 편집": "Edit policies",
  "가이드 장면": "Guided scenes",
  "대기": "Idle",
  "정책": "Policies",
  "중단": "Abort",
  "대화": "Chat",
  "실행 중 지시": "Steer the run",
  "보내기": "Send",
  "실행 버전": "Run versions",
  "이벤트 기록": "Event trace",
  "현재 정책으로 실행": "Run with the current policies",
  "다시 실행": "Run again",
  "설정으로 돌아가기": "Back to setup",
  "상세 닫기": "Close details",
  "복사": "Copy",
  "이 도구 호출을 실행할까요?": "Run this tool call?",
  "인자 (수정하면 바뀐 값으로 실행)": "Arguments (edit them and the edited values run)",
  "거부": "Deny",
  "승인": "Approve",
  "중단된 실행을 이어갈 수 있습니다.": "This run stopped and can be continued.",
  "복구": "Recover",
  "결제사에서 확인한 사실을 원장에 넣으면 실행이 이어집니다.":
    "Put what the payment provider confirmed into the ledger and the run continues.",
  "청구 안 됨": "Not charged",
  "청구됨": "Charged",
  "데모 설정을 불러오지 못했습니다.": "The demo setup did not load.",
  "다시 시도": "Try again",
  "실행 정책": "Run policies",
  "정책 닫기": "Close policies",
  "라이프사이클 정책": "Lifecycle policies",

  // ── run phases and tool cards ────────────────────────────────────────────────
  "실행 중": "Running",
  "승인 대기": "Waiting for approval",
  "복구 대기": "Waiting to recover",
  "완료": "Done",
  "오류": "Error",
  "정책으로 차단": "Blocked by policy",
  "실행 안 됨": "Not run",
  "실행 완료": "Ran",
  "알 수 없음": "Unknown",
  "중단됨": "Stopped",
  "응답을 기다리는 중": "Waiting for a response",
  "정책 없음": "No policies",

  // ── guided scenes ────────────────────────────────────────────────────────────
  "정책 없이": "No policy",
  "청구가 그대로 나간다": "The charge goes out as asked",
  "승인 게이트": "Approval gate",
  "루프가 멈추고 사람을 기다린다": "The loop stops and waits for a person",
  "승인 후 재검증": "Re-checked after approval",
  "멈춘 채 dlp_block 을 켜고 승인하면 거부된다":
    "Turn dlp_block on while it waits, then approve, and the call is denied",
  "정책에서 dlp_block 을 켠 다음 승인을 누르세요.":
    "Turn dlp_block on in the policy drawer, then press Approve.",
  "복구 가능한 장애": "A failure it recovers from",
  "복구해도 청구는 한 번": "Recovery still charges once",
  "외부 확인이 필요한 장애": "External reconciliation required",
  "원장만으로는 나갔는지 알 수 없다": "The ledger alone cannot tell whether it went out",
  "마스킹 후 분기": "Branch after masking",
  "정책을 끄고 되감으면 원본이 돌아온다":
    "Turn the policy off, rewind, and the original comes back",
  "끝난 뒤 pii_mask 를 끄고 입력에서 분기해 보세요.":
    "When it finishes, turn pii_mask off and branch from the input.",

  // ── branching ────────────────────────────────────────────────────────────────
  "원본": "Original",
  "분기": "Fork",
  "이 입력에서 분기": "Branch from this input",
  "툴 실행 전 분기": "Branch before the tool runs",
  "툴 결과에서 분기": "Branch from the tool result",
  "이 지점에서 분기": "Branch from here",
  "기록된 결과에 새 정책만 적용": "Apply only the new policy to the recorded result",
  "저장된 결과를 버리고 툴부터 다시": "Drop the stored result and run the tool again",
  "적용": "Applies",
  "없음": "None",
  "건너뜀": "Skips",
  "분기 기준": "Branch point",
  "분기 시작": "Branch starts",
  "{0}부터 다시 실행": "Re-runs from {0}",
  "{0}번 이벤트에서 다시 실행": "Re-run from event {0}",

  "요청이 많습니다. 잠시 후 다시 시도해 주세요.": "Too many requests. Try again in a moment.",

  // ── trace rows (reducer.mjs) ─────────────────────────────────────────────────
  "응답 생성": "Generating a response",
  "{0} 호출": "{0} call",
  "{0} 결과": "{0} result",
  "도구 호출 요청": "tool call requested",
  "도구 호출 생략": "tool call skipped",
  "결제 원장 재사용": "payment ledger reused",
  "도구 결과 가림": "tool result masked",
  "정책 평가": "policy check",
  "승인 후 재검증 · {0}": "Re-checked after approval · {0}",
  "운영자": "Operator",
  // The source of a steer, not the policy drawer's button.
  "steer|정책": "Control",
  "지시 반영": "Steer applied",
  "지시 대기": "Steer queued",
  "다음 도구 경계에 반영": "Applied at the next tool boundary",
  "재개될 때 반영": "Applied when the run resumes",

  // ── policy composer (units.py) ───────────────────────────────────────────────
  "입력 개인정보 가리기": "Mask PII in inputs",
  "모델에 넣기 전 주민번호를 가림.": "Masks SSNs before they reach the model.",
  "기록·청구·발송은 승인 대기. 조회는 통과.":
    "Records, charges and sends wait for approval. Reads pass.",
  "유출 차단": "Egress block",
  "메일 본문의 이메일·주민번호 거부.":
    "Denies a mail body carrying an address or an SSN.",
  "횟수 한도": "Rate cap",
  "개인정보 가리기": "Mask PII",
  "도구 결과의 이메일·주민번호 가림.":
    "Masks addresses and SSNs in a tool result.",
  "컨텍스트 방화벽": "Context firewall",
  "기밀 결과를 정책 문구로 교체.":
    "Replaces a confidential result with a policy notice.",
  "비신뢰 표시": "Mark untrusted",
  "도구 결과에 ‘신뢰할 수 없는 상태’와 구조 표시.":
    "Tags a tool result as untrusted and shows its structure.",
  "결과 폐기": "Drop result",
  "도구 결과를 모델 컨텍스트에서 버림. 효과는 남음.":
    "Drops the tool result from the model's context. The effect stays.",
  "기록 강제": "Force a log",
  "remember_note 전 종료 거부.": "Refuses to finish before remember_note.",
  "{0} · {1}회 동작": "{0} · fired {1} times",
  "실행 중인 설정 · 정책 {0}개": "Running setup · {0} policies",
  "정책 {0}개 선택": "{0} policies selected",

  // ── what a unit says when it acts (units.py, server.py) ──────────────────────
  "거부 — 메일 본문에 이메일이나 주민번호가 있습니다":
    "Denied: the mail body carries an email address or an SSN",
  "이메일·주민번호 가림": "Email address and SSN masked",
  "기밀 결과 → 정책 문구": "Confidential result swapped for a policy notice",
  "신뢰할 수 없는 상태": "Marked untrusted",
  "도구 결과 폐기": "Tool result discarded",
  "종료 거부": "Finish refused",
  "도구 결과를 다시 썼습니다": "The tool result was rewritten",

  // ── why a unit stayed dormant (dormancy.py) ──────────────────────────────────
  "메일 본문에 기밀이 없습니다 (앞에서 가렸거나 모델이 안 담음)":
    "No confidential data in the mail body (masked earlier, or the model left it out)",
  "이 작업에는 외부 메일이 없습니다": "This task sends no outside mail",
  "도구 결과에 개인정보가 없음": "No PII in the tool result",
  "도구 결과에 기밀이 없음": "No confidential data in the tool result",
  "구조화할 도구 결과가 없음": "No tool result to structure",
  "버릴 도구 결과가 없음": "No tool result to drop",
  "이미 remember_note": "remember_note already ran",
  "이미 기록됨": "Already logged",
  "이 작업에는 기록·청구·발송이 없습니다": "This task records, charges and sends nothing",
  "이 작업에서는 동작하지 않음": "Nothing here triggers it",

  // ── ledger stops and failures (server.py, app.js) ────────────────────────────
  "이 워커의 차례는 지났습니다": "This worker's turn has passed",
  "이 워커의 차례는 지났습니다 — 실행은 다른 워커에게 넘어갔습니다":
    "This worker's turn has passed: the run moved to another worker",
  "다른 워커가 잡고 있습니다": "Another worker holds it",
  "다른 워커가 이 실행을 잡고 있습니다": "Another worker holds this run",
  "다른 워커가 이 대화를 잡고 있습니다": "Another worker holds this conversation",
  "복구할 수 없습니다": "It cannot be recovered",
  "자동 복구할 수 없습니다 — 외부 청구 상태를 확인해야 합니다":
    "Automatic recovery is not safe: check the charge with the provider",
  "이 실행에는 확정할 미결 효과가 없습니다": "This run has no unsettled effect to confirm",
  "이 도구 호출은 더 이상 미결 상태가 아닙니다":
    "This tool call is no longer indeterminate",
  "중단된 도구 호출의 복구 지점을 찾을 수 없습니다":
    "The recovery point for the interrupted tool call is unavailable",
  "워커 장애": "Worker failure",
  "실행에 실패했습니다.": "The run failed.",
  "인자는 JSON 객체여야 합니다.": "Arguments must be a JSON object.",
  "실행 스트림을 열지 못했습니다.": "The run stream did not open.",
  "실행 스트림이 완료 상태 없이 종료되었습니다.":
    "The run stream ended without a final state.",
};

// Strings the server builds around a name or a number. Tried only when no exact entry
// matches, so an id stays an id and the sentence around it turns.
const PATTERNS = [
  [/^(\S+)은 되돌릴 수 없습니다\. 승인이 필요합니다\.$/,
    (m) => `${m[1]} cannot be undone. It needs approval.`],
  [/^(\S+)은 기록을 남깁니다\. 승인이 필요합니다\.$/,
    (m) => `${m[1]} leaves a record. It needs approval.`],
  [/^거부 — 이번 실행에서 청구·발송이 (\d+)회를 넘었습니다$/,
    (m) => `Denied: this run went past ${m[1]} charges or sends`],
  [/^청구·발송 (\d+)회\. 메모는 제외\.$/,
    (m) => `${m[1]} charges or sends. Notes do not count.`],
  [/^청구·발송이 (\d+)회를 넘지 않음$/,
    (m) => `Charges and sends stayed under ${m[1]}`],
  // The gate's own words, with the marker the server put in front of them.
  [/^승인 후 재검증 — (.+)$/, (m) => `Re-checked after approval: ${t(m[1])}`],
];

export const LANGS = ["ko", "en"];

// node runs the reducer and the catalog in tests and has no page. Its localStorage
// stand-in warns about a file it was never given, so the choice is only stored where
// there is a reader to remember it for.
const onPage = () => typeof document !== "undefined";

function stored() {
  if (!onPage()) return null;
  try {
    const saved = globalThis.localStorage?.getItem(KEY);
    return LANGS.includes(saved) ? saved : null;
  } catch {
    return null;
  }
}

// Korean by default. The console is written in Korean and English is the translation,
// so an unset browser gets the original rather than a guess from its locale.
let lang = stored() ?? "ko";

export const currentLang = () => lang;

export function setLang(next) {
  if (!LANGS.includes(next)) return;
  lang = next;
  if (!onPage()) return;
  try {
    globalThis.localStorage?.setItem(KEY, next);
  } catch {
    // Storage refused: the choice simply does not survive a reload.
  }
}

/** The string as this language says it. Unknown strings pass through untranslated. */
export function t(text, context = null) {
  if (lang === "ko" || text == null) return text;
  const source = String(text);
  const keyed = context ? EN[`${context}|${source}`] : undefined;
  if (keyed !== undefined) return keyed;
  if (EN[source] !== undefined) return EN[source];
  for (const [pattern, render] of PATTERNS) {
    const match = source.match(pattern);
    if (match) return render(match);
  }
  return source;
}

/** `t` with `{0}`-style slots filled in. The slots are the same in both languages. */
export function tf(text, ...args) {
  return String(t(text)).replace(
    /\{(\d+)\}/g,
    (_, index) => String(args[Number(index)] ?? ""),
  );
}

/**
 * Translate the markup the page ships with. The first pass keeps each element's Korean
 * as its message id, so switching back is a second lookup rather than a reload.
 */
export function applyStatic(documentRef) {
  for (const element of documentRef.querySelectorAll("[data-i18n]")) {
    if (element.dataset.i18nSource === undefined) {
      element.dataset.i18nSource = element.textContent.trim();
    }
    element.textContent = t(element.dataset.i18nSource);
  }
  for (const element of documentRef.querySelectorAll("[data-i18n-label]")) {
    if (element.dataset.i18nLabelSource === undefined) {
      element.dataset.i18nLabelSource = element.getAttribute("aria-label") ?? "";
    }
    element.setAttribute("aria-label", t(element.dataset.i18nLabelSource));
  }
  if (documentRef.documentElement) documentRef.documentElement.lang = lang;
}
