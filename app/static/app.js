'use strict';
const $ = id => document.getElementById(id);
const labels = {submitted:'已提交', existing:'已存在', failed:'需要处理', preview:'预览', disabled:'类型已关闭', mapped:'已映射', running:'运行中', complete:'已完成', partial:'部分失败', stopped:'已停止', interrupted:'已中断'};
let currentConfig = {}, items = [], dirty = false, running = false;
const fields = ['douban_user','seerr_url','seerr_api_key','bark_url','movies','tv','history_days','interval_minutes','request_delay','tv_seasons','enabled'];
function notice(message, error=false) { $('notice').textContent=message; $('notice').className='notice'+(error?' error':''); $('notice').hidden=false; }
async function api(path, method='GET', body) {
  const response = await fetch('/api'+path, {method, headers:{'Content-Type':'application/json','X-Requested-With':'douban-reel'}, ...(body===undefined?{}:{body:JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'操作失败，请检查输入');
  return data;
}
function busy(button, action) { return async event => { if(event) event.preventDefault(); button.disabled=true; try{await action();}catch(error){notice(error.message,true);}finally{button.disabled=['sync','preview'].includes(button.id)&&running;} }; }
function applyConfig(config) {
  currentConfig=config;
  for(const key of fields) { const el=$(key); if(el.type==='checkbox') el.checked=!!config[key]; else el.value=config[key]??''; }
  $('cookieStatus').textContent=config.douban_cookie_saved?'Cookie 已保存':'未配置';
  $('seerrSaved').textContent=config.seerr_api_key_saved?'已保存 · 留空保留':'';
  $('barkSaved').textContent=config.bark_url_saved?'已保存 · 留空保留':'未配置';
  $('deleteBark').disabled=!config.bark_url_saved;
  $('deleteCookie').disabled=!config.douban_cookie_saved;
  dirty=false; $('dirty').textContent='配置已加载';
}
function configData(){const data={};for(const key of fields){const el=$(key);data[key]=el.type==='checkbox'?el.checked:el.type==='number'?Number(el.value):el.value;}return data;}
function dateText(value){return value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';}
function badge(state){const el=document.createElement('span');el.className='badge '+state;el.textContent=labels[state]||state;return el;}
function cell(row,text){const el=document.createElement('td');el.textContent=text??'—';row.append(el);return el;}
function renderItems(){
  const list=items.filter(item=>!$('filter').value||item.state===$('filter').value); $('items').replaceChildren();
  if(!list.length){const row=document.createElement('tr');const el=cell(row,items.length?'没有符合条件的记录':'尚无记录。保存配置后，先运行一次预览。');el.colSpan=6;el.className='empty';$('items').append(row);return;}
  for(const item of list){const row=document.createElement('tr');const link=document.createElement('a');link.href='https://movie.douban.com/subject/'+item.subject+'/';link.target='_blank';link.rel='noreferrer';link.textContent=item.title;cell(row,'').append(link);cell(row,item.marked);cell(row,(item.media_type==='tv'?'电视剧':item.media_type==='movie'?'电影':'待匹配')+(item.tmdb_id?' / '+item.tmdb_id:'')+(item.season?' · S'+item.season:''));cell(row,'').append(badge(item.state));cell(row,item.message);const action=cell(row,'');if(!['submitted','existing'].includes(item.state)){const button=document.createElement('button');button.className='text-button';button.textContent='指定映射';button.onclick=()=>{$('mappingSubject').value=item.subject;$('mappingTitle').textContent=item.title;$('mappingId').value=item.tmdb_id||'';$('mappingType').value=item.media_type||'movie';$('mappingSeason').value=item.season||'';$('mappingDialog').showModal();};action.append(button);}$('items').append(row);}
}
async function refresh(){
  const state=await api('/status'); running=state.running;items=state.items;renderItems();
  const health=state.cookie_health;
  $('cookieHealth').textContent=health.status==='invalid'?'Cookie 已失效或需要登录验证':health.status==='valid'?'登录校验通过'+(health.refreshed?' · 凭据已更新 '+dateText(health.refreshed):''):'每次同步前校验登录并更新豆瓣返回的 Cookie / ck';
  const notificationLabels={sent:'Bark 失效提醒已发送',failed:'Bark 推送失败，将在下次失效检查时重试',not_configured:'未配置 Bark，无法发送失效提醒',none:'同一版 Cookie 成功提醒一次，重新导入后重新启用提醒'};
  $('notificationHealth').textContent=notificationLabels[health.notification]||'';
  $('submitted').textContent=state.counts.submitted||0;$('existing').textContent=state.counts.existing||0;$('failed').textContent=state.counts.failed||0;
  $('schedulerState').textContent=state.running?'任务进行中':state.next_run?'定时同步已开启':'定时同步已关闭';
  $('nextRun').textContent=state.settings_pending?'配置已更新，将在下次任务生效':state.next_run?'下次运行 '+dateText(state.next_run):'可手动预览或同步';
  $('runningBadge').textContent=state.running?'运行中':'空闲';$('stop').hidden=!state.running;
  $('sync').disabled=state.running;$('preview').disabled=state.running;
  $('runs').replaceChildren();
  if(!state.runs.length){const p=document.createElement('p');p.className='empty';p.textContent='尚无同步任务';$('runs').append(p);}
  for(const run of state.runs){const el=document.createElement('div');el.className='run';const top=document.createElement('div');top.className='top';const time=document.createElement('time');time.textContent=dateText(run.started);top.append(time,badge(run.state));const p=document.createElement('p');p.textContent=run.message||'正在读取与匹配豆瓣条目…';const small=document.createElement('small');small.textContent=(run.preview?'预览任务':'同步任务')+' · 已处理 '+run.processed+' 条';el.append(top,p,small);$('runs').append(el);}
}
fields.forEach(key=>$(key).addEventListener('input',()=>{dirty=true;$('dirty').textContent='有未保存的修改';}));
$('settingsForm').onsubmit=busy($('saveConfig'),async()=>{applyConfig(await api('/config','PUT',configData()));notice(running?'配置已保存，下次任务生效；当前任务继续使用启动时的配置':'配置已保存');await refresh();});
$('saveCookie').onclick=busy($('saveCookie'),async()=>{notice('正在验证豆瓣登录与想看列表，请稍候…');const data=await api('/cookie','POST',{cookie:$('cookie').value,douban_user:$('douban_user').value});$('cookie').value='';currentConfig.douban_cookie_saved=true;$('cookieStatus').textContent='Cookie 已保存';$('deleteCookie').disabled=false;notice(data.message);});
$('deleteCookie').onclick=busy($('deleteCookie'),async()=>{if(!confirm('删除 Cookie 并关闭定时同步？'))return;const data=await api('/cookie','DELETE');$('cookie').value='';applyConfig(await api('/config'));notice(data.message);await refresh();});
for(const [id,target] of [['testCookie','douban'],['testSeerr','seerr']])$(id).onclick=busy($(id),async()=>{if(dirty&&target!=='douban')throw new Error('请先保存配置，再测试连接');notice('正在验证连接…');notice((await api('/test/'+target,'POST')).message);});
$('testBark').onclick=busy($('testBark'),async()=>{if(dirty)throw new Error('请先保存配置，再测试推送');notice((await api('/test/bark','POST')).message);});
$('deleteBark').onclick=busy($('deleteBark'),async()=>{if(!confirm('删除已保存的 Bark 推送地址？'))return;notice((await api('/bark','DELETE')).message);applyConfig(await api('/config'));});
for(const [id,preview] of [['sync',false],['preview',true]])$(id).onclick=busy($(id),async()=>{if(dirty)throw new Error('配置有未保存的修改，请先保存');await api('/sync?preview='+preview,'POST');notice(preview?'预览任务已开始，不会提交下载请求':'同步任务已开始');await refresh();});
$('stop').onclick=busy($('stop'),async()=>notice((await api('/stop','POST')).message));
$('filter').onchange=renderItems;
$('cancelMapping').onclick=()=>$('mappingDialog').close();
$('mappingForm').onsubmit=busy($('mappingForm').querySelector('[type=submit]'),async()=>{const data={media_type:$('mappingType').value,tmdb_id:Number($('mappingId').value),season:$('mappingSeason').value?Number($('mappingSeason').value):null};const result=await api('/mappings/'+$('mappingSubject').value,'PUT',data);$('mappingDialog').close();notice(result.message);await refresh();});
async function init(){try{applyConfig(await api('/config'));await refresh();}catch(error){notice(error.message,true);}}
init();setInterval(()=>refresh().catch(error=>notice('刷新状态失败：'+error.message,true)),5000);
