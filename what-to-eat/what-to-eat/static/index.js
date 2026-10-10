"use strict";

// ===========================================================================
// 설정
// ===========================================================================
const QUICK_RADII = [300, 500, 1000, 2000];
const WIDEN_STEPS = [300, 500, 1000, 2000, 3000]; // "거리를 넓혀 보세요"에서 다음 단계
const WALK_M_PER_MIN = 80;   // 걸어서 1분에 80m
const SPIN_MS = 4200;        // 돌아가는 시간

// 차분한 색 16가지 (어두운 글자가 잘 읽히는 밝은 톤)
const SLICE_COLORS = [
  "#ece3d4", "#d5dfd8", "#ead5cc", "#d8dde8", "#efe4c6", "#ddd5e6", "#e1e6d2", "#ecd9de",
  "#d2e1e3", "#e8dbc8", "#ddd3ca", "#d0dbcc", "#eedcd2", "#d4d9e1", "#e6dfc4", "#e0d2da",
];

// localStorage에 저장하는 이름들
const KEY_RADIUS = "wmm.radius";
const KEY_OFF = "wmm.offMenus";
const KEY_CUSTOM = "wmm.customMenus";
const KEY_RECENT = "wmm.recent";

// ===========================================================================
// 상태
// ===========================================================================
let radius = loadRadius();
let coords = null;          // {lat, lng, label} — 메모리에만 둔다 (README 7번)
let data = null;            // /api/places 결과
let requestSeq = 0;         // 마지막 요청만 쓰기 위한 번호
let currentAbort = null;
let offMenus = new Set(loadJson(KEY_OFF, []));
let customMenus = loadJson(KEY_CUSTOM, []).filter(isValidMenuName);
let kindOff = new Set();    // 종류로 거르기 (이번 검색에서만)
let sessionExcluded = new Set(); // "방금 나온 메뉴 빼고 돌리기"로 뺀 메뉴
let wheelItems = [];        // 지금 돌림판에 있는 메뉴 이름
let rotation = 0;           // 돌림판 각도 (라디안)
let spinning = false;
let lastResult = null;

// ===========================================================================
// 화면 요소
// ===========================================================================
const $ = (id) => document.getElementById(id);
const screens = { start: $("screen-start"), menus: $("screen-menus"), wheel: $("screen-wheel") };
const slider = $("radius-slider");
const radiusLabel = $("radius-label");
const startStatus = $("start-status");
const menusStatus = $("menus-status");
const menusBody = $("menus-body");
const canvas = $("wheel");
const ctx = canvas.getContext("2d");

// ===========================================================================
// 작은 도우미
// ===========================================================================
function loadJson(key, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(key));
    return Array.isArray(v) ? v : fallback;
  } catch (_) {
    return fallback;
  }
}
function saveJson(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* 저장 못 해도 계속 */ }
}
function loadRadius() {
  const r = Number(localStorage.getItem(KEY_RADIUS));
  return clampRadius(Number.isFinite(r) && r > 0 ? r : 500);
}
function clampRadius(r) {
  return Math.min(3000, Math.max(100, Math.round(r / 100) * 100));
}
function formatDistance(m) {
  return m >= 1000 ? `${(m / 1000).toFixed(m % 1000 === 0 ? 0 : 1)}km` : `${m}m`;
}
function walkMinutes(m) {
  return Math.max(1, Math.round(m / WALK_M_PER_MIN));
}
function radiusText(m) {
  return `${formatDistance(m)} · 걸어서 약 ${walkMinutes(m)}분`;
}
function isValidMenuName(name) {
  return typeof name === "string" && name.trim().length >= 1 && name.trim().length <= 15;
}
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k.startsWith("aria")) node.setAttribute("aria-" + k.slice(4).toLowerCase(), v);
    else node[k] = v;
  }
  for (const c of children) node.append(c); // 글자는 글자 그대로 들어간다 (HTML 실행 안 됨)
  return node;
}
function showScreen(name) {
  for (const [k, s] of Object.entries(screens)) s.classList.toggle("hidden", k !== name);
  window.scrollTo(0, 0);
}
function randomInt(n) {
  // 메뉴마다 같은 확률로 뽑는다
  const buf = new Uint32Array(1);
  const limit = Math.floor(0x100000000 / n) * n;
  let x;
  do { crypto.getRandomValues(buf); x = buf[0]; } while (x >= limit);
  return x % n;
}
function randomFloat() {
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return buf[0] / 0x100000000;
}

