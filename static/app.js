(function(){
'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const store = {
  get(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } },
  set(k,v){ try{ localStorage.setItem(k,v); }catch(e){} }
};
const MAX_IMAGES = 10, FRAMES_PER_VIDEO = 5;

/* ---------------- language ---------------- */
let lang = store.get('ygl_lang') || 'en';
if(!LANGS.some(l=>l[0]===lang)) lang='en';
let dict = Object.assign({}, I18N.en, I18N[lang] || {});
function T(k, vars){
  let s = dict[k] ?? I18N.en[k] ?? k;
  if(vars) for(const v in vars) s = s.split('{'+v+'}').join(vars[v]);
  return s;
}
function applyText(){
  document.documentElement.lang = lang;
  document.documentElement.dir = RTL.includes(lang) ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n]').forEach(n => { n.textContent = T(n.dataset.i18n); });
  document.querySelectorAll('[data-i18n-ph]').forEach(n => { n.placeholder = T(n.dataset.i18nPh); });
  document.querySelectorAll('[data-i18n-title]').forEach(n => { n.title = T(n.dataset.i18nTitle); });
  document.querySelectorAll('[data-i18n-aria]').forEach(n => { n.setAttribute('aria-label', T(n.dataset.i18nAria)); });
  buildForm();
  renderThumbs();
  updateTheme();
  if(lastData) { renderReport(lastData); renderQuestions(lastData.report.questions || []); }
}
async function setLang(code){
  lang = code; store.set('ygl_lang', code);
  dict = Object.assign({}, I18N.en, I18N[code] || {});
  if(!I18N[code]){
    const key = 'ygl_ui_'+code+'_v'+I18N_VERSION;
    const cached = store.get(key);
    if(cached){ try{ Object.assign(dict, JSON.parse(cached)); }catch(e){} }
    else{
      $('translating').classList.add('on');
      applyText();
      try{
        const r = await fetch('/api/ui-text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lang:code,strings:I18N.en})});
        if(r.ok){ const j = await r.json(); if(lang===code){ Object.assign(dict, j.strings); store.set(key, JSON.stringify(j.strings)); } }
      }catch(e){}
      $('translating').classList.remove('on');
    }
  }
  applyText();
}
const langSel = $('langSel');
LANGS.forEach(([c,n]) => { const o=document.createElement('option'); o.value=c; o.textContent=n; langSel.appendChild(o); });
langSel.value = lang;
langSel.addEventListener('change', () => setLang(langSel.value));

/* ---------------- theme ---------------- */
const SUN='<circle class="i2" cx="12" cy="12" r="4"/><path class="i1" d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
const MOON='<path class="i1" d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/><path class="i2" d="M17 4v3M15.5 5.5h3"/>';
function isDark(){
  const t = document.documentElement.getAttribute('data-theme');
  if(t) return t==='dark';
  return window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches;
}
function updateTheme(){
  const dark = isDark();
  $('themeIcon').innerHTML = dark ? SUN : MOON;
  $('themeLabel').textContent = dark ? T('theme_light') : T('theme_dark');
}
$('themeBtn').addEventListener('click', () => {
  const next = isDark() ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  store.set('ygl_theme', next); updateTheme();
});
if(window.matchMedia) matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', updateTheme);

/* ---------------- engines ---------------- */
fetch('/health').then(r=>r.json()).then(h=>{
  const on = Object.values(h.providers||{}).filter(p=>p.keys>0);
  $('engines').textContent = on.length ? T('engines')+': '+on.map(p=>p.label+(p.keys>1?' ('+p.keys+')':'')).join(' · ') : T('engines_none');
}).catch(()=>{});

/* ---------------- errors ---------------- */
const err = $('err');
const showError = m => { err.textContent = m; err.classList.add('on'); err.scrollIntoView({behavior:'smooth',block:'nearest'}); };
const clearError = () => err.classList.remove('on');

/* ---------------- media ---------------- */
let media = []; // {id, kind:'photo'|'frame', blob, url, src}
let uid = 0;
const photos = () => media.filter(m=>m.kind==='photo');
const frames = () => media.filter(m=>m.kind==='frame');
const ordered = () => photos().concat(frames());

function loadImg(src){ return new Promise((ok,fail)=>{ const i=new Image(); i.onload=()=>ok(i); i.onerror=fail; i.src=src; }); }
async function shrink(blob, side=1600){
  try{
    const url = URL.createObjectURL(blob);
    const img = await loadImg(url); URL.revokeObjectURL(url);
    const s = Math.min(1, side/Math.max(img.naturalWidth, img.naturalHeight));
    const c = document.createElement('canvas'); c.width=Math.round(img.naturalWidth*s); c.height=Math.round(img.naturalHeight*s);
    c.getContext('2d').drawImage(img,0,0,c.width,c.height);
    return await new Promise(r=>c.toBlob(b=>r(b||blob),'image/jpeg',.88));
  }catch(e){ return blob; }
}
function addItem(kind, blob, src){
  if(media.length >= MAX_IMAGES){ showError(T('media_full')); return false; }
  media.push({id:++uid, kind, blob, url:URL.createObjectURL(blob), src:src||''});
  return true;
}
async function addPhotos(files){
  clearError();
  for(const f of files){
    if(!f.type.startsWith('image/')) continue;
    if(!addItem('photo', await shrink(f), f.name)) break;
  }
  renderThumbs();
}
function seek(v,t){ return new Promise((ok,fail)=>{ const to=setTimeout(()=>fail(new Error('seek')),8000); v.onseeked=()=>{clearTimeout(to);ok();}; v.currentTime=t; }); }
async function addVideo(file){
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.preload = 'auto';
  const url = URL.createObjectURL(file); v.src = url;
  try{
    await new Promise((ok,fail)=>{ v.onloadeddata=ok; v.onerror=()=>fail(new Error('decode')); setTimeout(()=>fail(new Error('timeout')),15000); });
    const dur = isFinite(v.duration) && v.duration>0 ? v.duration : 1;
    const n = Math.min(FRAMES_PER_VIDEO, MAX_IMAGES - media.length);
    if(n<=0){ showError(T('media_full')); return; }
    const side = 1280, s = Math.min(1, side/Math.max(v.videoWidth||side, v.videoHeight||side));
    const c = document.createElement('canvas'); c.width=Math.round((v.videoWidth||640)*s); c.height=Math.round((v.videoHeight||480)*s);
    const ctx = c.getContext('2d');
    for(let i=0;i<n;i++){
      await seek(v, dur*(i+0.5)/n);
      ctx.drawImage(v,0,0,c.width,c.height);
      const b = await new Promise(r=>c.toBlob(r,'image/jpeg',.86));
      if(b) addItem('frame', b, file.name);
    }
  }catch(e){ showError(T('video_err')); }
  finally{ URL.revokeObjectURL(url); }
}
async function addVideos(files){
  clearError();
  $('mediacount').textContent = T('frames_busy');
  for(const f of files){ if(f.type.startsWith('video/') || /\.(mp4|mov|m4v|webm|3gp)$/i.test(f.name)) await addVideo(f); }
  renderThumbs();
}
function renderThumbs(){
  const box = $('thumbs'); box.innerHTML = '';
  ordered().forEach((m,i)=>{
    const d = document.createElement('div'); d.className='thumb';
    d.innerHTML = `<img src="${m.url}" alt=""><span class="num">${i+1}</span>${m.kind==='frame'?`<span class="badge">${esc(T('video_badge'))}</span>`:''}<button class="x" type="button" aria-label="${esc(T('remove'))}">×</button>`;
    d.querySelector('.x').addEventListener('click', ()=>{ URL.revokeObjectURL(m.url); media = media.filter(x=>x!==m); renderThumbs(); });
    box.appendChild(d);
  });
  $('mediacount').textContent = media.length ? T('media_count',{p:photos().length,f:frames().length}) : '';
}
$('photoIn').addEventListener('change', e => { addPhotos([...e.target.files]); e.target.value=''; });
$('videoIn').addEventListener('change', e => { addVideos([...e.target.files]); e.target.value=''; });
const drop = $('drop');
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('over');}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('over');}));
drop.addEventListener('drop', e => {
  const fs=[...e.dataTransfer.files];
  addPhotos(fs.filter(f=>f.type.startsWith('image/'))).then(()=>addVideos(fs.filter(f=>f.type.startsWith('video/'))));
});

