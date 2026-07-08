// 人狼ビューア — フロントエンド
// ポーリング + サイドバー描画 + シーン描画 + タイピングインジケータ

(function () {
  "use strict";

  const POLL_INTERVAL_NORMAL = 3000;
  const POLL_INTERVAL_TYPING = 1000;  // 生成中は高速ポーリング

  let lastHash = null;
  let loadedScenes = new Set();
  let sceneDivs = {};          // scene name → div element（差分更新用）
  let charDescriptions = {};
  let userScrolledUp = false;
  let isTyping = false;        // 現在 NPC 生成中かどうか

  // ── 役職アイコンマッピング ──
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
  const $actionStatus = document.getElementById("action-status");
  const $actionControls = document.getElementById("action-controls");

  let actionPending = false;   // POST 送信中
  let lastUiKey = null;        // アクションUIの再構築判定用

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

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.error || (res.status + " " + res.statusText));
        return data;
      });
    });
  }

  // ── Action bar ──
  function setStatus(text, isError) {
    $actionStatus.textContent = text || "";
    $actionStatus.className = isError ? "error" : "";
  }

  function sendAction(url, body, pendingText) {
    if (actionPending) return;
    actionPending = true;
    setStatus(pendingText || "処理中…（NPCが考えています）");
    setControlsDisabled(true);
    isTyping = true;

    postJSON(url, body)
      .then(function (data) {
        setStatus("");
        if (data.seer_result) {
          var r = data.seer_result.result === "werewolf" ? "人狼" : "人狼ではない";
          setStatus("占い結果: " + data.seer_result.target + " は " + r);
        }
      })
      .catch(function (err) {
        setStatus(err.message, true);
      })
      .finally(function () {
        actionPending = false;
        lastUiKey = null;  // UI再構築を強制
        update().catch(function () {});
      });
  }

  function setControlsDisabled(disabled) {
    var elems = $actionControls.querySelectorAll("input, select, button");
    for (var i = 0; i < elems.length; i++) elems[i].disabled = disabled;
  }

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text) e.textContent = text;
    return e;
  }

  function makeSelect(options, placeholder) {
    var sel = el("select");
    if (placeholder) {
      var ph = el("option", { value: "" }, placeholder);
      ph.disabled = true;
      ph.selected = true;
      sel.appendChild(ph);
    }
    options.forEach(function (name) {
      sel.appendChild(el("option", { value: name }, name));
    });
    return sel;
  }

  function makeButton(label, onClick, secondary) {
    var btn = el("button", { class: "action-btn" + (secondary ? " secondary" : "") }, label);
    btn.addEventListener("click", onClick);
    return btn;
  }

  function renderActionBar(state) {
    var ui = state.ui || { mode: "unknown" };
    var busy = state.busy || actionPending;
    // busy 中は既存UIを無効化するだけで再構築しない（入力内容を保持）
    var uiKey = ui.mode + ":" + (ui.need || "") + ":" + state.day + ":" + state.phase;
    if (uiKey === lastUiKey) {
      setControlsDisabled(busy);
      return;
    }
    lastUiKey = uiKey;
    $actionControls.innerHTML = "";

    if (ui.mode === "setup") {
      var charSel = makeSelect(ui.characters || [], "キャラクターを選択");
      var startBtn = makeButton("新しいゲームを開始", function () {
        if (!charSel.value) { setStatus("キャラクターを選んでください", true); return; }
        sendAction("/api/new_game", { player: charSel.value }, "ゲームを準備しています…");
      });
      $actionControls.appendChild(charSel);
      $actionControls.appendChild(startBtn);
      setStatus("プレイするキャラクターを選んでゲームを開始してください");

    } else if (ui.mode === "discussion") {
      if (ui.can_speak) {
        var input = el("input", { type: "text", placeholder: "発言する（Enterで送信）" });
        var sayBtn = makeButton("発言", function () { submitSay(); });
        var voteSel = makeSelect(ui.vote_candidates || [], "投票先…");
        var voteBtn = makeButton("投票へ", function () {
          if (!voteSel.value) { setStatus("投票先を選んでください", true); return; }
          sendAction("/api/vote", { target: voteSel.value }, "投票を集計しています…");
        }, true);

        function submitSay() {
          var msg = input.value.trim();
          if (!msg) { setStatus("発言内容を入力してください", true); return; }
          input.value = "";
          sendAction("/api/say", { message: msg }, "NPCが応答しています…");
        }
        input.addEventListener("keydown", function (e) {
          if (e.key === "Enter" && !e.isComposing) submitSay();
        });

        $actionControls.appendChild(input);
        $actionControls.appendChild(sayBtn);
        $actionControls.appendChild(voteSel);
        $actionControls.appendChild(voteBtn);
        setStatus("");
      } else {
        var contBtn = makeButton("議論を見守る（次のラウンドへ）", function () {
          sendAction("/api/continue", {}, "NPCが議論しています…");
        });
        var skipBtn = makeButton("投票へ進む", function () {
          sendAction("/api/vote", {}, "投票を集計しています…");
        }, true);
        $actionControls.appendChild(contBtn);
        $actionControls.appendChild(skipBtn);
        setStatus("あなたは死亡しています。議論を見守りましょう");
      }

    } else if (ui.mode === "vote") {
      if (ui.can_vote) {
        var vSel = makeSelect(ui.vote_candidates || [], "投票先…");
        var vBtn = makeButton("投票する", function () {
          if (!vSel.value) { setStatus("投票先を選んでください", true); return; }
          sendAction("/api/vote", { target: vSel.value }, "投票を集計しています…");
        });
        $actionControls.appendChild(vSel);
        $actionControls.appendChild(vBtn);
      } else {
        $actionControls.appendChild(makeButton("開票する", function () {
          sendAction("/api/vote", {}, "投票を集計しています…");
        }));
      }
      setStatus("投票フェーズです");

    } else if (ui.mode === "night") {
      var labels = { seer: "占う相手", guard: "護衛する相手", attack: "襲撃する相手" };
      if (ui.need) {
        var nSel = makeSelect(ui.candidates || [], labels[ui.need] + "…");
        var nBtn = makeButton("決定", function () {
          if (!nSel.value) { setStatus("対象を選んでください", true); return; }
          var body = {};
          body[ui.need] = nSel.value;
          sendAction("/api/night_action", body, "夜が更けていきます…");
        });
        $actionControls.appendChild(el("span", { class: "action-note" },
          "夜になりました。" + labels[ui.need] + "を選んでください"));
        $actionControls.appendChild(nSel);
        $actionControls.appendChild(nBtn);
        setStatus("");
      } else {
        $actionControls.appendChild(makeButton("夜を明かす", function () {
          sendAction("/api/night_action", {}, "夜が更けていきます…");
        }));
        setStatus("夜になりました。あなたにできることはありません");
      }

    } else if (ui.mode === "epilogue_pending") {
      $actionControls.appendChild(el("span", { class: "action-note" },
        "決着がつきました。エピローグを生成しています…"));

    } else if (ui.mode === "game_over") {
      $actionControls.appendChild(el("span", { class: "action-note" }, "ゲーム終了"));
      $actionControls.appendChild(makeButton("新しいゲームを始める", function () {
        lastUiKey = null;
        renderSetupForRestart();
      }, true));
      setStatus("全役職が公開されました。お疲れさまでした");
    } else {
      setStatus("");
    }

    setControlsDisabled(busy);
  }

  function renderSetupForRestart() {
    $actionControls.innerHTML = "";
    fetchJSON("/api/characters").then(function (chars) {
      var names = Object.keys(chars);
      var charSel = makeSelect(names, "キャラクターを選択");
      var startBtn = makeButton("新しいゲームを開始", function () {
        if (!charSel.value) { setStatus("キャラクターを選んでください", true); return; }
        // 旧シーン表示をクリア
        loadedScenes = new Set();
        sceneDivs = {};
        $scenes.innerHTML = "";
        sendAction("/api/new_game", { player: charSel.value }, "ゲームを準備しています…");
      });
      $actionControls.appendChild(charSel);
      $actionControls.appendChild(startBtn);
      setStatus("プレイするキャラクターを選んでください");
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
        // 「名前（役職）」形式で生成された場合に備え、括弧部分を除いた基本名でアバター・色を解決する
        var baseName = b.name.replace(/（[^）]*）$/, "");
        var color = nameToColor(baseName);
        var title = charDescriptions[baseName] ? ' title="' + esc(charDescriptions[baseName]) + '"' : "";
        html += '<div class="line-speech">' +
          '<div class="speech-avatar-col"' + title + ">" +
          avatarHtml(baseName) +
          '<div class="speech-name" style="color:' + color + '">' + esc(baseName) + "</div>" +
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

  // ── タイピングインジケータ ──
  function renderTypingIndicator(typingData) {
    var existing = document.getElementById("typing-indicator");

    if (!typingData || !typingData.npc) {
      if (existing) existing.remove();
      return;
    }

    if (!existing) {
      existing = document.createElement("div");
      existing.id = "typing-indicator";
    }
    // 常に末尾に移動（appendChild は既存要素を移動する）
    $scenes.appendChild(existing);

    existing.className = "typing-indicator";
    var baseName = typingData.npc.replace(/（[^）]*）$/, "");
    var color = nameToColor(baseName);
    var imgUrl = "/chara_image/" + encodeURIComponent(baseName) + ".png";
    existing.innerHTML =
      '<div class="speech-avatar-col">' +
        '<div class="speech-avatar"><img src="' + esc(imgUrl) + '" alt="" loading="lazy"></div>' +
        '<div class="speech-name" style="color:' + color + '">' + esc(baseName) + "</div>" +
      "</div>" +
      '<div class="typing-dots"><span></span><span></span><span></span></div>';

    scrollToBottom();
  }

  // ── Scene loading（差分更新対応）──
  function loadScenes(sceneList, activeScene) {
    // サーバー側で削除されたシーン（新規ゲーム開始時）をDOMから除去
    Object.keys(sceneDivs).forEach(function (name) {
      if (sceneList.indexOf(name) === -1) {
        sceneDivs[name].remove();
        delete sceneDivs[name];
        loadedScenes.delete(name);
      }
    });

    // 未ロードのシーン ＋ アクティブ（生成中）シーンを再取得対象にする
    var newScenes = sceneList.filter(function (name) {
      return !loadedScenes.has(name);
    });
    var toRefresh = (activeScene && loadedScenes.has(activeScene)) ? [activeScene] : [];
    var toFetch = newScenes.slice();
    toRefresh.forEach(function (n) {
      if (toFetch.indexOf(n) === -1) toFetch.push(n);
    });

    if (toFetch.length === 0) return Promise.resolve();

    var promises = toFetch.map(function (name) {
      return fetchJSON("/api/scene/" + encodeURIComponent(name)).then(function (data) {
        return { name: data.name, content: data.content };
      });
    });

    return Promise.all(promises).then(function (results) {
      results.forEach(function (scene) {
        var blocks = parseScene(scene.content);
        var html = renderSceneBlocks(blocks);

        if (sceneDivs[scene.name]) {
          // 既存 div を差分更新（生成途中シーンの追記に対応）
          sceneDivs[scene.name].innerHTML = html;
        } else {
          var div = document.createElement("div");
          div.className = "scene-block";
          div.dataset.scene = scene.name;
          div.innerHTML = html;
          $scenes.appendChild(div);
          sceneDivs[scene.name] = div;
        }
        loadedScenes.add(scene.name);
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
        // 生成中は高速ポーリング
        setTimeout(poll, isTyping ? POLL_INTERVAL_TYPING : POLL_INTERVAL_NORMAL);
      });
  }

  function update() {
    return Promise.all([
      fetchJSON("/api/state"),
      fetchJSON("/api/scenes"),
      fetchJSON("/api/typing").catch(function () { return null; }),
    ]).then(function (results) {
      var typingData = results[2];
      isTyping = !!(typingData && (typingData.npc || typingData.busy)) || actionPending;
      var activeScene = (typingData && typingData.scene) ? typingData.scene : null;

      renderSidebar(results[0]);
      renderActionBar(results[0]);
      // シーン更新が完了してからタイピングインジケータを末尾に表示
      return loadScenes(results[1], activeScene).then(function () {
        renderTypingIndicator(typingData);
      });
    });
  }

  // ── Init ──
  function init() {
    var $newGameBtn = document.getElementById("new-game-btn");
    if ($newGameBtn) {
      $newGameBtn.addEventListener("click", function () {
        if (actionPending) return;
        if (!confirm("進行中のゲームを破棄して新規ゲームを始めますか？")) return;
        lastUiKey = null;
        renderSetupForRestart();
      });
    }

    fetchJSON("/api/characters")
      .then(function (data) { charDescriptions = data; })
      .catch(function () { /* ok */ })
      .finally(function () {
        update().catch(function () {});
        setTimeout(poll, POLL_INTERVAL_NORMAL);
      });
  }

  init();
})();