// ===========================================================================
// 검색 거리 (F4, F4-1, F4-2)
// ===========================================================================
function setRadius(r) {
  radius = clampRadius(r);
  slider.value = radius;
  radiusLabel.textContent = radiusText(radius);
  try { localStorage.setItem(KEY_RADIUS, String(radius)); } catch (_) { /* 무시 */ }
  renderQuick($("quick-start"), (r2) => setRadius(r2));
  renderQuick($("quick-menus"), (r2) => { setRadius(r2); if (coords) search(); });
}

function renderQuick(box, onPick) {
  box.replaceChildren(
    ...QUICK_RADII.map((r) =>
      el("button", {
        type: "button",
        class: "chip" + (r === radius ? " on" : ""),
        textContent: formatDistance(r),
        ariaPressed: String(r === radius),
        onclick: () => onPick(r),
      })
    )
  );
}

slider.addEventListener("input", () => setRadius(Number(slider.value)));

// ===========================================================================
// 위치 (F1, F2)
// ===========================================================================
$("locate-btn").addEventListener("click", () => {
  if (!("geolocation" in navigator) || !window.isSecureContext) {
    askAddress("이 브라우저에서는 위치를 쓸 수 없어요. 아래에 주소로 찾아 주세요.");
    return;
  }
  startStatus.textContent = "위치를 확인하는 중…";
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      coords = { lat: pos.coords.latitude, lng: pos.coords.longitude, label: "" };
      search();
    },
    (err) => {
      askAddress(
        err.code === err.PERMISSION_DENIED
          ? "위치 권한이 꺼져 있어요. 아래에 동네 이름이나 주소로 찾아 주세요."
          : "위치를 받지 못했어요. 아래에 동네 이름이나 주소로 찾아 주세요."
      );
    },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
  );
});

function askAddress(message) {
  startStatus.textContent = message;
  $("address-input").focus();
}

$("address-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = $("address-input").value.trim();
  if (!query) return;
  startStatus.textContent = "주소를 찾는 중…";
  try {
    const res = await fetch("/api/geocode?" + new URLSearchParams({ query }));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      startStatus.textContent = (body.error || "잠시 후 다시 시도해 주세요.");
      return;
    }
    coords = { lat: body.lat, lng: body.lng, label: body.name };
    search();
  } catch (_) {
    startStatus.textContent = "인터넷 연결을 확인하고 다시 시도해 주세요.";
  }
});

// ===========================================================================
// 음식점 찾기 (F3) — 마지막으로 고른 거리의 결과만 쓴다
// ===========================================================================
async function search() {
  const seq = ++requestSeq;
  if (currentAbort) currentAbort.abort();
  currentAbort = new AbortController();

  const onMenus = !screens.menus.classList.contains("hidden");
  const status = onMenus ? menusStatus : startStatus;
  status.replaceChildren(`${formatDistance(radius)} 안의 음식점을 찾는 중…`);
  if (onMenus) menusBody.classList.add("loading");

  const params = new URLSearchParams({ lat: coords.lat, lng: coords.lng, radius });
  let res, body;
  try {
    res = await fetch("/api/places?" + params, { signal: currentAbort.signal });
    body = await res.json().catch(() => ({}));
  } catch (err) {
    if (seq !== requestSeq) return; // 더 새 요청이 있으면 버린다
    showSearchError(status, "인터넷 연결을 확인하고 다시 시도해 주세요.");
    return;
  }
  if (seq !== requestSeq) return;
  menusBody.classList.remove("loading");

  if (!res.ok) {
    showSearchError(status, body.error || "잠시 후 다시 시도해 주세요.");
    return;
  }

  data = body;
  kindOff = new Set();
  sessionExcluded = new Set();
  startStatus.textContent = "";
  menusStatus.textContent = "";
  showScreen("menus");
  renderMenus();
}