/* camera: take several photos in a row */
let stream=null, facing='environment', shots=0;
async function startCam(){
  stopCam();
  stream = await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:facing},width:{ideal:1920},height:{ideal:1080}},audio:false});
  $('video').srcObject = stream;
}
function stopCam(){ if(stream){ stream.getTracks().forEach(t=>t.stop()); stream=null; } }
$('openCam').addEventListener('click', async () => {
  clearError();
  if(!navigator.mediaDevices?.getUserMedia){ showError(T('cam_err')); return; }
  try{ await startCam(); shots=0; $('camCount').textContent=''; $('camModal').classList.add('on'); }
  catch(e){ showError(T('cam_err')); }
});
$('camClose').addEventListener('click', () => { stopCam(); $('camModal').classList.remove('on'); renderThumbs(); });
$('camFlip').addEventListener('click', async () => { facing = facing==='environment'?'user':'environment'; try{ await startCam(); }catch(e){} });
$('shutter').addEventListener('click', () => {
  const v=$('video'); if(!v.videoWidth) return;
  const c=document.createElement('canvas'); const s=Math.min(1,1600/Math.max(v.videoWidth,v.videoHeight));
  c.width=Math.round(v.videoWidth*s); c.height=Math.round(v.videoHeight*s);
  c.getContext('2d').drawImage(v,0,0,c.width,c.height);
  c.toBlob(b => { if(b && addItem('photo',b,'camera')){ shots++; $('camCount').textContent='✓ '+shots; } else { stopCam(); $('camModal').classList.remove('on'); renderThumbs(); } }, 'image/jpeg', .9);
});
document.addEventListener('keydown', e => { if(e.key==='Escape' && $('camModal').classList.contains('on')) $('camClose').click(); });

/* ---------------- test-data form ---------------- */
const SHAPES = [['round','sh_round'],['oval','sh_oval'],['cushion','sh_cushion'],['emerald cut / rectangular','sh_emerald'],['princess / square','sh_princess'],
  ['pear','sh_pear'],['marquise','sh_marquise'],['heart','sh_heart'],['cabochon','sh_cabochon'],['other / unknown','sh_other']];
const UV = [['none','uv_none'],['weak','uv_weak'],['red','uv_red'],['orange','uv_orange'],['yellow','uv_yellow'],['green','uv_green'],['blue','uv_blue'],['white / chalky','uv_white'],['other','uv_other']];
const OBS = ['silk_intact','silk_broken','discoid','pits','fingerprints','mineral_crystals','zoning_angular','three_phase','horsetail','lily_pad',
  'curved_striae','plato_lines','chevron','nailhead','flux','bubbles','swirl','chips','doubling','very_clean','uniform_color','flash_effect','edge_color','glass_filled'];
const PHEN = ['asterism','chatoyancy','colour_change','play_of_colour','adularescence','labradorescence','aventurescence','iridescence'];
const SPEC = ['chromium','iron_450','cobalt','rare_earth','zircon_653','almandine','none_seen'];

function fillSelect(id, opts, withNone=true){
  const sel=$(id), keep=sel.value; sel.innerHTML='';
  if(withNone){ const o=document.createElement('option'); o.value=''; o.textContent=T('not_tested'); sel.appendChild(o); }
  opts.forEach(([v,k])=>{ const o=document.createElement('option'); o.value=v; o.textContent=k?T(k):v; sel.appendChild(o); });
  if([...sel.options].some(o=>o.value===keep)) sel.value=keep;
}
function fillChoice(id, type, name, items){
  const box=$(id); const checked=new Set([...box.querySelectorAll('input:checked')].map(i=>i.value));
  box.innerHTML='';
  items.forEach(([v,k])=>{
    const l=document.createElement('label');
    l.innerHTML=`<input type="${type}" name="${name}" value="${v}" ${checked.has(v)?'checked':''}><span>${esc(T(k))}</span>`;
    box.appendChild(l);
  });
}
let formBuilt=false;
function buildForm(){
  const keepShape = formBuilt ? $('m_shape').value : 'other / unknown';
  fillSelect('m_shape', SHAPES.map(([v,k])=>[v,k]), false); $('m_shape').value = keepShape;
  fillSelect('m_thermal', [0,1,2,3,4,5,6,7,8].map(n=>[String(n),null]));
  fillSelect('m_moiss', [['yes','moiss_yes'],['no','moiss_no']]);
  fillSelect('m_fig', [['uniaxial','fig_uniaxial'],['biaxial','fig_biaxial'],['none','fig_none']]);
  fillSelect('m_pleo', [['none','pleo_none'],['dichroic','pleo_dichroic'],['trichroic','pleo_trichroic']]);
  fillSelect('m_chelsea', [['none','ch_none'],['red','ch_red'],['pink','ch_pink'],['green','ch_green'],['other','ch_other']]);
  fillSelect('m_sign', [['+','sign_pos'],['-','sign_neg']]);
  fillSelect('m_magnet', [['none','mag_none'],['weak','mag_weak'],['strong','mag_strong']]);
  fillSelect('m_uvl', UV); fillSelect('m_uvs', UV);
  fillChoice('polChoice','radio','pol', [['','not_tested'],['SR','pol_SR'],['DR','pol_DR'],['AGG','pol_AGG'],['ADR','pol_ADR']]);
  if(!document.querySelector('#polChoice input:checked')) document.querySelector('#polChoice input').checked = true;
  fillChoice('specChoice','checkbox','spec', SPEC.map(s=>[s,'sp_'+s]));
  fillChoice('phenChoice','checkbox','phen', PHEN.map(p=>[p,'phen_'+p]));
  fillChoice('obsChoice','checkbox','obs', OBS.map(o=>[o,'obs_'+o]));
  formBuilt=true; updateFilled();
}
function collectManual(){
  const v=id=>$(id).value.trim();
  return {claim:v('m_claim'), weight_ct:v('m_weight'), shape:v('m_shape'), length_mm:v('m_l'), width_mm:v('m_w'), depth_mm:v('m_d'),
    ri:v('m_ri'), biref:v('m_biref'), sg:v('m_sg'), thermal:v('m_thermal'), moissanite:v('m_moiss'),
    polariscope:(document.querySelector('#polChoice input:checked')||{}).value||'',
    optic_figure:v('m_fig'), optic_sign:v('m_sign'), magnet:v('m_magnet'),
    phenomena:[...document.querySelectorAll('#phenChoice input:checked')].map(i=>i.value), pleochroism:v('m_pleo'), chelsea:v('m_chelsea'), uv_long:v('m_uvl'), uv_short:v('m_uvs'),
    spectro:[...document.querySelectorAll('#specChoice input:checked')].map(i=>i.value),
    observations:[...document.querySelectorAll('#obsChoice input:checked')].map(i=>i.value),
    free_text:v('m_free'), lab_text:v('m_lab'), answers:collectAnswers()};
}
function updateFilled(){
  const m=collectManual();
  const cnt={basic:[m.claim,m.weight_ct,m.length_mm,m.width_mm,m.depth_mm].filter(Boolean).length,
    ri:[m.ri,m.biref,m.sg,m.thermal,m.moissanite].filter(Boolean).length,
    pol:[m.polariscope,m.optic_figure,m.optic_sign,m.magnet,m.pleochroism,m.chelsea,m.uv_long,m.uv_short].filter(Boolean).length+m.spectro.length+m.phenomena.length,
    obs:m.observations.length, lab:m.lab_text?1:0};
  document.querySelectorAll('.filled').forEach(n=>{ const c=cnt[n.dataset.for]; n.textContent=c?('✓ '+c):''; });
}
$('testCard').addEventListener('input', updateFilled);
$('testCard').addEventListener('change', updateFilled);

