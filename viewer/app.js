// 人狼ビューア — フロントエンド
// ポーリング + サイドバー描画 + シーン描画

(function () {
  "use strict";

  const POLL_INTERVAL = 3000;
  let lastHash = null;
  let loadedScenes = new Set();
  let charDescriptions = {};
  let userScrolledUp = false;

  // ── 役職アイコンマッピング ──
  // 各画像は左右2枚組。side: "left" = 左半分, "right" = 右半分
  var ROLE_ICON = {
    seer:      { file: "占い師_霊媒師.png", side: "left" },
    medium:    { file: "占い師_霊媒師.png", side: "right" },
    villager:  { file: "村人_狩人.png",     side: "left" },
    bodyguard: { file: "村人_狩人.png",     side: "right" },
    werewolf:  { file: "人狼_狂人.png",     side: "left" },
    madman:    { file: "人狼_狂人.png",     side: "right" },
  };
  // 陣営アイコン（ゲーム中の死者用: 具体的役職は秘匿）
  var ALIGNMENT_ICON = {
    "人狼": { file: "人狼_狂人.png", side: "left" },
    "人間": { file: "村人_狩人.png", side: "left" },
  };

  // ── DOM refs ──
  const $dayPhase = document.getElementById("day-phase");
  const $liveIndicator = document.getElementById("live-indicator");
  const $playerInfo = document.getElementById("player-info");
  const $aliveHeader = document.getElementById("alive-header");
  const $aliveList = document.getElementById("alive-list");
  const $deadHeader = document.getElementById("dead-header");
  const $deadList = document.getElementById("dead-list");
  const $privateList = document.getElementById("private-list");
  const $scenes = document.getElementById("scenes");
  const $main = document.getElementById("main");

  // ── Scroll tracking ──
  $main.addEventListener("scroll", function () {
    var threshold = 80;
    var atBottom = $main.scrollHeight - $main.scrollTop - $main.clientHeight < threshold;
    userScrolledUp = !atBottom;
  });

  function scrollToBottom() {
    if (!userScrolledUp) {
      $main.scrollTop = $main.scrollHeight;
    }
  }

  // ── Color generation from name ──
  function nameToColor(name) {
    var hash = 0;
    for (var i = 0; i < name.length; i++) {
      hash = name.charCodeAt(i) + ((hash << 5) - hash);
    }
    var hue = ((hash % 360) + 360) % 360;
    return "hsl(" + hue + ", 45%, 65%)";
  }

  // ── API helpers ──
  function fetchJSON(url) {
    return fetch(url).then(function (res) {
      if (!res.ok) throw new Error(res.status + " " + res.statusText);
      return res.json();
    });
  }

  // ── Role icon badge ──
  function roleIconHtml(iconInfo) {
    if (!iconInfo) return "";
    var url = "/chara_image/" + encodeURIComponent(iconInfo.file);
    var posClass = iconInfo.side === "right" ? "role-icon-right" : "role-icon-left";
    return '<span class="role-icon ' + posClass + '">' +
      '<img src="' + esc(url) + '" alt="" loading="lazy">' +
      "</span>";
  }

  // ── Sidebar rendering ──
  function renderSidebar(state) {
    // Header
    $dayPhase.textContent = "Day " + state.day + " / " + state.phase_jp;

    // Player info
    if (state.player) {
      var statusText = state.player.alive ? "" : '<div class="player-dead">★ 死亡</div>';
      var playerIcon = roleIconHtml(ROLE_ICON[state.player.role]);
      $playerInfo.innerHTML =
        '<div class="player-name">' + esc(state.player.name) + "</div>" +
        '<div class="player-role">' + playerIcon + " " + esc(state.player.role_jp) + "</div>" +
        statusText;
    } else {
      $playerInfo.textContent = "（未設定）";
    }

    // Alive
    $aliveHeader.textContent = "生存者 " + state.alive.length + "名";
    $aliveList.innerHTML = state.alive.map(function (p) {
      var title = charDescriptions[p.name] ? ' title="' + esc(charDescriptions[p.name]) + '"' : "";
      var icon = "";
      if (state.game_over && p.role) {
        icon = roleIconHtml(ROLE_ICON[p.role]);
      }
      return '<li' + title + '><span class="dot dot-alive"></span>' + icon + esc(p.name) + "</li>";
    }).join("");

    // Deaths
    if (state.deaths.length > 0) {
      $deadHeader.textContent = "死亡者 " + state.deaths.length + "名";
      $deadList.innerHTML = state.deaths.map(function (d) {
        var icon = "";
        if (state.game_over && d.role) {
          // ゲーム終了後のみ: 具体的な役職アイコン
          icon = roleIconHtml(ROLE_ICON[d.role]);
        }
        var causeLabel = (d.alignment ? esc(d.alignment) + " " : "") + "D" + d.day + esc(d.cause);
        return '<li><span class="dot dot-dead"></span>' + icon +
          esc(d.name) +
          '<span class="death-cause">' + causeLabel + "</span></li>";
      }).join("");
    } else {
      $deadHeader.textContent = "死亡者";
      $deadList.innerHTML = '<li class="no-info">なし</li>';
    }

    // Private info
    if (state.private_info.length > 0) {
      $privateList.innerHTML = state.private_info.map(function (line) {
        return "<li>" + esc(line) + "</li>";
      }).join("");
    } else {
      $privateList.innerHTML = '<li class="no-info">なし</li>';
    }
  }

  // ── Scene parsing ──
  // Patterns:
  //   名前「セリフ」 → speech
  //   ――で始まる行 → separator
  //   その他 → narration
  function parseScene(text) {
    var lines = text.split("\n");
    var blocks = [];

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.trim() === "") continue;

      // セリフ: 名前「...」 or 名前「...
      var speechMatch = line.match(/^(\S+?)「(.+)$/);
      if (speechMatch) {
        var name = speechMatch[1];
        var content = speechMatch[2];
        // 閉じ括弧を除去
        if (content.endsWith("」")) {
          content = content.slice(0, -1);
        }
        blocks.push({ type: "speech", name: name, text: content });
        continue;
      }

      // シーン区切り: ――で始まる
      if (line.startsWith("――")) {
        blocks.push({ type: "separator", text: line });
        continue;
      }

      // ナレーション
      blocks.push({ type: "narration", text: line });
    }

    return blocks;
  }

  function avatarHtml(name) {
    var imgUrl = "/chara_image/" + encodeURIComponent(name) + ".png";
    return '<div class="speech-avatar"><img src="' + esc(imgUrl) + '" alt="" loading="lazy"></div>';
  }

  function renderSceneBlocks(blocks) {
    var html = "";
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (b.type === "speech") {
        var color = nameToColor(b.name);
        var title = charDescriptions[b.name] ? ' title="' + esc(charDescriptions[b.name]) + '"' : "";
        html += '<div class="line-speech">' +
          '<div class="speech-avatar-col"' + title + ">" +
          avatarHtml(b.name) +
          '<div class="speech-name" style="color:' + color + '">' + esc(b.name) + "</div>" +
          "</div>" +
          '<div class="speech-bubble">' + esc(b.text) + "</div>" +
          "</div>";
      } else if (b.type === "separator") {
        html += '<div class="line-separator">' + esc(b.text) + "</div>";
      } else {
        html += '<div class="line-narration">' + esc(b.text) + "</div>";
      }
    }
    return html;
  }

  // ── Scene loading ──
  function loadScenes(sceneList) {
    var newScenes = sceneList.filter(function (name) {
      return !loadedScenes.has(name);
    });

    if (newScenes.length === 0) return;

    var promises = newScenes.map(function (name) {
      return fetchJSON("/api/scene/" + encodeURIComponent(name)).then(function (data) {
        return { name: data.name, content: data.content };
      });
    });

    Promise.all(promises).then(function (results) {
      results.forEach(function (scene) {
        if (loadedScenes.has(scene.name)) return;
        loadedScenes.add(scene.name);

        var blocks = parseScene(scene.content);
        var div = document.createElement("div");
        div.className = "scene-block";
        div.dataset.scene = scene.name;
        div.innerHTML = renderSceneBlocks(blocks);
        $scenes.appendChild(div);
      });
      scrollToBottom();
    }).catch(function (err) {
      console.error("Scene load error:", err);
    });
  }

  // ── HTML escape ──
  function esc(str) {
    var d = document.createElement("div");
    d.appendChild(document.createTextNode(str));
    return d.innerHTML;
  }

  // ── Polling loop ──
  function poll() {
    fetchJSON("/api/hash")
      .then(function (data) {
        $liveIndicator.classList.remove("disconnected");
        if (data.hash !== lastHash) {
          lastHash = data.hash;
          return update();
        }
      })
      .catch(function () {
        $liveIndicator.classList.add("disconnected");
      })
      .finally(function () {
        setTimeout(poll, POLL_INTERVAL);
      });
  }

  function update() {
    return Promise.all([
      fetchJSON("/api/state"),
      fetchJSON("/api/scenes"),
    ]).then(function (results) {
      renderSidebar(results[0]);
      loadScenes(results[1]);
    });
  }

  // ── Init ──
  function init() {
    // Load character descriptions first, then start polling
    fetchJSON("/api/characters")
      .then(function (data) { charDescriptions = data; })
      .catch(function () { /* ok */ })
      .finally(function () {
        // Initial load
        update().catch(function () {});
        // Start polling
        setTimeout(poll, POLL_INTERVAL);
      });
  }

  init();
})();
