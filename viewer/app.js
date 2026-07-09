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
  const $voteList = document.getElementById("vote-list");
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

  // POST 完了後に必ず再取得するアクション
  var SLOW_ACTIONS = {
    "/api/say": false,
    "/api/npc_speak": true,
    "/api/continue": true,
    "/api/vote": true,
    "/api/night_action": true,
    "/api/new_game": true,
  };

  function invalidateScenesFromResponse(data) {
    if (!data) return;
    if (data.scene) loadedScenes.delete(data.scene);
    if (Array.isArray(data.scenes)) {
      data.scenes.forEach(function (name) { loadedScenes.delete(name); });
    }
  }

  function sendAction(url, body, pendingText) {
    if (actionPending) return;
    actionPending = true;
    setStatus(pendingText || "処理中…（NPCが考えています）");
    setControlsDisabled(true);
    if (SLOW_ACTIONS[url] !== false) {
      isTyping = true;
    }

    postJSON(url, body)
      .then(function (data) {
        invalidateScenesFromResponse(data);
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
    var uiKey = ui.mode + ":" + (ui.need || "") + ":" + state.day + ":" + state.phase
      + ":" + (ui.npc_queue_remaining || 0) + ":" + (ui.player_co || "");
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
        var input = el("input", { type: "text", placeholder: "発言内容を入力…" });
        var sayBtn = makeButton("発言する", function () { submitSay(); });
        var npcBtn = makeButton("NPCに話させる", function () {
          sendAction("/api/npc_speak", { mode: "one" }, "NPCが考えています…");
        }, true);
        var npcAllBtn = makeButton("全員発言", function () {
          sendAction("/api/npc_speak", { mode: "all" }, "NPCが議論しています…");
        }, true);
        var voteSel = makeSelect(ui.vote_candidates || [], "投票先…");
        var voteBtn = makeButton("投票へ", function () {
          if (!voteSel.value) { setStatus("投票先を選んでください", true); return; }
          sendAction("/api/vote", { target: voteSel.value }, "投票を集計しています…");
        }, true);

        function submitSay() {
          var msg = input.value.trim();
          if (!msg) { setStatus("発言内容を入力してください", true); return; }
          input.value = "";
          sendAction("/api/say", { message: msg }, "発言を記録しました");
        }
        input.addEventListener("keydown", function (e) {
          if (e.key === "Enter" && !e.isComposing) submitSay();
        });

        // CO宣言プルダウン（未COのときだけ表示。テンプレ文はサーバーが生成）
        var CO_LABELS = { seer: "占い師", medium: "霊媒師", bodyguard: "狩人" };
        var coSel = null;
        if ((ui.co_options || []).length && !ui.player_co) {
          coSel = el("select");
          var coPh = el("option", { value: "" }, "CO宣言…");
          coPh.disabled = true;
          coPh.selected = true;
          coSel.appendChild(coPh);
          ui.co_options.forEach(function (role) {
            coSel.appendChild(el("option", { value: role }, CO_LABELS[role] || role));
          });
          var coBtn = makeButton("COする", function () {
            if (!coSel.value) { setStatus("CO する役職を選んでください", true); return; }
            var msg = input.value.trim();
            input.value = "";
            sendAction("/api/say", { message: msg, co: coSel.value },
              "CO を宣言しました");
          }, true);
        }

        // 結果発表プルダウン（占い師/霊媒師でCO済みのときだけ表示）
        var annTargetSel = null;
        if (ui.can_announce_result) {
          annTargetSel = makeSelect(ui.announce_candidates || [], "発表対象…");
          var annResultSel = el("select");
          var annPh = el("option", { value: "" }, "結果…");
          annPh.disabled = true;
          annPh.selected = true;
          annResultSel.appendChild(annPh);
          annResultSel.appendChild(el("option", { value: "white" }, "白（人間）"));
          annResultSel.appendChild(el("option", { value: "black" }, "黒（人狼）"));
          var annBtn = makeButton("結果発表", function () {
            if (!annTargetSel.value) { setStatus("発表対象を選んでください", true); return; }
            if (!annResultSel.value) { setStatus("結果（白/黒）を選んでください", true); return; }
            var msg = input.value.trim();
            input.value = "";
            sendAction("/api/say", {
              message: msg,
              result_target: annTargetSel.value,
              result: annResultSel.value,
            }, "結果を発表しました");
          }, true);
        }

        npcBtn.disabled = !ui.can_npc_speak;
        $actionControls.appendChild(input);
        $actionControls.appendChild(sayBtn);
        if (coSel) {
          $actionControls.appendChild(coSel);
          $actionControls.appendChild(coBtn);
        }
        if (annTargetSel) {
          $actionControls.appendChild(annTargetSel);
          $actionControls.appendChild(annResultSel);
          $actionControls.appendChild(annBtn);
        }
        $actionControls.appendChild(npcBtn);
        $actionControls.appendChild(npcAllBtn);
        $actionControls.appendChild(voteSel);
        $actionControls.appendChild(voteBtn);
        var q = ui.npc_queue_remaining || 0;
        if (q > 0) {
          setStatus("NPCの発言待ち: 残り " + q + " 人");
        } else if (ui.can_start_new_disc === false) {
          setStatus("本日の議論は十分です。投票へ進んでください");
        } else {
          setStatus("発言するか、NPCに話させるか、投票へ進んでください");
        }
      } else {
        var contBtn = makeButton("NPC全員の発言を見る", function () {
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

    // Vote history
    var votes = state.execution_history || [];
    if (votes.length > 0) {
      $voteList.innerHTML = votes.map(function (v) {
        var breakdown = Object.keys(v.votes || {}).map(function (voter) {
          return esc(voter) + "→" + esc(v.votes[voter]);
        }).join(" / ");
        return "<li><strong>Day" + v.day + "</strong> " + esc(v.target) +
          " 処刑<span class=\"death-cause\">" + breakdown + "</span></li>";
      }).join("");
    } else {
      $voteList.innerHTML = '<li class="no-info">なし</li>';
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
  function loadScenes(sceneList, activeScene, discussionScene) {
    // サーバー側で削除されたシーン（新規ゲーム開始時）をDOMから除去
    Object.keys(sceneDivs).forEach(function (name) {
      if (sceneList.indexOf(name) === -1) {
        sceneDivs[name].remove();
        delete sceneDivs[name];
        loadedScenes.delete(name);
      }
    });

    // 未ロードのシーン ＋ 追記中の議論シーン ＋ 生成中シーンを再取得
    var newScenes = sceneList.filter(function (name) {
      return !loadedScenes.has(name);
    });
    var toRefresh = [];
    [activeScene, discussionScene].forEach(function (name) {
      if (name && loadedScenes.has(name) && toRefresh.indexOf(name) === -1) {
        toRefresh.push(name);
      }
    });
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
      var discussionScene = results[0].discussion_scene || null;
      // シーン更新が完了してからタイピングインジケータを末尾に表示
      return loadScenes(results[1], activeScene, discussionScene).then(function () {
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
