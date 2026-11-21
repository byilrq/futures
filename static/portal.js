/* No CDN dependencies; refreshing this page reads saved state, never market APIs. */
(() => {
  'use strict';
  const root=document.documentElement,themeInput=document.querySelector('#theme-toggle');
  function applyTheme(theme){
    const dark=theme!=='light';
    root.dataset.theme=dark?'dark':'light';
    if(themeInput){themeInput.checked=dark;themeInput.setAttribute('aria-checked',String(dark));}
    window.dispatchEvent(new Event('futures-theme'));
  }
  applyTheme(root.dataset.theme||'dark');
  themeInput?.addEventListener('change',()=>{
    const theme=themeInput.checked?'dark':'light';
    try{localStorage.setItem('futures_dashboard_theme',theme);}catch(_){}
    applyTheme(theme);
  });
  document.addEventListener('click',event=>{const help=event.target.closest('[data-help-target]');if(!help)return;const target=document.getElementById(help.dataset.helpTarget);if(!target)return;target.hidden=!target.hidden;help.setAttribute('aria-expanded',String(!target.hidden));});

  // All panels start collapsed on a fresh GET/reload. No panel state is stored
  // across navigation. A successful AJAX POST does not reload this document.
  function setPanelState(input){const body=document.getElementById(input.getAttribute('aria-controls'));if(body)body.hidden=!input.checked;input.setAttribute('aria-expanded',String(input.checked));}
  document.querySelectorAll('[data-collapse-toggle]').forEach(input=>{input.checked=false;setPanelState(input);});
  window.addEventListener('pageshow',event=>{if(event.persisted){document.querySelectorAll('[data-collapse-toggle]').forEach(input=>{input.checked=false;setPanelState(input);});}});
  document.addEventListener('change',event=>{
    const toggle=event.target.closest('[data-collapse-toggle]');if(toggle)setPanelState(toggle);
  });
  document.addEventListener('invalid',event=>{const content=event.target.closest('.collapse-content');if(!content||!content.hidden)return;const toggle=document.querySelector(`[data-collapse-toggle][aria-controls="${content.id}"]`);if(toggle){toggle.checked=true;setPanelState(toggle);}},true);
  document.addEventListener('input',event=>{const form=event.target.closest('form[data-revision-form]');if(form)form.dataset.dirty='true';});

  function toast(message,type='success'){
    let box=document.getElementById('client-action-message');
    if(!box){box=document.createElement('div');box.id='client-action-message';box.className='client-action-message';box.setAttribute('role','status');box.setAttribute('aria-live','polite');document.body.append(box);}
    box.textContent=message;box.dataset.type=type;box.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>{box.hidden=true;},5300);
  }
  // First-party confirmation component: never use window.confirm/alert/prompt.
  // Keep the dialog in the same document so cancel/confirm cannot navigate,
  // reload the page or reset collapsed panels.
  const confirmLayer=document.getElementById('futures-confirm-layer');
  const confirmTitle=confirmLayer?.querySelector('[data-confirm-title]');
  const confirmMessage=confirmLayer?.querySelector('[data-confirm-message]');
  const confirmButton=confirmLayer?.querySelector('[data-confirm-approve]');
  const cancelButton=confirmLayer?.querySelector('[data-confirm-cancel]');
  let resolveConfirmation=null,restoreConfirmationFocus=null;
  function finishConfirmation(approved){
    if(!resolveConfirmation)return;
    const resolve=resolveConfirmation,previousFocus=restoreConfirmationFocus;
    resolveConfirmation=null;restoreConfirmationFocus=null;
    confirmLayer.hidden=true;
    document.body.classList.remove('futures-modal-open');
    if(previousFocus&&previousFocus.isConnected&&typeof previousFocus.focus==='function'){
      previousFocus.focus({preventScroll:true});
    }
    resolve(approved);
  }
  function askConfirmation(message,options={}){
    // Fail closed if the expected custom modal markup is absent.
    if(!confirmLayer||resolveConfirmation)return Promise.resolve(false);
    const destructive=options.danger===true;
    confirmLayer.dataset.kind=destructive?'danger':'normal';
    confirmTitle.textContent=options.title||'确认操作';
    confirmMessage.textContent=String(message);
    confirmButton.textContent=options.confirmLabel||(destructive?'确认执行':'确认');
    confirmButton.className=destructive?'danger-btn':'primary-btn';
    restoreConfirmationFocus=document.activeElement;
    confirmLayer.hidden=false;
    document.body.classList.add('futures-modal-open');
    const result=new Promise(resolve=>{resolveConfirmation=resolve;});
    cancelButton.focus({preventScroll:true});
    return result;
  }
  confirmLayer?.addEventListener('click',event=>{
    const element=event.target;
    if(!(element instanceof Element))return;
    if(element.closest('[data-confirm-cancel]')||element===confirmLayer){finishConfirmation(false);return;}
    if(element.closest('[data-confirm-approve]'))finishConfirmation(true);
  });
  document.addEventListener('keydown',event=>{
    if(!resolveConfirmation)return;
    if(event.key==='Escape'){event.preventDefault();finishConfirmation(false);return;}
    if(event.key!=='Tab')return;
    const controls=[cancelButton,confirmButton].filter(button=>!button.disabled);
    if(!controls.length)return;
    const first=controls[0],last=controls[controls.length-1];
    if(event.shiftKey&&(document.activeElement===first||!controls.includes(document.activeElement))){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||!controls.includes(document.activeElement))){event.preventDefault();first.focus();}
  });
  const confirmedForms=new WeakSet();
  function submitConfirmedNative(form,submitter){
    // A destructive delete cannot be sent by an unconfirmed/no-JS form.
    const deleteMarker=form.querySelector('input[name="confirm_delete"]');
    if(deleteMarker)deleteMarker.value='yes';
    confirmedForms.add(form);
    // requestSubmit preserves browser validation, the selected submit button,
    // and the normal submit handler for non-AJAX forms.
    try{
      if(submitter&&submitter.form===form)form.requestSubmit(submitter);
      else form.requestSubmit();
    }finally{
      // If HTML validation suppressed the second submit event, do not leave
      // the confirmation bypass armed for a later attempt.
      queueMicrotask(()=>confirmedForms.delete(form));
    }
  }
  function channelVisibility(){const select=document.querySelector('[data-push-channel]');if(!select)return;document.querySelectorAll('[data-channel]').forEach(section=>{section.hidden=section.dataset.channel!==select.value;section.querySelectorAll('input,select').forEach(input=>input.disabled=section.hidden);});}
  document.querySelector('[data-push-channel]')?.addEventListener('change',channelVisibility);channelVisibility();
  function sourceVisibility(){const select=document.querySelector('[data-backtest-source]');if(!select)return;document.querySelectorAll('[data-backtest-input]').forEach(section=>{section.hidden=section.dataset.backtestInput!==select.value;section.querySelectorAll('input').forEach(input=>{input.disabled=section.hidden;if(input.type==='file')input.required=!section.hidden;});});}
  document.querySelector('[data-backtest-source]')?.addEventListener('change',sourceVisibility);sourceVisibility();
  document.querySelector('[data-backtest-symbol]')?.addEventListener('change',event=>{const select=event.target,url=new URL(select.dataset.selectUrl,window.location.origin);url.searchParams.set('symbol_id',select.value);window.location.assign(url.href);});
  // Switching contracts is a real GET navigation, not a market-data fetch.
  // The server remembers selection order and renders the selected contract first.
  document.querySelector('[data-auto-select-symbol]')?.addEventListener('change',event=>{
    const select=event.target;
    if(select.form){select.form.requestSubmit();}
  });
  function updateBacktestControls(){
    const mode=document.querySelector('[data-bt-mode]')?.value||'grid';
    const grid=mode==='grid', fixed=grid&&(document.querySelector('[data-bt-lines]')?.value==='fixed');
    document.querySelectorAll('[data-bt-grid]').forEach(el=>{el.hidden=!grid;el.querySelectorAll('input,select').forEach(i=>i.disabled=!grid);});
    document.querySelectorAll('[data-bt-hold]').forEach(el=>{el.hidden=grid;el.querySelectorAll('input').forEach(i=>{i.disabled=grid;i.required=!grid;});});
    document.querySelectorAll('[data-bt-fixed]').forEach(el=>{el.hidden=!fixed;el.querySelectorAll('input').forEach(i=>{i.disabled=!fixed;i.required=fixed;});});
  }
  document.querySelector('[data-bt-mode]')?.addEventListener('change',updateBacktestControls);
  document.querySelector('[data-bt-lines]')?.addEventListener('change',updateBacktestControls);
  updateBacktestControls();
  document.querySelectorAll('[data-time-preset]').forEach(button=>button.addEventListener('click',()=>{const form=button.closest('form');if(!form)return;const values=button.dataset.timePreset.split('|');['day_start','day_end','breaks','night_start','night_end'].forEach((name,i)=>{const field=form.querySelector(`[name="${name}"]`);if(field&&values[i]!=null)field.value=values[i];});const check=form.querySelector('[name="night_enabled"]');if(check)check.checked=values[5]==='1';}));

  const refreshRoot=document.querySelector('[data-refresh-seconds]');
  let refreshing=false;
  async function refreshLive(){
    if(!refreshRoot||refreshing)return;
    refreshing=true;
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000),note=document.querySelector('[data-refresh-note]');
    try{
      const response=await fetch(window.location.href,{credentials:'same-origin',cache:'no-store',signal:controller.signal,headers:{'X-Futures-Refresh':'1'}});
      if(!response.ok)throw new Error(String(response.status));
      const next=new DOMParser().parseFromString(await response.text(),'text/html');
      if(!next.querySelector('[data-refresh-seconds]')){if(note){note.textContent='登录已过期，请重新登录';note.classList.add('error');}return;}
      if(resolveConfirmation)return; // Do not replace a form while its confirmation is open.
      document.querySelectorAll('[data-live-section][id]').forEach(current=>{
        const replacement=next.getElementById(current.id);if(!replacement)return;
        if(resolveConfirmation&&current.id==='global-strategy-status')return;
        if(current.contains(document.activeElement)&&document.activeElement.matches('input,textarea,select'))return;
        const expanded=Array.from(current.querySelectorAll('details[open][id]')).map(x=>x.id);
        const panel=current.querySelector('[data-collapse-toggle]');const wasOpen=panel?.checked;
        current.className=replacement.className;current.innerHTML=replacement.innerHTML;
        const freshToggle=current.querySelector('[data-collapse-toggle]');
        if(freshToggle){freshToggle.checked=Boolean(wasOpen);setPanelState(freshToggle);}
        expanded.forEach(id=>{const item=current.querySelector(`#${CSS.escape(id)}`);if(item)item.open=true;});
      });
      document.querySelectorAll('form[data-revision-form][id]').forEach(form=>{
        if(form.dataset.dirty==='true')return;
        const revised=next.getElementById(form.id)?.querySelector('[name="revision"]');const field=form.querySelector('[name="revision"]');
        if(revised&&field)field.value=revised.value;
      });
      if(note){note.textContent=`已同步 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;note.classList.remove('error');}
    }catch(_){if(note){note.textContent='页面同步失败，稍后重试';note.classList.add('error');}}
    finally{refreshing=false;clearTimeout(timer);}
  }
  if(refreshRoot){const seconds=Math.max(15,Number(refreshRoot.dataset.refreshSeconds)||60);setInterval(()=>{if(!document.hidden&&!resolveConfirmation&&!document.querySelector('form[data-async][data-submitting="true"]'))refreshLive();},seconds*1000);}

  // Manual refresh is queued for the market worker. Poll only this job's
  // status and then update the visible cards without navigating away.
  function watchManualRefresh(jobId){
    const note=document.querySelector('[data-refresh-note]');
    let tries=0;
    async function check(){
      if(document.hidden)return setTimeout(check,1500);
      try{
        const response=await fetch(`/api/refresh/${jobId}`,{cache:'no-store',credentials:'same-origin'});
        if(!response.ok)throw new Error(`刷新状态查询失败 (${response.status})`);
        const result=await response.json();
        if(result.status==='done'){
          const detail=result.detail||{};
          const message=detail.status==='ok'?'行情已刷新，最新报价已同步。':`两路行情暂不可用：${detail.error||'请查看数据源状态'}`;
          toast(message,detail.status==='ok'?'success':'error');
          if(note)note.textContent=message;
          await refreshLive();
          return;
        }
        if(result.status==='error'){
          const message=`行情刷新失败：${String(result.detail||'请查看后台日志')}`;
          toast(message,'error');if(note)note.textContent=message;return;
        }
      }catch(error){
        if(tries>3){toast(error.message||'刷新状态查询失败','error');return;}
      }
      if(++tries<24)setTimeout(check,1500);
      else toast('刷新仍在等待后台处理，可稍后查看行情状态。','error');
    }
    setTimeout(check,800);
  }
  // Settings, trades, rollbacks and manual alerts: POST via fetch. The form's
  // ordinary HTTP route remains intact for browsers with JavaScript disabled.
  async function submitAsync(form){
    if(form.dataset.submitting==='true')return;
    form.dataset.submitting='true';
    const buttons=Array.from(form.querySelectorAll('button[type="submit"]'));
    buttons.forEach(button=>{button.disabled=true;});
    try{
      const response=await fetch(form.action,{method:'POST',body:new FormData(form),credentials:'same-origin',cache:'no-store',headers:{'X-Futures-Ajax':'1','Accept':'application/json'}});
      if(response.redirected&&new URL(response.url).pathname==='/login'){window.location.assign(response.url);return;}
      const payload=await response.json().catch(()=>null);
      if(!payload)throw new Error(`请求失败 (${response.status})，未返回有效结果`);
      if(!response.ok||!payload.ok)throw new Error(payload.error||payload.message||`保存失败 (${response.status})`);
      if(payload.revision!==undefined&&payload.symbol_id!==undefined){
        document.querySelectorAll('form[data-revision-form] input[name="revision"],#snapshot-history input[name="revision"]').forEach(input=>{input.value=String(payload.revision);});
      }
      if(payload.rotate_request_id){const field=form.querySelector('[name="request_id"]');if(field)field.value=payload.next_request_id||'';}
      if(payload.ntfy_password_length!==undefined){const field=form.querySelector('#ntfy-password');if(field){field.value='';field.placeholder='*'.repeat(payload.ntfy_password_length)||'未设置';}}
      const master=document.getElementById('master-strategy-toggle');
      if(master&&typeof payload.strategy_enabled==='boolean'){master.checked=payload.strategy_enabled;master.setAttribute('aria-checked',String(master.checked));const caption=master.closest('label')?.querySelector('[data-master-caption]');if(caption)caption.textContent=master.checked?'开启':'关闭';}
      form.dataset.dirty='false';toast(payload.message||'保存成功');
      if(form.hasAttribute('data-refresh-job')&&payload.refresh_job_id){watchManualRefresh(payload.refresh_job_id);}
      // Live cards are updated in place. Fold state, form values and scroll stay.
      await refreshLive();
    }catch(error){toast(error.message||'保存失败，请核对网络和参数','error');throw error;}
    finally{form.dataset.submitting='false';buttons.forEach(button=>{button.disabled=false;});}
  }
  document.addEventListener('submit',event=>{
    const form=event.target;if(!(form instanceof HTMLFormElement))return;
    if(form.dataset.once==='true'&&form.dataset.submitting==='true'){event.preventDefault();return;}
    if(confirmedForms.has(form))confirmedForms.delete(form);
    else if(form.dataset.confirm){
      event.preventDefault();
      const submitter=event.submitter;
      askConfirmation(form.dataset.confirm,{
        title:form.dataset.confirmTitle||'确认执行操作',
        danger:form.dataset.confirmDanger==='true',
        confirmLabel:form.dataset.confirmLabel||'确认继续'
      }).then(approved=>{
        if(!approved||!form.isConnected)return;
        if(form.hasAttribute('data-async'))submitAsync(form).catch(()=>{});
        else submitConfirmedNative(form,submitter);
      });
      return;
    }
    if(form.hasAttribute('data-async')){event.preventDefault();submitAsync(form).catch(()=>{});return;}
    if(form.dataset.once==='true'){form.dataset.submitting='true';setTimeout(()=>form.querySelectorAll('button[type="submit"]').forEach(button=>button.disabled=true),0);}
  });
  let masterBusy=false;
  document.addEventListener('change',async event=>{
    const switchInput=event.target.closest('#master-strategy-toggle');if(!switchInput)return;
    const requested=Boolean(switchInput.checked),previous=!requested;
    if(masterBusy){switchInput.checked=previous;return;}
    masterBusy=true;
    // While the modal is open, keep the actual persisted switch state visible.
    switchInput.checked=previous;
    let allowed=true;
    if(!requested){
      allowed=await askConfirmation('关闭后所有合约停止产生新策略信号，并向已配置的推送通道发送一条关闭提醒；行情与历史记录继续更新。',{
        title:'关闭全部合约策略',danger:true,confirmLabel:'确认关闭'
      });
    }
    if(!allowed){switchInput.setAttribute('aria-checked',String(previous));masterBusy=false;return;}
    switchInput.checked=requested;
    switchInput.setAttribute('aria-checked',String(requested));
    try{await submitAsync(switchInput.closest('form'));}
    catch(_){
      // A failed submission must restore both the visible toggle and its label.
      switchInput.checked=previous;
      switchInput.setAttribute('aria-checked',String(previous));
      const caption=switchInput.closest('label')?.querySelector('[data-master-caption]');
      if(caption)caption.textContent=previous?'开启':'关闭';
    }
    finally{masterBusy=false;}
  });
  const chartData = document.getElementById('backtest-chart-data');
  const chartBox = document.getElementById('backtest-chart');
  if (chartData && chartBox) {
    let payload;
    try { payload = JSON.parse(chartData.textContent); } catch (_) { return; }
    const records = Array.isArray(payload.series) ? payload.series : [];
    if (!records.length) return;

    const ns = 'http://www.w3.org/2000/svg';
    const tooltip = chartBox.querySelector('.chart-tooltip');
    const BUY_COLOR = '#ed555e';
    const SELL_COLOR = '#23ba83';
    const dayOf = time => String(time || '').slice(0, 10);
    const numberText = value => {
      const number = Number(value);
      return Number.isFinite(number)
        ? number.toLocaleString('zh-CN', { maximumFractionDigits: 4 }) : String(value ?? '-');
    };

    // Exchange rolls are NOT directional fills. Match only actual executed
    // buy/sell trades to the already back-adjusted series at the trade date.
    const exactIndex = new Map();
    const dayIndex = new Map();
    records.forEach((record, index) => {
      const time = String(record.time || '');
      if (!exactIndex.has(time)) exactIndex.set(time, index);
      const day = dayOf(time);
      if (day && !dayIndex.has(day)) dayIndex.set(day, index);
    });
    const groups = new Map();
    for (const trade of (Array.isArray(payload.trades) ? payload.trades : [])) {
      if (!trade || typeof trade !== 'object') continue;
      const side = String(trade.side || '').toLowerCase();
      if (side !== 'buy' && side !== 'sell') continue;
      if (String(trade.action || '').toLowerCase() === 'roll') continue;
      const lots = Number(trade.lots);
      if (!Number.isFinite(lots) || lots <= 0) continue;
      const time = String(trade.time || '');
      const index = exactIndex.has(time) ? exactIndex.get(time) : dayIndex.get(dayOf(time));
      if (index === undefined || !Number.isFinite(Number(records[index].price))) continue;
      const key = `${index}:${side}`;
      if (!groups.has(key)) groups.set(key, { index, side, trades: [], totalLots: 0, x: 0, y: 0 });
      const group = groups.get(key);
      group.trades.push(trade);
      group.totalLots += lots;
    }
    const tradeMarks = Array.from(groups.values()).sort((a, b) => a.index - b.index || (a.side === 'buy' ? -1 : 1));
    const oppositeDay = new Set();
    for (const mark of tradeMarks) {
      if (groups.has(`${mark.index}:${mark.side === 'buy' ? 'sell' : 'buy'}`)) oppositeDay.add(mark.index);
    }
    const buyLegend = document.querySelector('[data-chart-trade-buy]');
    const sellLegend = document.querySelector('[data-chart-trade-sell]');

    function tradeTooltip(mark) {
      const name = mark.side === 'buy' ? '\u4e70\u5165\u6210\u4ea4' : '\u5356\u51fa\u6210\u4ea4';
      const lines = [
        `${records[mark.index].time}  ${name} ${mark.trades.length} \u7b14 / ${numberText(mark.totalLots)} \u624b`,
        `\u4ef7\u5dee\u8c03\u6574\u53c2\u8003\u4ef7 ${numberText(records[mark.index].price)}`
      ];
      for (const trade of mark.trades.slice(0, 4)) {
        lines.push(`${String(trade.symbol || '-')}: \u5b9e\u9645\u6210\u4ea4\u4ef7 ${numberText(trade.price)} / ${numberText(trade.lots)} \u624b`);
      }
      if (mark.trades.length > 4) lines.push(`\u5176\u4ed6 ${mark.trades.length - 4} \u7b14\u8be6\u89c1\u4e0b\u65b9\u6210\u4ea4\u8868`);
      return lines.join('\n');
    }

    function drawChart() {
      chartBox.querySelector('svg')?.remove();
      if (tooltip) tooltip.hidden = true;
      const width = Math.max(chartBox.clientWidth, 300);
      const height = chartBox.clientHeight || 300;
      const left = 64, right = 20, top = 24, bottom = 34;
      const w = width - left - right, h = height - top - bottom;
      const style = getComputedStyle(root);
      const accent = style.getPropertyValue('--accent').trim();
      const muted = style.getPropertyValue('--muted').trim();
      const line = style.getPropertyValue('--line').trim();
      const card = style.getPropertyValue('--card').trim() || '#152237';
      const mode = document.querySelector('[data-chart-mode]:checked')?.value || 'price';
      document.querySelectorAll('[data-chart-price-legend]').forEach(item => { item.hidden = mode !== 'price'; });
      if (buyLegend) buyLegend.hidden = mode !== 'price' || !tradeMarks.some(mark => mark.side === 'buy');
      if (sellLegend) sellLegend.hidden = mode !== 'price' || !tradeMarks.some(mark => mark.side === 'sell');
      const legend = document.querySelector('[data-chart-legend-main]');
      if (legend) {
        legend.replaceChildren();
        const mark = document.createElement('i');
        mark.className = 'legend-mark';
        legend.append(mark, document.createTextNode(mode === 'equity' ? '\u6743\u76ca\u5e8f\u5217' : '\u4ef7\u683c\u5e8f\u5217'));
      }
      const benchmarkLegend = document.querySelector('[data-chart-benchmark-legend]');
      if (benchmarkLegend) benchmarkLegend.hidden = mode !== 'equity';
      const values = records.map(record => Number(record[mode]))
        .concat(mode === 'equity' ? records.map(record => Number(record.benchmark_equity)) : [])
        .filter(Number.isFinite);
      if (!values.length) return;
      let lo = values.reduce((a, b) => Math.min(a, b), Infinity);
      let hi = values.reduce((a, b) => Math.max(a, b), -Infinity);
      if (mode === 'price') {
        if (payload.lower != null && Number.isFinite(Number(payload.lower))) lo = Math.min(lo, Number(payload.lower));
        if (payload.upper != null && Number.isFinite(Number(payload.upper))) hi = Math.max(hi, Number(payload.upper));
      }
      const pad = (hi - lo || Math.abs(hi) * .02 || 1) * .12;
      lo -= pad;
      hi += pad;
      const sx = i => left + (records.length === 1 ? w / 2 : i * w / (records.length - 1));
      const sy = value => top + (hi - Number(value)) / (hi - lo) * h;
      const svg = document.createElementNS(ns, 'svg');
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', mode === 'equity' ? '\u56de\u6d4b\u6743\u76ca\u66f2\u7ebf' : '\u56de\u6d4b\u4ef7\u5dee\u8c03\u6574\u53c2\u8003\u7ebf\u4e0e\u6210\u4ea4\u6807\u8bb0');
      const node = (tag, attrs, text) => {
        const el = document.createElementNS(ns, tag);
        Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
        if (text != null) el.textContent = text;
        svg.appendChild(el);
        return el;
      };
      for (let i = 0; i <= 4; i++) {
        const value = lo + (hi - lo) * i / 4;
        const y = sy(value);
        node('line', { x1: left, x2: width - right, y1: y, y2: y, stroke: line, 'stroke-width': 1 });
        node('text', { x: left - 9, y: y + 4, fill: muted, 'font-size': 10, 'text-anchor': 'end' },
          value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }));
      }
      [[0, 'start'], [records.length - 1, 'end']].forEach(([index, anchor]) => {
        node('text', { x: sx(index), y: height - 10, fill: muted, 'font-size': 10, 'text-anchor': anchor },
          String(records[index].time || '').replace('T', ' ').slice(0, 16));
      });
      if (mode === 'price') {
        [['lower', '#ee9b91'], ['upper', '#7ad6b1']].forEach(([key, color]) => {
          const value = Number(payload[key]);
          if (payload[key] != null && Number.isFinite(value) && value >= lo && value <= hi) {
            node('line', { x1: left, x2: width - right, y1: sy(value), y2: sy(value),
              stroke: color, 'stroke-width': 1, 'stroke-dasharray': '5 5', opacity: .65 });
          }
        });
      }
      const path = records.map((record, index) =>
        `${index ? 'L' : 'M'}${sx(index).toFixed(2)},${sy(record[mode]).toFixed(2)}`).join(' ');
      node('path', { d: path, fill: 'none', stroke: accent, 'stroke-width': 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
      if (mode === 'equity' && records.every(record => Number.isFinite(Number(record.benchmark_equity)))) {
        const benchmarkPath = records.map((record, index) =>
          `${index ? 'L' : 'M'}${sx(index).toFixed(2)},${sy(record.benchmark_equity).toFixed(2)}`).join(' ');
        node('path', { d: benchmarkPath, fill: 'none', stroke: '#d5b36b',
          'stroke-width': 1.6, 'stroke-dasharray': '5 4', 'stroke-linejoin': 'round' });
      }

      // Compact, high-contrast filled markers placed at the adjusted close,
      // not at the unadjusted monthly fill price. Same-day sides are split 4px.
      if (mode === 'price') {
        for (const mark of tradeMarks) {
          mark.x = sx(mark.index) + (oppositeDay.has(mark.index) ? (mark.side === 'buy' ? -4 : 4) : 0);
          mark.y = sy(records[mark.index].price);
          const common = { 'data-trade-marker': mark.side, 'data-trade-day': dayOf(records[mark.index].time),
            'data-trade-count': mark.trades.length, 'pointer-events': 'none' };
          if (mark.side === 'buy') {
            node('circle', { ...common, cx: mark.x, cy: mark.y, r: 3.9,
              fill: BUY_COLOR, stroke: card, 'stroke-width': 1.1 });
          } else {
            const cross = `M${(mark.x - 3.6).toFixed(2)},${(mark.y - 3.6).toFixed(2)} ` +
              `L${(mark.x + 3.6).toFixed(2)},${(mark.y + 3.6).toFixed(2)} ` +
              `M${(mark.x + 3.6).toFixed(2)},${(mark.y - 3.6).toFixed(2)} ` +
              `L${(mark.x - 3.6).toFixed(2)},${(mark.y + 3.6).toFixed(2)}`;
            node('path', { d: cross, fill: 'none', stroke: card, 'stroke-width': 4,
              'stroke-linecap': 'round', 'pointer-events': 'none' });
            node('path', { ...common, d: cross, fill: 'none', stroke: SELL_COLOR,
              'stroke-width': 2.3, 'stroke-linecap': 'round' });
          }
        }
      }
      const cursor = node('line', { x1: left, x2: left, y1: top, y2: height - bottom,
        stroke: muted, 'stroke-dasharray': '3 4', opacity: 0 });
      const area = node('rect', { x: left, y: top, width: w, height: h, fill: 'transparent' });
      let touchPinned = false;
      function showAt(event) {
        const rect = svg.getBoundingClientRect();
        const x = (event.clientX - rect.left) * width / rect.width;
        const y = (event.clientY - rect.top) * height / rect.height;
        const index = Math.max(0, Math.min(records.length - 1, Math.round((x - left) / w * (records.length - 1))));
        const record = records[index];
        cursor.setAttribute('x1', sx(index));
        cursor.setAttribute('x2', sx(index));
        cursor.setAttribute('opacity', '.6');
        let nearMark = null, nearest = 12 * 12;
        if (mode === 'price') {
          for (const mark of tradeMarks) {
            const distance = (mark.x - x) ** 2 + (mark.y - y) ** 2;
            if (distance <= nearest) { nearest = distance; nearMark = mark; }
          }
        }
        if (tooltip) {
          tooltip.hidden = false;
          tooltip.textContent = nearMark ? tradeTooltip(nearMark)
            : `${String(record.time || '').replace('T', ' ')}\n${mode === 'equity' ? '\u6743\u76ca' : '\u53c2\u8003\u4ef7'} ${numberText(record[mode])}` +
              (mode === 'equity' && record.benchmark_equity != null
                ? `\n\u957f\u671f\u6301\u6709 1 \u624b\u57fa\u51c6 ${numberText(record.benchmark_equity)}` : '');
        }
        return nearMark;
      }
      area.addEventListener('pointermove', event => { if (!touchPinned) showAt(event); });
      area.addEventListener('pointerdown', event => {
        // Touch has no persistent hover. Tap a mark to pin its fill details.
        touchPinned = event.pointerType === 'touch' && !!showAt(event);
      });
      area.addEventListener('pointerleave', () => {
        if (!touchPinned) {
          cursor.setAttribute('opacity', '0');
          if (tooltip) tooltip.hidden = true;
        }
      });
      chartBox.prepend(svg);
    }
    drawChart();
    window.addEventListener('futures-theme', drawChart);
    document.querySelectorAll('[data-chart-mode]').forEach(input => input.addEventListener('change', drawChart));
    let chartTimer;
    window.addEventListener('resize', () => {
      clearTimeout(chartTimer);
      chartTimer = setTimeout(drawChart, 120);
    });
  }
})();