function showSearchError(status, message) {
  menusBody.classList.remove("loading");
  status.replaceChildren(
    message + " ",
    el("button", { type: "button", class: "chip", textContent: "다시 찾기", onclick: search })
  );
}

// ===========================================================================
// 메뉴 고르기 화면 (F6, F7, F11)
// ===========================================================================
function visibleApiMenus() {
  if (!data) return [];
  return data.menus.filter((m) => m.kinds.some((k) => !kindOff.has(k)));
}

function renderMenus() {
  const label = coords.label || data.region || "내 위치";
  $("where").textContent = label;
  $("summary-line").textContent =
    `${radiusText(data.radius)} 안 · 음식점 ${data.places.length}곳`;
  renderQuick($("quick-menus"), (r) => { setRadius(r); search(); });
  $("limited-note").classList.toggle("hidden", !data.limited);

  // 음식점이 하나도 없을 때
  if (data.places.length === 0) {
    const next = WIDEN_STEPS.find((r) => r > data.radius);
    menusStatus.replaceChildren("이 거리 안에는 음식점이 없어요. 거리를 넓혀 보세요. ");
    if (next) {
      menusStatus.append(
        el("button", {
          type: "button",
          class: "chip on",
          textContent: `${formatDistance(next)}로 다시 찾기`,
          onclick: () => { setRadius(next); search(); },
        })
      );
    }
  }

  // 종류 칩
  const kinds = [...new Set(data.places.map((p) => p.kind))];
  $("kinds").replaceChildren(
    ...kinds.map((k) =>
      el("button", {
        type: "button",
        class: "chip" + (kindOff.has(k) ? "" : " on"),
        textContent: k,
        ariaPressed: String(!kindOff.has(k)),
        onclick: () => {
          kindOff.has(k) ? kindOff.delete(k) : kindOff.add(k);
          renderMenus();
        },
      })
    )
  );

  // 메뉴 목록
  const list = $("menu-list");
  const items = [];
  for (const name of customMenus) {
    items.push(menuRow(name, "직접 넣음", true));
  }
  for (const m of visibleApiMenus()) {
    if (customMenus.includes(m.name)) continue;
    items.push(menuRow(m.name, `${m.count}곳`, false));
  }
  list.replaceChildren(...items);
  updateMakeButton();
}

function menuRow(name, countText, isCustom) {
  const box = el("input", {
    type: "checkbox",
    checked: !offMenus.has(name),
    onchange: () => {
      box.checked ? offMenus.delete(name) : offMenus.add(name);
      saveJson(KEY_OFF, [...offMenus]);
      updateMakeButton();
    },
  });
  const row = el(
    "li",
    {},
    el("label", { class: "menu-item" }, box, el("span", { class: "menu-name", textContent: name }),
      el("span", { class: "count", textContent: countText }))
  );
  if (isCustom) {
    row.append(
      el("button", {
        type: "button",
        class: "remove",
        textContent: "✕",
        ariaLabel: `${name} 지우기`,
        onclick: () => {
          customMenus = customMenus.filter((n) => n !== name);
          offMenus.delete(name);
          saveJson(KEY_CUSTOM, customMenus);
          saveJson(KEY_OFF, [...offMenus]);
          renderMenus();
        },
      })
    );
  }
  return row;
}

