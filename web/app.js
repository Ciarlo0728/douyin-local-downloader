const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name=local-token]').content;
const selected = new Set();
const covers = new Map();
const requestedScan = new URLSearchParams(location.search).get('scan') || '';
let currentScan = (/^[a-f0-9]{32}$/.test(requestedScan) ? requestedScan : localStorage.getItem('douyin-scan')) || '', scan = null, lastGrid = '', lastJobs = '', state = null;
let pollBusy = false, toastTimer;
const labels = {queued:'排队中',running:'下载中',success:'已完成',skipped:'已跳过',failed:'失败',cancelled:'已取消'};

function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(()=>$('toast').hidden=true,6500); }
async function api(path, data) {
  const response = await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:{'X-Local-Token':token,'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});
  if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(typeof error.detail==='string'?error.detail:'操作失败，请稍后重试');}
  return response.json();
}
function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function button(text,fn){const b=el('button','text-button',text);b.type='button';b.onclick=()=>Promise.resolve(fn()).catch(e=>toast(e.message));return b;}
function date(ts){return ts?new Date(ts*1000).toLocaleDateString('zh-CN').replaceAll('/','.'):'';}
function duration(ms){const s=Math.floor(ms/1000);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');}
function bytes(n){return n>=1048576?(n/1048576).toFixed(1)+' MB':Math.round(n/1024)+' KB';}
function visibleItems(){return (scan?.items||[]).filter(x=>x.title.toLowerCase().includes($('keyword').value.toLowerCase())&&($('download-filter').value==='all'||($('download-filter').value==='done'?x.downloaded:!x.downloaded)));}
function selection(){
  $('selected-count').textContent=selected.size;$('selection-bar').hidden=!selected.size;
  const visible=visibleItems().filter(x=>x.type==='video');
  $('select-all').checked=visible.length>0&&visible.every(x=>selected.has(x.id));
  $('select-all').indeterminate=visible.some(x=>selected.has(x.id))&&!$('select-all').checked;
  document.querySelectorAll('.video-card').forEach(card=>{card.classList.toggle('selected',selected.has(card.dataset.id));card.querySelector('input').checked=selected.has(card.dataset.id);});
}
async function coverUrl(id){
  if(!covers.has(id))covers.set(id,(async()=>{const r=await fetch('/api/covers/'+id,{headers:{'X-Local-Token':token}});return r.ok?URL.createObjectURL(await r.blob()):'';})());
  return covers.get(id);
}
const imageObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){const img=entry.target;imageObserver.unobserve(img);coverUrl(img.dataset.id).then(url=>{if(url)img.src=url;else img.style.display='none';});}}),{rootMargin:'300px'});
function renderGrid(){
  const list=visibleItems(), signature=JSON.stringify(list);
  if(signature===lastGrid){selection();return;}lastGrid=signature;
  imageObserver.disconnect();$('grid').replaceChildren();
  $('empty').hidden=Boolean(list.length);$('selection-toolbar').hidden=!list.length;
  if(scan&&!list.length){$('empty').querySelector('h3').textContent=scan.status==='running'?'正在发现作品…':'暂时没有匹配的作品';$('empty').querySelector('p').textContent=scan.items?.length?'试试调整标题或下载状态筛选。':scan.message;}
  list.forEach(item=>{
    const card=el('article','video-card');card.dataset.id=item.id;
    const poster=el('div','poster');const check=el('input');check.type='checkbox';check.disabled=item.type!=='video';check.setAttribute('aria-label','选择 '+item.title);
    const toggle=()=>{if(item.type!=='video')return;selected.has(item.id)?selected.delete(item.id):selected.add(item.id);selection();};
    check.onclick=e=>{e.stopPropagation();toggle();};poster.onclick=toggle;
    const img=el('img');img.alt='';img.dataset.id=item.id;poster.append(img,check);imageObserver.observe(img);
    poster.append(el('span','duration',item.type==='video'?duration(item.duration):'图文 · 暂不支持'));
    if(item.downloaded)poster.append(el('span','downloaded-tag','✓ 已下载'));
    const content=el('div','card-content');const title=el('h3','card-title',item.title);title.title=item.title;
    const meta=el('div','card-meta');meta.append(el('span','',date(item.published)));const link=el('a','','查看原作 ↗');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';meta.append(link);
    content.append(title,meta);card.append(poster,content);$('grid').append(card);
  });selection();
}
function renderScan(){
  if(!scan)return;
  $('result-title').textContent=scan.items?.[0]?.author||'作品列表';
  $('result-count').textContent=`已发现 ${scan.items?.length||0} 条 · 当前显示 ${visibleItems().length} 条`;
  $('library-count').textContent=scan.items?.length||0;
  $('scan-status').hidden=false;$('scan-status-text').textContent=scan.message;
  $('scan-status').classList.toggle('warning',scan.status==='restricted');
  const busy=['running','queued'].includes(scan.status);
  $('stop-scan').hidden=!busy;$('load-more').hidden=busy||scan.type!=='user'||scan.status==='complete';
  $('scan-submit').disabled=busy;$('scan-submit').textContent=busy?'扫描中…':'扫描作品 →';
  renderGrid();
}
function renderJobs(){
  const jobs=state.jobs,signature=JSON.stringify(jobs);if(signature===lastJobs)return;lastJobs=signature;
  const active=jobs.filter(x=>['queued','running'].includes(x.status)).length,done=jobs.filter(x=>['success','skipped'].includes(x.status)).length,failed=jobs.filter(x=>x.status==='failed').length;
  $('queue-count').textContent=active||jobs.length;$('queue-summary').textContent=jobs.length?`${active} 条进行中 · ${done} 条完成 / 跳过${failed?' · '+failed+' 条失败':''}`:'还没有下载任务';
  $('jobs').replaceChildren();
  jobs.slice(0,60).forEach(job=>{
    const row=el('div','job '+job.status);row.dataset.job=job.id;
    row.append(el('div','job-icon',['success','skipped'].includes(job.status)?'✓':job.status==='failed'?'!':'↓'));
    const main=el('div','job-main');main.append(el('div','job-title',job.title));
    const size=job.bytes_read?` · ${bytes(job.bytes_read)}${job.bytes_total?' / '+bytes(job.bytes_total):''}`:'';
    main.append(el('div','job-message',`${labels[job.status]||job.status} · ${job.message}${size}`));
    if(job.status==='running'){const p=el('div','job-progress');const fill=el('span');fill.style.width=job.bytes_total?Math.min(100,100*job.bytes_read/job.bytes_total)+'%':'8%';p.append(fill);main.append(p);}
    row.append(main);
    if(['failed','cancelled'].includes(job.status))row.append(button('重试',async()=>{await api('jobs/'+job.id+'/retry',{});await poll();}));
    if(['queued','running'].includes(job.status))row.append(button('取消',async()=>{await api('jobs/'+job.id+'/cancel',{});await poll();}));
    $('jobs').append(row);
  });
}
async function setScan(id){currentScan=id;localStorage.setItem('douyin-scan',id);selected.clear();lastGrid='';scan=await api('scans/'+id);$('url').value=scan.url;renderScan();}
async function poll(){
  if(pollBusy)return;pollBusy=true;
  try{
    state=await api('state');
    if(document.activeElement!==$('output'))$('output').value=state.settings.output;
    $('login-label').textContent=state.auth.logged_in?'抖音已登录':'登录抖音';$('login-dot').classList.toggle('logged',state.auth.logged_in);$('login').disabled=state.auth.busy;
    $('auth-message').textContent=state.auth.logged_in?'登录状态已保存在本机':state.auth.message;
    if(!currentScan&&state.scans.length)currentScan=state.scans[0].id;
    if(currentScan)localStorage.setItem('douyin-scan',currentScan);
    if(currentScan){try{scan=await api('scans/'+currentScan);renderScan();}catch{currentScan='';localStorage.removeItem('douyin-scan');}}
    renderJobs();
  }catch(error){$('auth-message').textContent='本地服务暂未连接，请双击 start.cmd 启动；若服务已重启请刷新页面。';}
  finally{pollBusy=false;}
}
$('scan-form').onsubmit=async e=>{e.preventDefault();$('scan-submit').disabled=true;try{const s=await api('scan',{url:$('url').value,count:Number($('scan-count').value)});await setScan(s.id);}catch(error){toast(error.message);}finally{$('scan-submit').disabled=false;}};
$('login').onclick=async()=>{try{await api('login',{});toast('请在打开的 Edge 窗口完成登录，然后回来扫描。');await poll();}catch(e){toast(e.message);}};
$('load-more').onclick=async()=>{try{await api('scans/'+currentScan+'/more',{});await poll();}catch(e){toast(e.message);}};
$('stop-scan').onclick=async()=>{try{await api('scans/'+currentScan+'/stop',{});await poll();}catch(e){toast(e.message);}};
$('select-all').onchange=e=>{visibleItems().filter(x=>x.type==='video').forEach(x=>e.target.checked?selected.add(x.id):selected.delete(x.id));selection();};
$('clear-selection').onclick=()=>{selected.clear();selection();};
$('keyword').oninput=()=>renderScan();$('download-filter').onchange=()=>renderScan();
$('download-selected').onclick=async()=>{if(!selected.size)return;$('download-selected').disabled=true;try{const r=await api('download',{ids:[...selected]});selected.clear();selection();toast(`已加入 ${r.jobs.length} 条下载任务`);await poll();$('queue-section').scrollIntoView({behavior:'smooth'});}catch(e){toast(e.message);}finally{$('download-selected').disabled=false;}};
$('save-output').onclick=async()=>{try{await api('settings',{output:$('output').value});toast('保存路径已更新，新任务会使用这个文件夹');await poll();}catch(e){toast(e.message);}};
$('choose-folder').onclick=async()=>{try{$('choose-folder').disabled=true;const s=await api('choose-folder',{});$('output').value=s.output;await poll();}catch(e){toast(e.message);}finally{$('choose-folder').disabled=false;}};
$('open-folder').onclick=()=>api('open-folder',{}).catch(e=>toast(e.message));
$('history-toggle').onclick=()=>{$('history').hidden=!$('history').hidden;$('history').replaceChildren();for(const s of state?.scans||[]){$('history').append(button(`${s.ids?.length||0} 条 · ${new Date(s.created*1000).toLocaleString('zh-CN')} · ${s.url}`,async()=>{await setScan(s.id);$('history').hidden=true;}));}};
$('queue-nav').onclick=()=>$('queue-section').scrollIntoView({behavior:'smooth'});
$('library-nav').onclick=()=>$('library-section').scrollIntoView({behavior:'smooth'});
$('shutdown').onclick=async()=>{try{await api('shutdown',{});toast('工具已退出。下次双击 start.cmd 即可重新打开。');}catch(e){toast(e.message);}};
poll();setInterval(poll,2000);
