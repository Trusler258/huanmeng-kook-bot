/* ============================================================
   幻梦控制台 · 前端逻辑
   WebSocket 连接 + 日志渲染 + 统计 + 火花图
   ============================================================ */
(function(){
  'use strict';

  /* ---- 工具 ---- */
  var $ = function(id){ return document.getElementById(id); };

  /* ---- 元素 ---- */
  var stream      = $('stream'),
      connLed     = $('conn-led'),
      connText    = $('conn-text'),
      rateEl      = $('rate'),
      totalEl     = $('total'),
      cntInfo     = $('cnt-info'),
      cntWarn     = $('cnt-warn'),
      cntError    = $('cnt-error'),
      uptimeEl    = $('uptime'),
      clockEl     = $('clock'),
      sparkCanvas = $('spark'),
      searchInput = $('search'),
      chkScroll   = $('chk-autoscroll'),
      chkTs       = $('chk-timestamp'),
      btnClear    = $('btn-clear');

  /* ---- 状态 ---- */
  var ws = null,
      total = 0,
      counts = { info: 0, warn: 0, error: 0 },
      rateWindow = [],
      sparkData = new Array(60).fill(0),
      startTime = Date.now(),
      filterLevel = 'all',
      filterText = '',
      MAX_LINES = 500;

  /* ---- 冻结检查（guard.js 注入） ---- */
  function isFrozen(){ return false; }

  /* ================= 连接状态 ================= */
  function setConn(state, text){
    connText.textContent = text;
    connLed.className = 'led';
    if(state === 'ok')   connLed.classList.add('led-green');
    if(state === 'wait') connLed.classList.add('led-amber', 'blink');
    if(state === 'err')  connLed.classList.add('led-red');
  }

  /* ================= WebSocket ================= */
  function connectWS(){
    if(isFrozen()) return;

    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    var url = proto + '://' + location.host + '/ws';

    setConn('wait', '连接中…');
    ws = new WebSocket(url);
    window.__ws = ws;  /* guard.js 冻结时切断 */

    ws.onopen = function(){
      if(isFrozen()) return;
      setConn('ok', '已连接');
      $('spark').classList.remove('stale');
    };

    ws.onmessage = function(e){
      if(isFrozen()) return;
      try{
        var data = JSON.parse(e.data);
        handleLog(data);
      }catch(err){
        handleLog({ level: 'info', msg: e.data, ts: Date.now() });
      }
    };

    ws.onclose = function(){
      /* ★ 冻结检查必须在第一行，否则用户会看到"断开"提示 */
      if(isFrozen()) return;
      setConn('err', '断开');
      $('spark').classList.add('stale');
      setTimeout(connectWS, 3000);
    };

    ws.onerror = function(){
      if(isFrozen()) return;
      ws.close();
    };
  }

  /* ================= 日志处理 ================= */
  function handleLog(data){
    var level = (data.lv || data.level || 'info').toLowerCase();
    if(level !== 'info' && level !== 'warn' && level !== 'error' && level !== 'warning' && level !== 'debug') level = 'info';
    if(level === 'warning') level = 'warn';

    total++;
    counts[level]++;
    rateWindow.push(Date.now());

    /* 更新统计 */
    totalEl.textContent = total.toLocaleString();
    cntInfo.textContent = counts.info;
    cntWarn.textContent = counts.warn;
    cntError.textContent = counts.error;

    /* 渲染 */
    appendLine(level, data.msg || '', data.time);
  }

  function appendLine(level, msg, timeStr){
    var line = document.createElement('div');
    line.className = 'log-line lv-' + level;
    line.dataset.level = level;
    line.dataset.msg = msg.toLowerCase();

    var tsStr = '';
    if(chkTs.checked){
      tsStr = timeStr || new Date().toTimeString().slice(0, 8);
    }

    line.innerHTML =
      (tsStr ? '<span class="log-ts">' + tsStr + '</span>' : '') +
      '<span class="log-level">' + level.toUpperCase() + '</span>' +
      '<span class="log-msg">' + esc(msg) + '</span>';

    /* 应用当前过滤 */
    applyFilterToLine(line);

    stream.appendChild(line);

    /* 限制行数 */
    while(stream.children.length > MAX_LINES){
      stream.removeChild(stream.firstChild);
    }

    /* 自动滚动 */
    if(chkScroll.checked){
      stream.scrollTop = stream.scrollHeight;
    }
  }

  /* ================= 过滤 ================= */
  function applyFilterToLine(line){
    var lvOk = (filterLevel === 'all') || (line.dataset.level === filterLevel);
    var txOk = !filterText || (line.dataset.msg.indexOf(filterText) > -1);
    line.classList.toggle('hidden', !(lvOk && txOk));
  }

  function applyFilterAll(){
    var lines = stream.children;
    for(var i = 0; i < lines.length; i++){
      applyFilterToLine(lines[i]);
    }
  }

  /* 级别按钮 */
  var fbtns = document.querySelectorAll('.fbtn');
  fbtns.forEach(function(btn){
    btn.addEventListener('click', function(){
      fbtns.forEach(function(b){ b.classList.remove('active'); });
      btn.classList.add('active');
      filterLevel = btn.dataset.level;
      applyFilterAll();
    });
  });

  /* 搜索框 */
  searchInput.addEventListener('input', function(){
    filterText = searchInput.value.trim().toLowerCase();
    applyFilterAll();
  });

  /* 清空 */
  btnClear.addEventListener('click', function(){
    stream.innerHTML = '';
    total = 0;
    counts = { info: 0, warn: 0, error: 0 };
    totalEl.textContent = '0';
    cntInfo.textContent = '0';
    cntWarn.textContent = '0';
    cntError.textContent = '0';
  });

  /* ================= 速率 + 火花图 ================= */
  function updateRate(){
    var now = Date.now();
    rateWindow = rateWindow.filter(function(t){ return now - t < 1000; });
    var rate = rateWindow.length;
    rateEl.textContent = rate;

    sparkData.push(rate);
    sparkData.shift();
    drawSpark();
  }
  setInterval(updateRate, 1000);

  function drawSpark(){
    var ctx = sparkCanvas.getContext('2d');
    var w = sparkCanvas.width, h = sparkCanvas.height;
    var max = Math.max.apply(null, sparkData) || 1;

    ctx.clearRect(0, 0, w, h);

    /* 填充区域 */
    ctx.beginPath();
    ctx.moveTo(0, h);
    for(var i = 0; i < sparkData.length; i++){
      var x = (i / (sparkData.length - 1)) * w;
      var y = h - (sparkData[i] / max) * (h - 4);
      ctx.lineTo(x, y);
    }
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = 'rgba(240,168,48,.08)';
    ctx.fill();

    /* 线条 */
    ctx.beginPath();
    for(var j = 0; j < sparkData.length; j++){
      var x2 = (j / (sparkData.length - 1)) * w;
      var y2 = h - (sparkData[j] / max) * (h - 4);
      if(j === 0) ctx.moveTo(x2, y2);
      else ctx.lineTo(x2, y2);
    }
    ctx.strokeStyle = '#f0a830';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    /* 末端亮点 */
    var lastX = w, lastY = h - (sparkData[sparkData.length-1] / max) * (h - 4);
    ctx.beginPath();
    ctx.arc(lastX - 1, lastY, 2.5, 0, Math.PI * 2);
    ctx.fillStyle = '#f0a830';
    ctx.fill();
  }

  /* ================= 时钟 + 运行时间 ================= */
  function tick(){
    var now = new Date();
    clockEl.textContent = pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());

    var elapsed = Math.floor((Date.now() - startTime) / 1000);
    var hh = Math.floor(elapsed / 3600);
    var mm = Math.floor((elapsed % 3600) / 60);
    var ss = elapsed % 60;
    uptimeEl.textContent = pad(hh) + ':' + pad(mm) + ':' + pad(ss);
  }
  setInterval(tick, 1000);
  tick();

  /* ================= 工具函数 ================= */
  function pad(n){ return n < 10 ? '0' + n : '' + n; }
  function esc(s){
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  /* ================= 启动 ================= */
  connectWS();

})();