$("custom-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("custom-input");
  const name = input.value.trim();
  const error = $("custom-error");
  error.textContent = "";
  if (!isValidMenuName(name)) {
    error.textContent = "메뉴 이름은 1~15자로 써 주세요.";
    return;
  }
  const exists = customMenus.includes(name) || (data && data.menus.some((m) => m.name === name));
  if (exists) {
    error.textContent = "이미 있는 메뉴예요.";
    return;
  }
  customMenus.push(name);
  offMenus.delete(name);
  saveJson(KEY_CUSTOM, customMenus);
  saveJson(KEY_OFF, [...offMenus]);
  input.value = "";
  renderMenus();
});

// 켜진 메뉴: 직접 넣은 메뉴 먼저, 그다음 음식점 수가 많은 순
function enabledMenus() {
  const names = customMenus.filter((n) => !offMenus.has(n));
  for (const m of visibleApiMenus()) {
    if (!offMenus.has(m.name) && !names.includes(m.name)) names.push(m.name);
  }
  return names;
}

function updateMakeButton() {
  const n = enabledMenus().length;
  const btn = $("make-wheel");
  btn.disabled = n === 0;
  btn.textContent = n === 0 ? "메뉴를 1개 이상 켜 주세요" : n === 1 ? "바로 정하기" : `돌림판 만들기 · ${n}칸`;
}

$("make-wheel").addEventListener("click", () => {
  sessionExcluded = new Set();
  openWheel();
});
$("back-start").addEventListener("click", () => {
  renderRecent();
  showScreen("start");
});

// ===========================================================================
// 돌림판 (F8, F10)
// ===========================================================================
function openWheel() {
  let names = enabledMenus().filter((n) => !sessionExcluded.has(n));
  if (names.length === 0) {
    // 빼고 나니 남는 게 없으면 다시 전체로
    sessionExcluded = new Set();
    names = enabledMenus();
  }
  if (names.length === 0) return;
  if (names.length === 1) {
    // 1개면 바로 그 메뉴가 결과다
    wheelItems = names;
    showResult(names[0]);
    return;
  }

  // 칸 수 제한 없음: 켜 둔 메뉴를 모두 넣는다
  const note = $("overflow-note");
  if (names.length > 30) {
    note.textContent = `메뉴 ${names.length}개를 모두 넣었어요. 칸이 좁으면 이름이 작게 보여요.`;
    note.classList.remove("hidden");
  } else {
    note.classList.add("hidden");
  }

  wheelItems = names;
  rotation = 0;
  showScreen("wheel");
  drawWheel();
  $("spin-btn").disabled = false;
}

// 칸 색: 색을 돌려 쓰되, 맞닿은 칸(마지막 칸과 첫 칸 포함)은 같은 색이 되지 않게
function sliceColor(i, n) {
  const k = SLICE_COLORS.length;
  if (i === n - 1 && n > 1 && (n - 1) % k === 0) return SLICE_COLORS[k / 2];
  return SLICE_COLORS[i % k];
}

// 정해진 길이에 들어가도록 글자를 줄인다 (넘치면 끝에 …)
function fitText(text, room) {
  if (ctx.measureText(text).width <= room) return text;
  let s = text;
  while (s.length > 1 && ctx.measureText(s + "…").width > room) s = s.slice(0, -1);
  return s + "…";
}

