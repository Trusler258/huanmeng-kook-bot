/* 幻梦控制台 · 防护（无窗口检测，DOM getter 为主） */
(function(){
  'use strict';

  var GRACE = 3, PATROL = 1000, STARTUP_DELAY = 3000;
  var WARN = document.getElementById('devwarn'),
      DW_BAR = document.getElementById('dw-progress'),
      DW_N = null, DW_MSG = document.getElementById('dw-msg'),
      warning = false, locked = false,
      warnTimer = null, barTimer = null, startTime = Date.now();

  if(location.href.indexOf('blocked=1') > -1) return;
  if(location.protocol === 'about:') return;  /* reader view */

  function block(){ if(locked) return; locked = true; hideWarn(); location.replace('/?blocked=1'); }

  function showWarn(){
    if(warning || locked) return;
    warning = true;
    if(!WARN){
      WARN = document.createElement('div'); WARN.id = 'devwarn'; WARN.className = 'devwarn';
      WARN.innerHTML =
        '<div class="devwarn-in"><span class="dw-ico">⚠</span>' +
        '<div class="dw-txt"><b>检测到开发者工具</b>' +
        '<span id="dw-msg">页面将在 <i id="dw-n">' + GRACE + '</i> 秒后禁止访问，关闭可撤销</span></div>' +
        '<button class="dw-btn" id="dw-cancel">我关错了</button>' +
        '</div><i class="dw-progress" id="dw-progress"></i>';
      document.body.appendChild(WARN);
      DW_BAR = document.getElementById('dw-progress');
      DW_MSG = document.getElementById('dw-msg');
      var cb = document.getElementById('dw-cancel'); if(cb) cb.addEventListener('click', hideWarn);
    } else { WARN.hidden = false; }
    if(DW_MSG) DW_MSG.innerHTML = '页面将在 <i id="dw-n">' + GRACE + '</i> 秒后禁止访问，关闭可撤销';
    DW_N = document.getElementById('dw-n');
    if(DW_BAR) DW_BAR.style.width = '100%';
    var end = Date.now() + GRACE * 1000;
    warnTimer = setInterval(function(){
      var left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      if(DW_N) DW_N.textContent = left;
      if(left <= 0) block();
    }, 200);
    if(DW_BAR){
      barTimer = setInterval(function(){
        DW_BAR.style.width = Math.max(0, (end - Date.now()) / (GRACE * 1000) * 100) + '%';
      }, 100);
    }
  }

  function hideWarn(){
    warning = false;
    if(warnTimer){ clearInterval(warnTimer); warnTimer = null; }
    if(barTimer){ clearInterval(barTimer); barTimer = null; }
    if(WARN) WARN.hidden = true;
  }

  var cb = document.getElementById('dw-cancel');
  if(cb) cb.addEventListener('click', hideWarn);

  /* ================= 检测 ================= */
  /* 方案 1：DOM getter 探针（Safari Inspector 最优） */
  function consoleProbe(){
    try{
      var flag = false;
      var el = document.createElement('div');
      Object.defineProperty(el, 'id', {
        get: function(){ flag = true; return 'hm-probe'; }
      });
      console.log(el);
      console.clear();
      return flag;
    }catch(e){ return false; }
  }

  /* 方案 2：debugger 计时 */
  function timingCheck(){
    try{ var s = Date.now(); eval('debugger'); return Date.now() - s > 30; }catch(e){ return false; }
  }

  /* 方案 3：console.log 被篡改检测 */
  var _origLog = console.log;
  function consoleTamperCheck(){
    try{
      if(console.log !== _origLog) return true;
      var s = Function.prototype.toString.call(console.log);
      if(s.indexOf('[native code]') === -1) return true;
    }catch(e){ return true; }
    return false;
  }

  /* 方案 4：Safari 特征全局变量 */
  function safariFlagsCheck(){
    try{ return !!window.__webKitInspectorIsOpen; }catch(e){ return false; }
  }

  function patrol(){
    if(locked) return;
    if(Date.now() - startTime < STARTUP_DELAY) return;
    try{
      if(consoleProbe() || timingCheck() || consoleTamperCheck() || safariFlagsCheck()) showWarn();
    }catch(e){}
  }
  setInterval(patrol, PATROL);

  /* ================= 快捷键 ================= */
  addEventListener('keydown', function(e){
    if(locked) return;
    try{
      var k = (e.key || '').toUpperCase(),
          meta = e.metaKey, ctrl = e.ctrlKey, shift = e.shiftKey, alt = e.altKey, blocked = false;
      if(k === 'F12') blocked = true;
      if(meta && alt && 'IJKCU'.indexOf(k) > -1) blocked = true;
      if(meta && shift && 'IJCU'.indexOf(k) > -1) blocked = true;
      if(ctrl && shift && 'IJC'.indexOf(k) > -1) blocked = true;
      if(ctrl && !shift && !alt && k === 'U') blocked = true;
      if(blocked){
        e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
        showWarn();
      }
    }catch(e){}
  }, true);

  /* ================= 右键 / 拖拽 ================= */
  try{
    document.addEventListener('contextmenu', function(e){ e.preventDefault(); });
    document.addEventListener('dragstart', function(e){ e.preventDefault(); });
  }catch(e){}
})();