/* voice input in the chosen language */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec=null, recOn=false;
$('speakBtn').addEventListener('click', () => {
  if(!SR){ $('speakMsg').textContent=T('speech_na'); return; }
  if(recOn){ rec.stop(); return; }
  rec = new SR(); rec.lang = (LANGS.find(l=>l[0]===lang)||[])[2] || 'en-US'; rec.continuous = true; rec.interimResults = false;
  const ta=$('m_free');
  rec.onresult = e => { for(let i=e.resultIndex;i<e.results.length;i++) if(e.results[i].isFinal) ta.value=(ta.value+' '+e.results[i][0].transcript).trim(); updateFilled(); };
  rec.onend = () => { recOn=false; $('recDot').className=''; $('speakLabel').textContent=T('btn_speak'); };
  rec.onerror = ev => { $('speakMsg').textContent = ev.error==='not-allowed' ? T('cam_err').split('.')[0] : T('speech_na'); };
  try{ rec.start(); recOn=true; $('recDot').className='rec'; $('speakLabel').textContent=T('btn_stop'); $('speakMsg').textContent=''; }catch(e){ $('speakMsg').textContent=T('speech_na'); }
});

/* ---------------- analyze ---------------- */
let lastData=null, markers=[], answers={}, qaHistory=[];
function collectAnswers(){
  const out=qaHistory.slice();
  (lastData?.report?.questions||[]).forEach(q=>{
    const a=answers[q.id]; const txt=[a?.opt, a?.text].filter(Boolean).join(' - ');
    if(txt){ const i=out.findIndex(x=>x.q===q.question); if(i>=0) out.splice(i,1); out.push({q:q.question, a:txt}); }
  });
  return out;
}
function setBusy(on){
  ['analyzeBtn','reBtn'].forEach(id=>{ $(id).disabled=on; });
  $('analyzeBtn').textContent = on ? T('analyzing') : T('btn_analyze');
  $('busytext').classList.toggle('on',on); $('progress').classList.toggle('on',on);
}
async function analyze(retried){
  if(retried!==true) retried=false;
  if(!media.length){ showError(T('need_media')); return; }
  clearError(); setBusy(true);
  const fd=new FormData();
  ordered().forEach((m,i)=>fd.append('images', m.blob, `img${i+1}.jpg`));
  fd.append('video_frames', String(frames().length));
  fd.append('lang', lang);
  const manual=collectManual();
  fd.append('manual', JSON.stringify(manual));
  try{
    const r=await fetch('/api/analyze',{method:'POST',body:fd});
    let data; try{ data=await r.json(); }catch(_){ throw new Error(T('err_server',{s:r.status})); }
    if(!r.ok && data.busy && !retried){
      // every AI service was busy: wait a little and try once more automatically
      $('busytext').textContent = data.error.split('Details:')[0] + ' (auto-retry in 20 s)';
      await new Promise(ok=>setTimeout(ok,20000));
      $('busytext').textContent = T('busy');
      return await analyze(true);
    }
    if(!r.ok) throw new Error(data.error || T('err_server',{s:r.status}));
    data.when = new Date().toISOString();
    lastData=data; answers={}; qaHistory=manual.answers.slice(-10);
    renderReport(data);
    renderQuestions(data.report.questions||[]);
    $('report').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(e){ showError(e.message); }
  finally{ setBusy(false); }
}
$('analyzeBtn').addEventListener('click', ()=>analyze(false));
$('reBtn').addEventListener('click', ()=>analyze(false));

/* ---------------- follow-up questions ---------------- */
const LOUPE = [['curved_striae','lq_curved'],['bubbles','lq_bubbles'],['silk_intact','lq_silk'],['mineral_crystals','lq_crystals'],
  ['fingerprints','lq_fingerprints'],['zoning_angular','lq_angular'],['very_clean','lq_clean']];
function loupeQuestion(box){
  const d=document.createElement('div'); d.className='q';
  d.style.cssText='border:1.5px solid var(--amber);border-radius:12px;padding:12px;margin-bottom:8px';
  d.innerHTML=`<div class="qt">${esc(T('lq_q'))}</div><div class="why">${esc(T('lq_note'))}</div>
    <div class="opts">${LOUPE.map(([c,k])=>`<button type="button" class="opt" data-obs="${c}" aria-pressed="${!!document.querySelector('#obsChoice input[value="'+c+'"]:checked')}">${esc(T(k))}</button>`).join('')}</div>`;
  d.querySelectorAll('.opt').forEach(b=>b.addEventListener('click',()=>{
    const cb=document.querySelector('#obsChoice input[value="'+b.dataset.obs+'"]'); if(!cb) return;
    cb.checked=!cb.checked; b.setAttribute('aria-pressed', cb.checked?'true':'false'); updateFilled(); countAnswers();
  }));
  box.appendChild(d);
}
function renderQuestions(qs){
  const box=$('qlist'); box.innerHTML='';
  const needLoupe = !!lastData?.report?.gate?.need_loupe;
  $('qcard').classList.toggle('on', qs.length>0 || needLoupe);
  if(needLoupe) loupeQuestion(box);
  qs.forEach(q=>{
    const d=document.createElement('div'); d.className='q';
    d.innerHTML=`<div class="qt">${esc(q.question)}</div>${q.why?`<div class="why">${esc(q.why)}</div>`:''}
      <div class="opts">${(q.options||[]).map(o=>`<button type="button" class="opt" aria-pressed="false">${esc(o)}</button>`).join('')}</div>
      <input type="text" maxlength="300" placeholder="${esc(T('q_other'))}">`;
    d.querySelectorAll('.opt').forEach((b,i)=>b.addEventListener('click',()=>{
      const on=b.getAttribute('aria-pressed')!=='true';
      d.querySelectorAll('.opt').forEach(x=>x.setAttribute('aria-pressed','false'));
      b.setAttribute('aria-pressed',on?'true':'false');
      answers[q.id]=Object.assign(answers[q.id]||{}, {opt:on?q.options[i]:''}); countAnswers();
    }));
    d.querySelector('input').addEventListener('input',e=>{ answers[q.id]=Object.assign(answers[q.id]||{}, {text:e.target.value.trim()}); countAnswers(); });
    box.appendChild(d);
  });
  countAnswers();
}
function countAnswers(){ $('qcount').textContent = T('q_answered',{n:collectAnswers().length}); }

/* ---------------- report helpers ---------------- */
const NAME_KEY = {'not applicable':'na','unheated':'tr_unheated','heated':'tr_heated','diffusion-treated / filled':'tr_treated','natural':'au_natural','synthetic / lab-grown':'au_synthetic','glass / imitation':'au_glass','other':'other'};
const tn = n => { const k=NAME_KEY[String(n).toLowerCase()]; return k?T(k):n; };
const confT = c => ({low:T('low'),medium:T('medium'),high:T('high')}[String(c).toLowerCase()]||c||'');
function enumT(s){
  const t=String(s||'').toLowerCase();
  if(t.includes('unheated')) return T('tr_unheated');
  if(t.includes('diffusion')||t.includes('filled')) return T('tr_treated');
  if(t.includes('heated')) return T('tr_heated');
  if(t.includes('natural')) return T('au_natural');
  if(t.includes('synthetic')||t.includes('lab')) return T('au_synthetic');
  if(t.includes('glass')||t.includes('imitation')) return T('au_glass');
  return s||'-';
}
function cls(s){
  const t=String(s||'').toLowerCase();
  if(t.includes('unheated')||t.includes('natural')) return 'good';
  if(t.includes('diffusion')||t.includes('filled')||t.includes('glass')||t.includes('imitation')) return 'bad';
  if(t.includes('heated')||t.includes('synth')||t.includes('lab')) return 'mid';
  return '';
}
const pillCls = s => ({good:'ok',mid:'warn',bad:'bad'}[cls(s)]||'neutral');
function bars(list, colour){
  if(!list||!list.length) return `<p class="meta">${esc(T('no_data'))}</p>`;
  return '<div class="bars">'+list.map(x=>`<div class="bar"><span>${esc(tn(x.name))}</span>
    <div class="track" role="img" aria-label="${esc(tn(x.name))} ${x.percent}%"><div class="fill ${colour?cls(x.name):''}" style="width:${Math.max(2,Math.min(100,x.percent))}%"></div></div>
    <span class="pct">${Math.round(x.percent)}%</span></div>`).join('')+'</div>';
}
const PAL={blue:'#2347B5',red:'#C0233F',green:'#12906A',yellow:'#E2B007',orange:'#E27A07',pink:'#E06FA0',purple:'#7B3FB5',violet:'#7B3FB5',white:'#DDE3EA',colo:'#DDE3EA',black:'#222',brown:'#7B5130',gr:'#8A94A0',teal:'#12868F',
  'නිල්':'#2347B5','රතු':'#C0233F','කොළ':'#12906A','කහ':'#E2B007','රෝස':'#E06FA0','நீல':'#2347B5','சிவப்':'#C0233F','பச்சை':'#12906A','மஞ்சள்':'#E2B007'};
const dot = n => { n=String(n||'').toLowerCase(); for(const k in PAL) if(n.includes(k)) return PAL[k]; return '#8A94A0'; };
const fmtDate = iso => new Date(iso||Date.now()).toLocaleString(lang==='en'?undefined:lang);
const E = k => I18N.en[k] ?? k;   /* report PDF / print is always English */
const UVK = {'none':'uv_none','weak':'uv_weak','red':'uv_red','orange':'uv_orange','yellow':'uv_yellow','green':'uv_green','blue':'uv_blue','white / chalky':'uv_white','other':'uv_other'};
function readingRows(r, t=T){
  const rd = r.instrument?.readings || {}, rows=[];
  const rng = (a,d) => a[0]===a[1] ? Number(a[0]).toFixed(d) : Number(a[0]).toFixed(d)+' – '+Number(a[1]).toFixed(d);
  if(rd.ri) rows.push([t('f_ri').replace(/\s*\(RI\)/,'')+' (RI)', rng(rd.ri,3)]);
  if(rd.ri_otl) rows.push(['RI','OTL (> 1.81)']);
  if(rd.biref!=null) rows.push([t('f_biref').replace(/\s*\(BR\)/,'')+' (BR)', Number(rd.biref).toFixed(3)]);
  if(rd.sg) rows.push([t('f_sg').replace(/\s*\(SG\)/,'')+' (SG)', rng(rd.sg,2)]);
  if(rd.polariscope) rows.push([t('f_pol'), t('pol_'+rd.polariscope)]);
  if(rd.optic_figure) rows.push([t('f_fig'), t('fig_'+rd.optic_figure)]);
  if(rd.optic_sign) rows.push([t('f_sign'), t(rd.optic_sign==='+'?'sign_pos':'sign_neg')]);
  if(rd.pleochroism) rows.push([t('f_pleo'), t('pleo_'+rd.pleochroism)]);
  if(rd.chelsea) rows.push([t('f_chelsea'), t('ch_'+rd.chelsea)]);
  if(rd.spectro) rows.push([t('f_spec'), rd.spectro.map(s=>t('sp_'+s)).join(', ')]);
  if(rd.uv_long) rows.push([t('f_uvl'), t(UVK[rd.uv_long]||rd.uv_long)]);
  if(rd.uv_short) rows.push([t('f_uvs'), t(UVK[rd.uv_short]||rd.uv_short)]);
  if(rd.magnet) rows.push([t('f_magnet'), t('mag_'+rd.magnet)]);
  if(rd.phenomena) rows.push([t('f_phen'), rd.phenomena.map(p=>t('phen_'+p)).join(', ')]);
  if(rd.thermal!=null) rows.push([t('f_thermal'), rd.thermal+' / 8']);
  if(rd.moissanite) rows.push([t('f_moiss'), t(rd.moissanite==='yes'?'moiss_yes':'moiss_no')]);
  return rows;
}
function weightInfo(r, t=T){
  const w=r.instrument?.weight, rd=r.instrument?.readings||{}, out=[];
  if(w?.measured_ct) out.push([t('r_measured'), Number(w.measured_ct).toFixed(2)+' ct']);
  if(w?.estimated_ct) out.push([t('r_from_size'), `≈ ${w.estimated_ct} ct (${w.low}–${w.high})`]);
  if(rd.length_mm && rd.width_mm && rd.depth_mm) out.push([t('f_l').replace(/\s*\(.*/,'')+' × '+t('f_w').replace(/\s*\(.*/,'')+' × '+t('f_d').replace(/\s*\(.*/,''), `${rd.length_mm} × ${rd.width_mm} × ${rd.depth_mm} mm`]);
  if(!out.length && r.visual_weight) out.push([t('r_photo_weight'), `${r.visual_weight.min_ct} – ${r.visual_weight.max_ct} ct`]);
  return out;
}
function idName(r){ const id=r.identification||{}; const n=(lang!=='en' && id.name_local) || id.variety || id.species || '-';
  const v=r.gate?.auth_verdict; return v==='Synthetic / lab-grown' ? T('au_synthetic').split('/')[0].trim()+' · '+n : v==='Glass / imitation' ? T('au_glass')+' · '+n : n; }
function titleEn(r){ const id=r.identification||{}; const n=id.variety||id.species||'Gemstone'; const v=r.gate?.auth_verdict;
  return v==='Synthetic / lab-grown' ? 'Synthetic '+n : v==='Glass / imitation' ? 'Imitation of '+n : n; }

/* ---------------- markers ---------------- */
function drawMarkers(){
  const box=$('markstage'); if(!box) return;
  box.querySelectorAll('.mk').forEach(n=>n.remove());
  markers.forEach((m,i)=>{
    const b=document.createElement('button'); b.type='button'; b.className='mk'; b.textContent=i+1;
    b.style.left=(m.x*100)+'%'; b.style.top=(m.y*100)+'%';
    b.addEventListener('click',ev=>{ ev.stopPropagation(); markers.splice(i,1); drawMarkers(); });
    box.appendChild(b);
  });
  const v=$('verifiedNum'); if(v) v.textContent=markers.length;
}
function wireMarkers(initial, keep){
  if(!keep) markers=initial.map(p=>({x:p.x,y:p.y}));
  const box=$('markstage'), img=$('markimg'); if(!box) return;
  box.addEventListener('click',ev=>{
    const r=img.getBoundingClientRect(); const x=(ev.clientX-r.left)/r.width, y=(ev.clientY-r.top)/r.height;
    if(x<0||x>1||y<0||y>1) return; markers.push({x:+x.toFixed(4),y:+y.toFixed(4)}); drawMarkers();
  });
  $('resetMarks').addEventListener('click',()=>{ markers=initial.map(p=>({x:p.x,y:p.y})); drawMarkers(); });
  $('clearMarks').addEventListener('click',()=>{ markers=[]; drawMarkers(); });
  drawMarkers();
}

/* ---------------- on-screen report ---------------- */
let renderedFor=null;
function renderReport(data){
  const r=data.report, panel=data.panel||[], id=r.identification||{}, ev=r.evidence||{}, inst=r.instrument||{};
  const firstImg = ordered()[0]?.url || '';
  const locReason = lang!=='en' ? (r.local?.reasoning||'') : '', locConcl = lang!=='en' ? (r.local?.conclusion||'') : '';
  const topAuth=(r.authenticity||[])[0], topTreat=(r.treatment||[])[0], topOrigin=(r.origin||[])[0];
  const gate = r.gate||{};
  const stPill = st => st?`<span class="pill ${({strong:'ok',probable:'warn',indicative:'neutral'})[st]||'neutral'}">${esc(T('st_'+st))}</span>`:'';
  const ptCls = p => ({natural:'ok',unheated:'ok',synthetic:'bad',imitation:'bad',heated:'warn'})[String(p).toLowerCase()]||'neutral';
  const ptTxt = p => ({natural:T('au_natural'),synthetic:T('au_synthetic'),imitation:T('au_glass'),heated:T('tr_heated'),unheated:T('tr_unheated')})[String(p).toLowerCase()]||p;
  const forensic = (gate.features?.length || r.closeup || gate.notes?.length) ? `<div class="sec box">
      <h3>${esc(T('r_forensic'))}</h3>
      ${r.closeup?`<div style="display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 10px">
        <figure style="margin:0"><img src="${r.closeup.crop}" alt="" style="width:200px;max-width:42vw;border-radius:10px;display:block"><figcaption class="meta">${esc(T('r_closeup'))}</figcaption></figure>
        <figure style="margin:0"><img src="${r.closeup.enhanced}" alt="" style="width:200px;max-width:42vw;border-radius:10px;display:block"><figcaption class="meta">${esc(T('r_enhanced'))}</figcaption></figure></div>`:''}
      ${gate.features?.length?`<table><tbody>${gate.features.map(f=>`<tr><td class="meta" style="width:50px">#${esc(f.image)}</td><td>${esc(f.feature)}</td><td><span class="pill ${ptCls(f.points_to)}">${esc(ptTxt(f.points_to))}</span></td></tr>`).join('')}</tbody></table>`:''}
      ${gate.notes?.length?`<ul>${gate.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul>`:''}
      ${gate.expert_reasoning?`<p style="margin:8px 0 0">${esc(gate.expert_reasoning)}</p>`:''}
      ${gate.metrics?.uniformity!=null?`<p class="meta" style="margin:6px 0 0">${esc(T('r_uniform'))}: ${Math.round(gate.metrics.uniformity*100)}/100</p>`:''}
      ${gate.need_loupe?`<p class="meta" style="margin:8px 0 0;color:var(--amber)">${esc(T('auth_tip'))}</p>`:''}
    </div>` : '';
  const fromText = inst.from_text && Object.keys(inst.from_text).length
    ? `<p class="meta">${esc(T('r_from_text'))}: ${Object.entries(inst.from_text).map(([k,v])=>esc(k+': '+(Array.isArray(v)?v.join('–'):v))).join(' · ')}</p>` : '';

  const conflicts = (inst.conflicts||[]).length ? `<div class="sec warnbox"><b>${esc(T('r_conflicts'))}</b><ul>${inst.conflicts.map(c=>`<li>${esc(c)}</li>`).join('')}</ul></div>` : '';
  const tests = (r.tests_used||[]).length ? `<div class="sec"><h3>${esc(T('r_tests'))}</h3><div class="tablewrap"><table>
      <thead><tr><th>${esc(T('th_method'))}</th><th>${esc(T('th_finding'))}</th><th>${esc(T('th_meaning'))}</th></tr></thead>
      <tbody>${r.tests_used.map(t=>`<tr><td><b>${esc(t.method)}</b></td><td>${esc(t.finding)}</td><td>${esc(t.meaning)}</td></tr>`).join('')}</tbody></table></div></div>` : '';
  const rd = readingRows(r), wi = weightInfo(r);
  const readings = (rd.length||wi.length) ? `<div class="sec grid2">
      ${rd.length?`<div class="box"><h3>${esc(T('p_testdata'))}</h3><table>${rd.map(([a,b])=>`<tr><td class="meta">${esc(a)}</td><td><b>${esc(b)}</b></td></tr>`).join('')}</table></div>`:''}
      ${wi.length?`<div class="box"><h3>${esc(T('r_weight'))}</h3><table>${wi.map(([a,b])=>`<tr><td class="meta">${esc(a)}</td><td><b>${esc(b)}</b></td></tr>`).join('')}</table>${inst.weight?.check?`<p style="color:var(--ruby)">${esc(inst.weight.check)}</p>`:''}</div>`:''}
    </div>` : '';
  const match = (inst.candidates||[]).length ? `<div class="sec box"><h3>${esc(T('r_match'))}</h3>${inst.candidates.map(c=>`<div class="match"><span><b>${esc(c.name)}</b></span>
      <div class="track"><div class="fill ${c.simulant?'bad':''}" style="width:${Math.max(3,c.score)}%"></div></div><span class="pct">${c.score}%</span></div>
      <div class="meta" style="margin:-3px 0 6px">${esc(c.reasons.join('; '))}</div>`).join('')}<p class="meta" style="margin:6px 0 0">${esc(T('r_match_note'))}</p>
      ${inst.thermal_note?`<p class="meta">${esc(inst.thermal_note)}</p>`:''}</div>` : (inst.thermal_note?`<div class="sec box">${esc(inst.thermal_note)}</div>`:'');
  const evidenceUsed = (inst.evidence||[]).length ? `<div class="sec box"><h3>${esc(T('r_evidence_used'))}</h3><ul>${inst.evidence.map(e=>`<li>${esc(e)}</li>`).join('')}</ul></div>` : '';
  const rows=(r.gems||[]).map(g=>`<tr><td>${esc(g.id)}</td><td><b>${esc(g.type_local||g.type)}</b><br><span class="meta">${esc(g.shape_cut)}</span></td>
      <td><span class="chip"><i style="background:${dot(g.color)}"></i>${esc(g.color)}</span></td>
      <td>${esc((g.estimated_origin||[]).join(', '))}<br><span class="meta">${esc(T('conf'))}: ${esc(confT(g.origin_confidence))}</span></td>
      <td><span class="pill ${pillCls(g.treatment)}">${esc(enumT(g.treatment))}</span><br><span class="meta">${esc(g.treatment_indicators)}</span></td>
      <td><span class="pill ${pillCls(g.authenticity)}">${esc(enumT(g.authenticity))}</span><br><span class="meta">${esc(T('conf'))}: ${esc(confT(g.authenticity_confidence))}</span></td>
      <td>${esc(g.notes)}</td></tr>`).join('');
  const panelRows=panel.map(p=>`<tr><td>${esc(p.label)}</td><td class="meta">${esc(p.model)}</td>
      <td><span class="pill ${p.ok?'ok':'bad'}">${esc(T(p.ok?'st_ok':'st_failed'))}</span></td>
      <td>${p.ok?esc((p.species||'')+' · '+p.count):esc(p.error)}</td><td>${esc(p.seconds)} s</td></tr>`).join('');

  const keepMarks = renderedFor===data;
  $('sheet').innerHTML = `
    <div class="rhead">
      <div><h2>${esc(T('app_title'))} · ${esc(T('r_title'))}</h2>
        <div class="meta">${esc(T('r_report_no'))}: <b>${esc(r.report_no)}</b> · ${esc(fmtDate(data.when))} · ${esc(T('r_images',{n:r.images,f:r.video_frames}))} · ${esc(T('r_quality'))}: ${esc(confT(r.image_quality)||r.image_quality)}</div></div>
    </div>

    <div class="idbox">
      <div class="ring" style="--p:${id.confidence||0}"><div>${id.confidence||0}%<small>${esc(T('r_confidence'))}</small></div></div>
      <div>
        <div class="meta">${esc(T('r_ident'))}</div>
        <div class="idname">${esc(idName(r))}</div>
        <div class="idsp">${esc(T('r_species'))}: <b>${esc(id.species||'-')}</b>${id.variety?` · ${esc(T('r_variety'))}: <b>${esc(id.variety)}</b>`:''}</div>
        <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
          <span class="pill ${id.source==='instruments'?'ok':'acc'}">${esc(id.source==='instruments'?T('r_by_inst'):T('r_by_ai',{a:id.panel_agreement||''}))}</span>
          <span class="pill ${({A:'ok',B:'ok',C:'warn',D:'bad'})[ev.level]||'neutral'}">${esc(T('r_evidence'))}: ${esc(T('ev_'+ev.level))}</span>
        </div>
        ${(locReason||id.reasoning)?`<p style="margin:8px 0 0">${esc(locReason||id.reasoning)}</p>`:''}
        ${fromText}
      </div>
    </div>

    <div class="grid3">
      <div class="box"><h3>${esc(T('r_auth'))}</h3><div class="verdict">${esc(tn(gate.auth_verdict||topAuth?.name||'-'))}</div><div style="margin:-2px 0 6px">${stPill(gate.auth_strength)}</div>${bars(r.authenticity,true)}</div>
      <div class="box"><h3>${esc(T('r_treat'))}</h3><div class="verdict">${esc(tn(gate.treat_verdict||topTreat?.name||'-'))}</div><div style="margin:-2px 0 6px">${gate.treat_verdict==='Not applicable'?'':stPill(gate.treat_strength)}</div>${gate.treat_verdict==='Not applicable'?'':bars(r.treatment,true)}</div>
      <div class="box"><h3>${esc(T('r_origin'))}</h3><div class="verdict">${esc(topOrigin?topOrigin.name:'-')}</div>${bars(r.origin,false)}</div>
    </div>

    ${forensic}
    ${(locConcl||r.conclusion)?`<div class="sec"><h3>${esc(T('r_conclusion'))}</h3><div class="conclusion">${esc(locConcl||r.conclusion)}</div>
      ${(r.treatment_statement||r.origin_statement)?`<p class="meta" style="margin:8px 0 0">${esc([r.treatment_statement,r.origin_statement].filter(Boolean).join(' '))}</p>`:''}</div>`:''}
    ${conflicts}

    <div class="sec">
      <h3>${esc(T('r_evidence'))}: ${esc(T('ev_'+ev.level))} (${ev.score||0}/100)</h3>
      <div class="chips">${(ev.have||[]).map(c=>`<span class="chip">✓ ${esc(T('ev_'+c))}</span>`).join('')}</div>
      ${(ev.missing||[]).length?`<div class="meta" style="margin:8px 0 4px">${esc(T('r_ev_missing'))}:</div><div class="chips">${ev.missing.map(c=>`<span class="chip miss">+ ${esc(T('ev_'+c))}</span>`).join('')}</div>`:''}
    </div>

    ${tests}
    ${readings}
    ${match}
    ${evidenceUsed}

    <div class="sec box">
      <h3>${esc(T('r_count'))}</h3>
      <div class="countbox"><div class="bignum">${esc(r.consensus_count)}</div>
        <div><div class="meta">${esc(T('r_ai_consensus'))}${r.count_range&&r.count_range[0]!==r.count_range[1]?` (${r.count_range[0]}–${r.count_range[1]})`:''}</div>
        <div>${esc(T('r_verified'))}: <b id="verifiedNum">0</b></div></div></div>
      ${firstImg?`<div class="markstage" id="markstage"><img id="markimg" src="${firstImg}" alt=""></div>
      <p class="meta no-print">${esc(T('r_mark_hint'))}</p>
      <div class="row no-print" style="margin-top:4px"><button class="btn ghost small" id="resetMarks" type="button">${esc(T('r_reset_marks'))}</button>
        <button class="btn ghost small" id="clearMarks" type="button">${esc(T('r_clear_marks'))}</button></div>`:''}
    </div>

    <div class="sec"><h3>${esc(T('r_stones'))}</h3><div class="tablewrap"><table class="wide">
      <thead><tr><th>${esc(T('th_no'))}</th><th>${esc(T('th_type'))}</th><th>${esc(T('th_color'))}</th><th>${esc(T('th_origin'))}</th><th>${esc(T('th_treat'))}</th><th>${esc(T('th_auth'))}</th><th>${esc(T('th_notes'))}</th></tr></thead>
      <tbody>${rows||'<tr><td colspan="7">-</td></tr>'}</tbody></table></div></div>

    ${(r.lab_tests||[]).length?`<div class="sec box"><h3>${esc(T('r_labtests'))}</h3><ul>${r.lab_tests.map(t=>`<li>${esc(t)}</li>`).join('')}</ul></div>`:''}
    ${r.overall_remarks?`<div class="sec"><h3>${esc(T('r_remarks'))}</h3><p>${esc(r.overall_remarks)}</p></div>`:''}

    <div class="sec no-print"><h3>${esc(T('r_panel'))}</h3><div class="tablewrap"><table style="min-width:600px">
      <thead><tr><th>${esc(T('th_provider'))}</th><th>${esc(T('th_model'))}</th><th>${esc(T('th_status'))}</th><th>${esc(T('th_result'))}</th><th>${esc(T('th_time'))}</th></tr></thead>
      <tbody>${panelRows}</tbody></table></div></div>

    <div class="disc">${esc(T('disclaimer'))}</div>`;
  $('report').classList.add('on');
  wireMarkers(r.stone_points||[], keepMarks);
  renderedFor=data;
}

/* ---------------- one-page English report (PDF + print) ---------------- */
const LOGO='<svg viewBox="0 0 64 64" fill="none" stroke-linejoin="round" stroke-linecap="round"><path d="M20 14h24l12 12H8z" fill="#0E9F6E" opacity=".14"/><path stroke="#16325F" stroke-width="3.2" d="M20 14h24l12 12-24 32L8 26z"/><path stroke="#16325F" stroke-width="2.4" d="M8 26h48"/><path stroke="#0E9F6E" stroke-width="2" d="M8 26 20 14l6 12 6-12 6 12 6-12 12 12M26 26l6 32 6-32M17 26l15 32 15-32"/><path fill="#0E9F6E" d="M53 2.5l1.7 4.8 4.8 1.7-4.8 1.7L53 15.5l-1.7-4.8L46.5 9l4.8-1.7z"/></svg>';
const ENUM_EN = {'unheated':'Unheated','heated':'Heated','diffusion-treated / filled':'Diffusion-treated / filled','natural':'Natural','synthetic / lab-grown':'Synthetic / lab-grown','glass / imitation':'Glass / imitation'};
const cap = s => { s=String(s||'').trim(); return s ? s[0].toUpperCase()+s.slice(1) : ''; };
function getClient(){
  const name=$('c_name').value.trim(), phone=$('c_phone').value.trim(), email=$('c_email').value.trim();
  $('c_name').classList.toggle('bad', !name);
  $('c_email').classList.toggle('bad', !!email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email));
  if(!name){ showError(T('need_cname')); $('c_name').focus(); return null; }
  if(email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)){ showError(T('bad_email')); $('c_email').focus(); return null; }
  clearError();
  return {name, phone, email};
}
function onePageHtml(data, client){
  const r=data.report, ev=r.evidence||{};
  const id=Object.assign({}, r.identification||{}); id.species=String(id.species||'').replace(/\s*\(.*\)\s*$/,'');
  const g0=(r.gems||[])[0]||{};
  const est = l => (l||[]).filter(x=>x.name!=='Other').slice(0,3).map(x=>`${esc(ENUM_EN[String(x.name).toLowerCase()]||x.name)} ${Math.round(x.percent)}%`).join(' · ');
  const top = l => l&&l.length ? (ENUM_EN[String(l[0].name).toLowerCase()]||l[0].name) : '';
  const imgs = ordered().slice(0, r.closeup?4:5).map(m=>`<img src="${m.url}" alt="">`).join('') + (r.closeup?`<img src="${r.closeup.enhanced}" alt="">`:'');
  const wi = weightInfo(r,E), rd = readingRows(r,E);
  const weight = wi.find(w=>w[0]===E('r_measured'))?.[1] || wi.find(w=>w[0]===E('r_from_size'))?.[1] || wi.find(w=>w[0]===E('r_photo_weight'))?.[1] || '';
  const meas = wi.find(w=>/mm$/.test(w[1]))?.[1] || '';
  const desc = [
    [E('pdf_weight'), weight], [E('pdf_meas'), meas], [E('p_shape'), cap(g0.shape_cut)], [E('p_colour'), cap(g0.color)],
    [E('pdf_transp'), cap(id.transparency)], [E('pdf_phen'), id.phenomenon && !/^none$/i.test(id.phenomenon) ? cap(id.phenomenon) : ''],
    [E('pdf_count'), r.consensus_count>1 ? String(r.consensus_count) : '']
  ].filter(x=>x[1]);
  const identLine = id.natural_or_synthetic || top(r.authenticity) || 'Undetermined';
  const treatNA = r.gate?.treat_verdict==='Not applicable';
  const results = [
    [E('r_species'), esc(id.species||'-')],
    [E('r_variety'), esc(id.variety||'-')],
    ...(id.group?[[E('pdf_group'), esc(id.group)]]:[]),
    [E('pdf_ident'), `${esc(cap(identLine))}<small>${esc(E('pdf_estimate'))}: ${est(r.authenticity)||'-'}</small>`],
    [E('pdf_treat'), `${esc(r.treatment_statement || top(r.treatment) || 'Not determined')}${treatNA?'':`<small>${esc(E('pdf_estimate'))}: ${est(r.treatment)||'-'}</small>`}`],
    [E('pdf_origin'), `${esc(r.origin_statement || top(r.origin) || 'Not determined')}${(r.origin||[]).length?`<small>${esc(E('pdf_estimate'))}: ${est(r.origin)}</small>`:''}`],
  ];
  const tr = rows => rows.map(([a,b])=>`<tr><td>${esc(a)}</td><td>${b}</td></tr>`).join('');
  const half = Math.ceil(rd.length/2);
  const testsHtml = rd.length ? `<h4>${esc(E('pdf_tests'))}</h4><div class="ptests"><table>${tr(rd.slice(0,half).map(([a,b])=>[a,esc(b)]))}</table><table>${tr(rd.slice(half).map(([a,b])=>[a,esc(b)]))}</table></div>` : '';
  const findings=(r.tests_used||[]).slice(0,5).map(t=>`<li><b>${esc(t.method)}:</b> ${esc(t.finding)}${t.meaning?' – '+esc(t.meaning):''}</li>`).join('');
  const labs=(r.lab_tests||[]).slice(0,3).map(t=>`<li>${esc(t)}</li>`).join('');
  const conflicts=(r.instrument?.conflicts||[]).slice(0,2).map(c=>`<li>${esc(c)}</li>`).join('');
  const comment=[r.comments, r.conclusion].filter(Boolean).join(' ');
  const date=new Date(data.when||Date.now()).toLocaleDateString('en-GB',{day:'numeric',month:'long',year:'numeric'});
  const basis=`${E('pdf_ai')} of ${r.images} image${r.images>1?'s':''}${r.video_frames?` (incl. ${r.video_frames} video frames)`:''}${(ev.have||[]).filter(c=>!['photos','views'].includes(c)).length?' and '+(ev.have||[]).filter(c=>!['photos','views'].includes(c)).map(c=>E('ev_'+c).toLowerCase()).join(', '):''}.`;
  return `<div class="a4" id="a4">
    <div class="ph">
      <div class="pbrand">${LOGO}<div><div class="pname">YASIRU GEMS LAB</div><div class="psub">Gem testing &amp; analysis</div></div></div>
      <div class="pdoc"><div class="serif">${esc(E('pdf_title'))}</div><div>No. <b>${esc(r.report_no)}</b> &nbsp;·&nbsp; ${esc(date)}</div></div>
    </div>
    <div class="pclient"><div><span>${esc(E('pdf_issued'))}</span><b>${esc(client.name)}</b></div>${client.phone?`<div><span>${esc(E('pdf_tel'))}</span>${esc(client.phone)}</div>`:''}${client.email?`<div><span>${esc(E('pdf_email'))}</span>${esc(client.email)}</div>`:''}</div>
    <div class="ptitle serif">${esc(titleEn(r))}</div>
    <div class="ptsub">${esc([id.species, id.group].filter(Boolean).join(' · '))}</div>
    <div class="pgrid">
      <div>
        <div class="pphotos">${imgs}</div>
        <div class="pconf"><div><b>${id.confidence||0}%</b>${esc(E('r_confidence'))}</div><div><b>${esc(ev.level||'-')}</b>${esc(E('r_evidence'))}</div></div>
      </div>
      <div>
        ${desc.length?`<h4>${esc(E('pdf_desc'))}</h4><table>${tr(desc.map(([a,b])=>[a,esc(b)]))}</table>`:''}
        <h4>${esc(E('pdf_results'))}</h4><table>${tr(results)}</table>
      </div>
    </div>
    ${testsHtml}
    ${comment?`<h4>${esc(E('pdf_comments'))}</h4><p class="pcomm">${esc(comment)}</p>`:''}
    <div class="pcols">
      ${findings?`<div><h4>${esc(E('p_keyfind'))}</h4><ul>${findings}</ul></div>`:'<div></div>'}
      <div>${conflicts?`<h4>${esc(E('pdf_conflicts'))}</h4><ul>${conflicts}</ul>`:''}${labs?`<h4>${esc(E('pdf_further'))}</h4><ul>${labs}</ul>`:''}</div>
    </div>
    <div class="psign"><div style="font-size:.84em;color:var(--grey);max-width:380px"><b style="color:#1B2433">${esc(E('pdf_basis'))}:</b> ${esc(basis)}</div><div class="line">${esc(E('pdf_sign'))}</div></div>
    <div class="pfoot"><p>${esc(E('disclaimer'))}</p><div style="text-align:right;white-space:nowrap">${esc(E('dev'))}<br>Pryme x AI</div></div>
  </div>`;
}
const LIBS={
  canvas:['https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js','https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js'],
  pdf:['https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js','https://cdn.jsdelivr.net/npm/jspdf@2.5.1/dist/jspdf.umd.min.js']};
function loadScript(src){ return new Promise((ok,fail)=>{ const s=document.createElement('script'); s.src=src; s.onload=ok; s.onerror=fail; document.head.appendChild(s); }); }
async function loadOne(list,ready){ if(ready()) return; for(const src of list){ try{ await loadScript(src); if(ready()) return; }catch(_){} } throw new Error('PDF library could not load'); }
const waitImages = el => Promise.all([...el.querySelectorAll('img')].map(i=>i.complete?0:new Promise(r=>{ i.onload=i.onerror=r; })));
async function buildA4(client){
  const host=$('pdfhost'); host.innerHTML=onePageHtml(lastData, client||{name:'-'});
  const a4=$('a4'); await waitImages(a4); if(document.fonts?.ready) await document.fonts.ready;
  // shrink the text until everything fits on one A4 page
  let fs=12.4;
  const over = () => a4.scrollHeight>a4.clientHeight+1 || [...a4.children].some(c=>c.offsetTop+c.offsetHeight>a4.clientHeight-28);
  while(fs>7.5 && over()){ fs-=0.3; a4.style.setProperty('--fs',fs+'px'); }
  return a4;
}
const fileName = (r,c) => `${r.report_no||'Report'}_${(c.name||'client').replace(/[^\p{L}\p{N}]+/gu,'-').replace(/^-|-$/g,'')}.pdf`;
$('pdfBtn').addEventListener('click', async () => {
  if(!lastData) return;
  const client=getClient(); if(!client) return;
  const btn=$('pdfBtn'); btn.disabled=true; btn.textContent=T('making_pdf');
  try{
    await loadOne(LIBS.canvas,()=>window.html2canvas);
    await loadOne(LIBS.pdf,()=>window.jspdf&&window.jspdf.jsPDF);
    const a4=await buildA4(client);
    const canvas=await html2canvas(a4,{scale:2.2,backgroundColor:'#ffffff',useCORS:true,logging:false,width:794,height:1123,windowWidth:1200});
    const doc=new window.jspdf.jsPDF({unit:'mm',format:'a4',orientation:'portrait'});
    doc.setProperties({title:'Gemmological Report '+(lastData.report.report_no||''), author:'Yasiru Gems Lab', subject:client.name});
    doc.addImage(canvas.toDataURL('image/jpeg',0.94),'JPEG',0,0,210,297);
    doc.save(fileName(lastData.report, client));
  }catch(e){ console.error(e); showError(T('pdf_err')); }
  finally{ $('pdfhost').innerHTML=''; btn.disabled=false; btn.textContent=T('btn_pdf'); }
});
$('printBtn').addEventListener('click', async () => {
  if(!lastData) return;
  const client=getClient(); if(!client) return;
  await buildA4(client);
  const done=()=>{ $('pdfhost').innerHTML=''; window.removeEventListener('afterprint',done); };
  window.addEventListener('afterprint',done);
  window.print();
});
$('jsonBtn').addEventListener('click', () => {
  if(!lastData) return;
  const out=Object.assign({}, lastData, {verified_count:markers.length, verified_markers:markers});
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json'}));
  a.download=(lastData.report.report_no||'gem-report')+'.json'; a.click();
});
$('newBtn').addEventListener('click', () => {
  media.forEach(m=>URL.revokeObjectURL(m.url)); media=[]; lastData=null; answers={}; qaHistory=[]; renderedFor=null;
  document.querySelectorAll('#testCard input[type=text],#testCard input[type=number],#testCard textarea').forEach(i=>i.value='');
  document.querySelectorAll('#testCard input[type=checkbox]').forEach(i=>i.checked=false);
  document.querySelectorAll('#testCard select').forEach(s=>s.selectedIndex=0); $('m_shape').value='other / unknown';
  document.querySelector('#polChoice input').checked=true;
  $('report').classList.remove('on'); $('qcard').classList.remove('on'); renderThumbs(); updateFilled();
  window.scrollTo({top:0,behavior:'smooth'});
});

/* debug hook for testing the PDF layout */
window.__ygl = { buildA4, setData:d=>{ lastData=d; renderReport(d); renderQuestions(d.report.questions||[]); }, addPhotos };

setLang(lang);
})();