function drawWheel() {
  const n = wheelItems.length;
  const size = canvas.width;
  const c = size / 2;
  const r = c - 8;
  const seg = (Math.PI * 2) / n;
  const hub = size * 0.06;       // 가운데 동그라미 반지름
  const pad = size * 0.03;       // 바깥 테두리와 글자 사이
  // 글자 높이는 글자가 놓이는 자리(반지름 60%)의 칸 너비에 맞춘다
  const fontSize = Math.max(8, Math.min(size * 0.05, seg * r * 0.6 * 0.85));
  const textRoom = r - pad - hub - size * 0.02; // 글자가 들어갈 수 있는 길이
  ctx.clearRect(0, 0, size, size);

  for (let i = 0; i < n; i++) {
    const start = rotation + i * seg;
    ctx.beginPath();
    ctx.moveTo(c, c);
    ctx.arc(c, c, r, start, start + seg);
    ctx.closePath();
    ctx.fillStyle = sliceColor(i, n);
    ctx.fill();
    ctx.strokeStyle = "#fbfaf7";
    ctx.lineWidth = n > 60 ? 1 : n > 30 ? 2 : 3;
    ctx.stroke();

    // 메뉴 이름 — 칸이 좁을수록 글자를 작게, 칸 안에 들어가는 만큼만
    ctx.save();
    ctx.translate(c, c);
    const mid = start + seg / 2;
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#2a2621";
    ctx.font = `700 ${fontSize}px system-ui, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif`;
    const label = fitText(wheelItems[i], textRoom);
    if (Math.cos(mid) < 0) {
      // 왼쪽 절반은 글자가 뒤집히지 않게 반대로 돌려서 쓴다
      ctx.rotate(mid + Math.PI);
      ctx.textAlign = "left";
      ctx.fillText(label, -(r - pad), 0);
    } else {
      ctx.rotate(mid);
      ctx.textAlign = "right";
      ctx.fillText(label, r - pad, 0);
    }
    ctx.restore();
  }

  // 가운데 동그라미
  ctx.beginPath();
  ctx.arc(c, c, hub, 0, Math.PI * 2);
  ctx.fillStyle = "#1c1a17";
  ctx.fill();
  ctx.beginPath();
  ctx.arc(c, c, hub * 0.28, 0, Math.PI * 2);
  ctx.fillStyle = "#b08d57";
  ctx.fill();
}

// 화살표(맨 위, 각도 -90°)가 가리키는 칸 번호
function indexAtPointer(rot) {
  const n = wheelItems.length;
  const seg = (Math.PI * 2) / n;
  const pointer = -Math.PI / 2;
  let a = (pointer - rot) % (Math.PI * 2);
  if (a < 0) a += Math.PI * 2;
  return Math.floor(a / seg) % n;
}

function spin() {
  if (spinning || wheelItems.length < 2) return;
  spinning = true;
  $("spin-btn").disabled = true;
  $("back-menus").disabled = true;

  const n = wheelItems.length;
  const seg = (Math.PI * 2) / n;
  const target = randomInt(n); // 칸마다 같은 확률
  // 칸 안에서 멈추는 자리도 조금씩 다르게 (경계는 피한다)
  const inside = 0.15 + randomFloat() * 0.7;
  const pointer = -Math.PI / 2;
  const base = pointer - (target + inside) * seg; // 이 각도면 화살표가 target 칸을 가리킨다
  const turns = 5 + randomInt(3);
  let end = base;
  while (end < rotation + turns * Math.PI * 2) end += Math.PI * 2;

  const from = rotation;
  const t0 = performance.now();
  const ease = (t) => 1 - Math.pow(1 - t, 3);

  function frame(now) {
    const t = Math.min(1, (now - t0) / SPIN_MS);
    rotation = from + (end - from) * ease(t);
    drawWheel();
    if (t < 1) {
      requestAnimationFrame(frame);
      return;
    }
    rotation = end % (Math.PI * 2);
    drawWheel();
    spinning = false;
    $("back-menus").disabled = false;
    // 멈춘 자리에서 결과를 읽는다 → 화면과 결과가 항상 같다
    showResult(wheelItems[indexAtPointer(rotation)]);
  }
  requestAnimationFrame(frame);
}

$("spin-btn").addEventListener("click", spin);
$("back-menus").addEventListener("click", () => {
  if (spinning) return;
  renderMenus();
  showScreen("menus");
});

