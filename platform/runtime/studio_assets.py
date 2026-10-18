"""The Employee Studio page: HTML, script and style, all served from the gateway's own origin (the CSP allows nothing else).

No inline script or style, no external JS or CDN, and every value from the server is written with ``textContent`` (never as markup).
The session is the web chat's, kept in ``sessionStorage``; the state-changing calls also carry the anti-CSRF token the server issued
for that session (``X-Studio-CSRF``).
"""
from __future__ import annotations

STUDIO_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Employee Studio</title><link rel="stylesheet" href="/studio/app.css"></head>
<body><main>
<h1>Employee Studio</h1>
<p id="note" role="status"></p>
<section id="signin" hidden><p>Enter your work email address and we will send you a sign-in link that brings you back here.</p>
<form id="login"><input id="address" type="email" autocomplete="email" required><button>Send link</button></form></section>
<section id="app" hidden>
<p id="who"></p>
<div id="coworkers"></div>
<section id="actions" hidden><h2>Propose a change</h2><div id="forms"></div></section>
<h2>Change requests</h2><div id="changes"></div>
<section id="detail" hidden></section>
</section>
</main><script src="/studio/app.js"></script></body></html>
"""

STUDIO_CSS = """body{font:16px system-ui,sans-serif;margin:0;background:#f6f6f4;color:#1b1b1b}main{max-width:60rem;margin:0 auto;padding:1rem}
h1{font-size:1.5rem}h2{font-size:1.2rem;margin-top:1.5rem}h3{font-size:1rem;margin:.8rem 0 .3rem}
.card{border:1px solid #999;background:#fff;padding:.6rem;margin:.6rem 0}.ok{color:#0a6b2d}.bad{color:#a11}.muted{color:#555}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.25rem .4rem;text-align:left;vertical-align:top}
pre{white-space:pre-wrap;word-break:break-word;background:#eee;padding:.4rem}
form{display:flex;flex-wrap:wrap;gap:.5rem;margin:.5rem 0}input,select,textarea{padding:.4rem;font:inherit}textarea{width:100%;min-height:6rem}
button{padding:.4rem .8rem}.state{font-weight:600}
@media (prefers-color-scheme:dark){body{background:#161616;color:#eee}.card{background:#1f1f1f;border-color:#555}pre{background:#2a2a2a}td,th{border-color:#444}.ok{color:#6fd48b}.bad{color:#ff8d8d}.muted{color:#aaa}}
"""

STUDIO_JS = """(function(){
var S=window.sessionStorage,$=function(i){return document.getElementById(i)},session=S.getItem('mg-session'),csrf=null,role='viewer',state=null;
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined&&text!==null)e.textContent=String(text);return e}
function clear(n){while(n.firstChild)n.removeChild(n.firstChild)}
function say(t){$('note').textContent=t||''}
function api(method,path,body){var h={};if(session)h.Authorization='Bearer '+session;
if(method==='POST'){h['Content-Type']='application/json';h['X-Studio-CSRF']=csrf||''}
return fetch(path,{method:method,headers:h,credentials:'omit',body:method==='POST'?JSON.stringify(body||{}):undefined}).then(function(r){
return r.json().then(function(j){return {status:r.status,body:j}})})}
function show(id){$('signin').hidden=id!=='signin';$('app').hidden=id!=='app'}
function dep(label,rows){var d=el('div');d.appendChild(el('h3','',label));if(!rows.length){d.appendChild(el('p','muted','none'))}
else{var ul=el('ul');rows.forEach(function(r){ul.appendChild(el('li','',r))});d.appendChild(ul)}return d}
function propose(kind,params){say('');api('POST','/studio/api/changes',{kind:kind,params:params}).then(function(x){
if(x.status===200){say('Change request '+x.body.change.id+' created. Review it below; nothing has changed yet.');load(x.body.change.id)}
else say(x.body.error||'That was not accepted.')})}
function coworker(c){var card=el('div','card');card.appendChild(el('h3','',c.name+(c.deployed?'':' (planned, not deployed yet)')));
var t=el('table'),hr=el('tr');['Skill','On','Ready','Next step',''].forEach(function(h){hr.appendChild(el('th','',h))});t.appendChild(hr);
c.skills.forEach(function(s){var r=el('tr');r.appendChild(el('td','',s.name));r.appendChild(el('td',s.enabled?'ok':'muted',s.enabled?'on':'off'));
r.appendChild(el('td',s.ready?'ok':'bad',s.ready?'ready':'blocked'));r.appendChild(el('td','muted',s.next||''));var a=el('td');
if(role==='owner'&&c.deployed){var b=el('button','',s.enabled?'Propose switch off':'Propose switch on');
b.onclick=function(){propose('skill',{skill:s.name,enabled:!s.enabled})};a.appendChild(b)}r.appendChild(a);t.appendChild(r)});card.appendChild(t);
card.appendChild(dep('Tools',c.tools));
card.appendChild(dep('Access (grants)',c.grants.map(function(g){return g.family+': '+g.provider+' (tier '+g.tier+', '+g.status+(g.grant?', grant '+(g.grant.state||g.grant):'')+')'})));
card.appendChild(dep('Blockers',c.blockers.map(function(b){return b.skill+': '+b.next})));
card.appendChild(dep('Schedules',c.schedules.map(function(s){return s.id+' '+(s.cron||s.event||'')+' budget '+JSON.stringify(s.budget||{})+(s.pendingWorkspaceChange?' (edited, awaiting workspace change)':'')})));
card.appendChild(dep('Approval policy rules',c.policyRules));return card}
function field(form,name,ph,type){var i=el(type||'input');i.name=name;i.placeholder=ph;form.appendChild(i);return i}
function forms(){var box=$('forms');clear(box);$('actions').hidden=role!=='owner';if(role!=='owner')return;
var f=el('form');f.appendChild(el('strong','','Edit a schedule'));var s=field(f,'id','schedule id'),c=field(f,'cron','cron, e.g. 0 9 * * 1-5'),b=field(f,'budget','budget JSON, e.g. {"steps":20}');
var go=el('button','','Propose');go.type='submit';f.appendChild(go);f.onsubmit=function(e){e.preventDefault();var p={schedule:s.value};if(c.value)p.cron=c.value;
if(b.value){try{p.budget=JSON.parse(b.value)}catch(x){say('The budget is not valid JSON.');return}}propose('schedule',p)};box.appendChild(f);
f=el('form');f.appendChild(el('strong','','Create a coworker from this one'));var n=field(f,'name','new name (lowercase, dashes)'),r=field(f,'roleTitle','role title');
go=el('button','','Propose');go.type='submit';f.appendChild(go);f.onsubmit=function(e){e.preventDefault();propose('new-coworker',{name:n.value,roleTitle:r.value})};box.appendChild(f);
f=el('form');f.appendChild(el('strong','','Approval policy rule'));var a=el('select');['set','remove'].forEach(function(v){var o=el('option','',v);o.value=v;a.appendChild(o)});f.appendChild(a);
var rid=field(f,'ruleId','rule id (to remove)'),rule=field(f,'rule','rule JSON (to set)','textarea');go=el('button','','Propose');go.type='submit';f.appendChild(go);
f.onsubmit=function(e){e.preventDefault();if(a.value==='remove'){propose('policy',{action:'remove',ruleId:rid.value});return}
var p;try{p=JSON.parse(rule.value)}catch(x){say('The rule is not valid JSON.');return}propose('policy',{action:'set',rule:p})};box.appendChild(f);
f=el('form');f.appendChild(el('strong','','Business knowledge'));go=el('button','','Propose promoting the changed knowledge');go.type='submit';f.appendChild(go);
f.onsubmit=function(e){e.preventDefault();propose('knowledge',{action:'promote'})};box.appendChild(f)}
function changes(list){var box=$('changes');clear(box);if(!list.length)box.appendChild(el('p','muted','No change requests yet.'));
list.forEach(function(c){var card=el('div','card');card.appendChild(el('span','state',c.state+' '));card.appendChild(el('span','',c.summary+' ('+c.id+')'));
var b=el('button','','Review');b.onclick=function(){load(c.id)};card.appendChild(b);box.appendChild(card)})}
function detail(c){var d=$('detail');clear(d);d.hidden=false;d.className='card';d.appendChild(el('h2','',c.summary));
d.appendChild(el('p','','Request '+c.id+' ('+c.state+'), proposed by '+c.proposedBy+' at '+c.proposedAt+'. Digest '+c.digest));
d.appendChild(el('h3','','Exact change'));c.diff.forEach(function(x){d.appendChild(el('pre','',x.field+'\\n  before: '+JSON.stringify(x.before)+'\\n  after:  '+JSON.stringify(x.after))})});
if(c.pack){d.appendChild(el('h3','','Onboarding pack the new coworker starts with'));d.appendChild(el('pre','',c.pack))}
d.appendChild(el('h3','','Evidence checklist (checked again when you approve)'));var ul=el('ul');
(c.liveEvidence||c.evidence).forEach(function(i){ul.appendChild(el('li',i.met?'ok':'bad',(i.met?'Met: ':'Not met: ')+i.label+(i.detail?' - '+i.detail:'')))});d.appendChild(ul);
var sig=null;if(c.signoffSubject&&c.state==='proposed'&&role==='owner'){d.appendChild(el('h3','','Owner sign-off'));
d.appendChild(el('p','muted','Relaxing an approval needs your signature. On the host run: studio-changes.py signoff '+c.id+' --owner '+c.owner+' --key-file <your key>, then paste the printed JSON here.'));
sig=el('textarea');d.appendChild(sig)}
if(role!=='owner'){d.appendChild(el('p','muted','You can read this. Only the owner can approve, reject or roll back.'));return}
function act(kind,body){api('POST','/studio/api/changes/'+c.id+'/'+kind,body).then(function(x){if(x.status===200){say('Done: '+kind+'.');refresh();load(c.id)}
else say((x.body.error||'Refused.')+(x.body.reasons?' ('+x.body.reasons.join(', ')+')':''))})}
if(c.state==='proposed'){var ok=el('button','','Approve exactly this change');ok.onclick=function(){var body={digest:c.digest};
if(sig&&sig.value){try{body.signoff=JSON.parse(sig.value)}catch(x){say('The sign-off is not valid JSON.');return}}act('approve',body)};d.appendChild(ok);
var no=el('button','','Reject');no.onclick=function(){act('reject',{})};d.appendChild(no)}
if(c.state==='applied'){var rb=el('button','','Roll back');rb.onclick=function(){act('rollback',{})};d.appendChild(rb);
d.appendChild(el('p','muted','Applied here. The workspace change still has to be merged by your operator before the live coworker changes.'))}}
function load(id){api('GET','/studio/api/changes/'+encodeURIComponent(id)).then(function(x){if(x.status===200)detail(x.body.change);else say(x.body.error||'Not found.')})}
function render(){role=state.role;csrf=state.csrf;$('who').textContent='Signed in as '+state.user+' ('+(role==='owner'?'owner: you can propose and approve changes':'read-only')+')';
var box=$('coworkers');clear(box);state.coworkers.forEach(function(c){box.appendChild(coworker(c))});
var act=el('div','card');act.appendChild(dep('Recent activity (ledger)',state.activity.map(function(a){return a.timestamp+' '+(a.action||a.event)+' '+(a.outcome||'')+' '+(a.tool||'')+' '+(a.by||'')})));box.appendChild(act);
forms();changes(state.changes)}
function refresh(){api('GET','/studio/api/state').then(function(x){if(x.status===401){S.removeItem('mg-session');session=null;show('signin');say('Sign in to continue.');return}
if(x.status!==200){say('The Studio is not available.');return}state=x.body;show('app');render()}).catch(function(){say('Could not reach the Studio. Check your connection.')})}
$('login').onsubmit=function(e){e.preventDefault();fetch('/channels/web/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({address:$('address').value,studio:true})}).then(function(){say('If that address is allowed, a sign-in link is on its way.')})};
var m=/login=([^&]+)/.exec(location.hash);
if(m){history.replaceState(null,'','/studio');fetch('/channels/web/session',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({login:decodeURIComponent(m[1])})}).then(function(r){return r.json()}).then(function(j){
if(j.session){S.setItem('mg-session',j.session);S.setItem('mg-prefix',j.prefix);session=j.session;refresh()}else{show('signin');say('That link is not valid or was already used.')}})}
else if(session)refresh();else show('signin');
})();
"""