// ===========================================================================
// 결과 (F9, F10, F12, F13)
// ===========================================================================
function placesFor(menu) {
  if (!data) return [];
  return data.places
    .filter((p) => p.menus.includes(menu))
    .sort((a, b) => a.distance - b.distance);
}

function showResult(menu) {
  lastResult = menu;
  $("result-menu").textContent = menu;
  $("share-status").textContent = "";
  $("exclude-last").checked = false;
  $("exclude-last").disabled = enabledMenus().filter((n) => !sessionExcluded.has(n)).length <= 2;

  const list = $("result-places");
  const places = placesFor(menu);
  if (places.length === 0) {
    const url = "https://map.kakao.com/?q=" + encodeURIComponent(menu);
    list.replaceChildren(
      el("li", { class: "empty" }, "직접 넣은 메뉴라 근처 가게 목록이 없어요. ",
        el("a", { href: url, target: "_blank", rel: "noopener noreferrer", textContent: "카카오맵에서 찾아보기" }))
    );
  } else {
    list.replaceChildren(
      ...places.slice(0, 10).map((p) =>
        el(
          "li",
          {},
          el("div", { class: "place-info" },
            el("strong", { textContent: p.name }),
            el("span", { class: "sub", textContent: `${formatDistance(p.distance)} · 걸어서 약 ${walkMinutes(p.distance)}분 · ${p.category}` })),
          el("a", {
            class: "map-link",
            href: p.url,
            target: "_blank",
            rel: "noopener noreferrer",
            textContent: "지도에서 보기",
          })
        )
      )
    );
  }

  addRecent(menu);
  $("result").classList.remove("hidden");
  $("again-btn").focus();
}

function closeResult() {
  $("result").classList.add("hidden");
}

$("again-btn").addEventListener("click", () => {
  if ($("exclude-last").checked && lastResult) sessionExcluded.add(lastResult);
  closeResult();
  if ($("exclude-last").checked || wheelItems.length < 2) {
    openWheel(); // 메뉴 하나를 빼서 돌림판을 새로 만든다
    if (wheelItems.length >= 2) spin();
  } else {
    showScreen("wheel");
    spin();
  }
});

$("reselect-btn").addEventListener("click", () => {
  closeResult();
  renderMenus();
  showScreen("menus");
});

$("result").addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeResult();
});

// 최근 결과 5개
function addRecent(menu) {
  const list = loadJson(KEY_RECENT, []).filter((r) => r && typeof r.menu === "string");
  list.unshift({ menu, at: Date.now() });
  saveJson(KEY_RECENT, list.slice(0, 5));
}

const timeFormat = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

function renderRecent() {
  const list = loadJson(KEY_RECENT, []).filter((r) => r && typeof r.menu === "string").slice(0, 5);
  $("recent-box").classList.toggle("hidden", list.length === 0);
  $("recent-list").replaceChildren(
    ...list.map((r) =>
      el("li", {},
        el("span", { textContent: r.menu }),
        el("span", { class: "sub", textContent: timeFormat.format(new Date(r.at)) }))
    )
  );
}

// 공유
$("share-btn").addEventListener("click", async () => {
  const text = `🎉 오늘은 ${lastResult}! — 오늘 뭐 먹지? 돌림판으로 정했어요`;
  const url = location.origin + "/";
  const status = $("share-status");
  if (navigator.share) {
    try {
      await navigator.share({ title: "오늘 뭐 먹지?", text, url });
      return;
    } catch (err) {
      if (err && err.name === "AbortError") return; // 사용자가 닫음
    }
  }
  try {
    await navigator.clipboard.writeText(`${text}\n${url}`);
    status.textContent = "복사했어요. 친구에게 붙여 넣어 보내 주세요.";
  } catch (_) {
    status.textContent = "복사하지 못했어요. 주소창의 주소를 직접 보내 주세요.";
  }
});

// ===========================================================================
// 시작
// ===========================================================================
setRadius(radius);
renderRecent();
