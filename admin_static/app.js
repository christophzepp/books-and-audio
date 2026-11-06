'use strict';
const $ = id => document.getElementById(id);
const names = {audiobookshelf:'Audiobookshelf',bookorbit:'BookOrbit',bookbridge:'BookBridge',jellyfin:'Jellyfin',pinepods:'PinePods · GPodder',ntfy:'ntfy'};
const pages = {overview:['Alles im Blick','Hörpositionen und Lesestände, zuverlässig verbunden.'],books:['Deine Bücher','E-Books und Mitlese-Versionen mit ihrem Lesestand.'],audiobooks:['Deine Hörbücher','Hörpositionen und Dateistände deiner Hörbücher.'],connections:['Deine Verbindungen','Zugänge prüfen und den aktiven Dienst auswählen.'],folders:['Deine Medienordner','Speicherpfade prüfen und den Anwendungen zuordnen.'],podcasts:['Deine Podcasts','Episoden und Hörfortschritte über mehrere Dienste verbinden.'],reviews:['Freigaben','Offene Fälle für Bücher, Hörbücher und Podcastfolgen.'],events:['Was zuletzt passiert ist','Abgleiche und Änderungen an einem Ort.']};
const eventNames = {synced:'Fortschritt synchronisiert',sync_completed:'Abgleich abgeschlossen',connection_tested:'Verbindung geprüft',connection_activated:'Verbindung aktiviert',connection_updated:'Verbindung aktualisiert',review_approved:'Dateistand freigegeben',sync_paused:'Synchronisation pausiert',sync_resumed:'Synchronisation fortgesetzt',blocked:'Prüfung erforderlich',native_readalong_enrolled:'Mitlese-Version zugeordnet'};
let state = {authenticated:false,initialized:true,status:{},connections:{revision:0,profiles:[],schemas:{}},folders:{revision:0,rows:[],access_roots:[]},events:[],page:'overview',editing:null,review:null,busy:false,refreshing:false};
let toastTimer;
function element(tag, cls, text) { const node=document.createElement(tag); if(cls)node.className=cls; if(text!==undefined&&text!==null)node.textContent=String(text); return node; }
function clear(node){node.replaceChildren();}
function text(id,value){$(id).textContent=value===undefined||value===null?'—':String(value);}
function cookie(name){const found=document.cookie.split('; ').find(row=>row.startsWith(name+'='));return found?decodeURIComponent(found.slice(name.length+1)):'';}
function epoch(value){if(typeof value==='number')return value<1e12?value*1000:value;const parsed=Date.parse(value||'');return Number.isFinite(parsed)?parsed:0;}
function formatDate(value){if(!value)return 'Noch nicht erfasst';const date=new Date(epoch(value));return Number.isNaN(date.getTime())?String(value):new Intl.DateTimeFormat('de-DE',{dateStyle:'medium',timeStyle:'short'}).format(date);}
function bytes(value){if(!Number.isFinite(Number(value)))return 'Größe unbekannt';const n=Number(value);if(n<1024)return n+' B';const i=Math.min(4,Math.floor(Math.log(n)/Math.log(1024)));return (n/1024**i).toLocaleString('de-DE',{maximumFractionDigits:1})+' '+['B','KiB','MiB','GiB','TiB'][i];}
function describe(value){if(value===null||value===undefined)return '';if(typeof value==='string')return value;if(Array.isArray(value))return value.map(describe).filter(Boolean).join('\n');if(typeof value==='object')return Object.entries(value).map(([key,item])=>key+': '+describe(item)).join('\n');return String(value);}
function toast(message,error=false){clearTimeout(toastTimer);text('toast',message);$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,6500);}
function notice(message,error=false){text('global-message',message);$('global-message').hidden=!message;$('global-message').className='notice'+(error?' error':'');}
async function api(path,body){const options={credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}};if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.headers['X-CSRF-Token']=cookie('sync_admin_csrf');options.body=JSON.stringify(body);}const response=await fetch(path,options);let data;try{data=await response.json();}catch{throw new Error('Der Server hat keine lesbare Antwort gesendet.');}if(!response.ok){if(response.status===401&&!['/api/login','/api/setup'].includes(path)){state.authenticated=false;showAuth();}const error=new Error(data.error||'Die Anfrage konnte nicht ausgeführt werden.');error.status=response.status;throw error;}return data;}
function showAuth(){document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close());$('connection-form').reset();state.review=null;state.episodeReview=null;state.editing=null;state.podcastDirty=false;$('podcast-form').reset();state.status={};state.connections={revision:0,profiles:[],schemas:{}};state.events=[];['service-list','connection-list','review-books','review-book-options','review-podcasts','attention-books','attention-podcasts','podcast-rows','event-list','recent-events','review-files','connection-fields','book-rows','book-empty'].forEach(id=>clear($(id)));$('app-screen').hidden=true;$('auth-screen').hidden=false;$('setup-token-row').hidden=state.initialized;$('confirmation-row').hidden=state.initialized;text('auth-title',state.initialized?'Willkommen zurück':'Dein privater Sync-Zugang');text('auth-description',state.initialized?'Melde dich an, um Verbindungen und Freigaben zu verwalten.':'Lege einen eigenen Zugang für diese Verwaltung an. Der einmalige Einrichtungscode stammt aus der Server-Einrichtung.');text('auth-submit',state.initialized?'Anmelden':'Zugang anlegen');$('auth-password').autocomplete=state.initialized?'current-password':'new-password';$('auth-password').minLength=state.initialized?1:12;}
async function session(){const result=await api('/api/session');state.initialized=result.initialized;state.authenticated=result.authenticated;if(!state.authenticated){showAuth();return false;}$('auth-screen').hidden=true;$('app-screen').hidden=false;return true;}
$('auth-form').addEventListener('submit',async event=>{event.preventDefault();text('auth-error','');$('auth-submit').disabled=true;try{const body={username:$('auth-username').value,password:$('auth-password').value};if(!state.initialized){body.token=$('setup-token').value.trim();body.confirmation=$('auth-confirmation').value;}await api(state.initialized?'/api/login':'/api/setup',body);$('auth-form').reset();await session();await refresh();}catch(error){text('auth-error',error.message);}finally{$('auth-submit').disabled=false;}});
$('logout-button').addEventListener('click',async()=>{try{await api('/api/logout',{});state.authenticated=false;state.status={};state.connections={revision:0,profiles:[],schemas:{}};state.events=[];state.review=null;state.editing=null;await session();}catch(error){toast(error.message,true);}});
function navigate(page){if(!Object.hasOwn(pages,page))page='overview';state.page=page;document.querySelectorAll('.page').forEach(node=>node.hidden=node.id!=='page-'+page);document.querySelectorAll('[data-page]').forEach(node=>node.classList.toggle('active',node.dataset.page===page));text('page-title',pages[page][0]);text('page-description',pages[page][1]);history.replaceState(null,'','#'+page);window.scrollTo(0,0);}
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
document.querySelectorAll('[data-page]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.page)));
document.querySelectorAll('[data-go]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.go)));
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>$(button.dataset.close).close()));
function badge(label,cls=''){return element('span','badge '+cls,label);}
function button(label,cls,handler){const node=element('button',cls,label);node.type='button';node.disabled=state.busy;node.addEventListener('click',handler);return node;}
function empty(target,title,description){const box=element('div','empty');box.append(element('strong','',title),element('p','',description));target.append(box);}
function hasIssue(row){return ['blocked','error','failed','pending_review','review'].includes(row.status)||row.item_blocked===true||row.pine_status==='blocked';}
function podcastIssues(){
  const podcasts = state.status.podcasts;
  if (!podcasts?.enabled) return [];
  const episodes = podcasts.episodes || [];
  const byId = new Map(episodes.filter(row => row.id).map(row => [row.id, row]));
  const issues = episodes.filter(row => hasIssue(row) || row.can_review);
  for (const blocked of podcasts.blocked || []) {
    if (!blocked.id && !blocked.title) continue;
    const episode = byId.get(blocked.id);
    if (!episode) issues.push(blocked);
    else if (!issues.includes(episode)) issues.push(episode);
  }
  return issues;
}
function blockedBooks(){
  const books = state.status.books || [];
  const issues = books.filter(hasIssue);
  for (const blocked of state.status.blocked || []) {
    const matches = books.filter(book => blocked.abs_id ? book.abs_id === blocked.abs_id :
      book.title === blocked.title && (!blocked.type || book.type === blocked.type));
    if (matches.length !== 1) issues.push(blocked);
    else if (!issues.includes(matches[0])) issues.push(matches[0]);
  }
  return issues;
}
function bannerState(status,blocked){const paused=Boolean(status.paused||status.admin?.paused);const fatal=status.fatal||status.last_run?.fatal||status.admin?.reason;if(status.stale)return {tone:'issue',label:'Status nicht aktuell',detail:'Der Sync-Dienst hat seit '+Math.max(1,Math.floor((status.age_seconds||180)/60))+' Minuten keinen aktuellen Stand gemeldet. Die laufende Synchronisation ist nicht bestätigt.'};if(paused)return {tone:'paused',label:'Synchronisation pausiert',detail:status.admin?.reason||'Der Fortschrittsabgleich ist für alle Bücher angehalten.'};if(status.podcasts?.enabled&&(status.podcasts?.blocked||[]).length&&!fatal)return {tone:'issue',label:'Podcastfolgen benötigen Prüfung',detail:'Die betroffenen Folgen und mögliche Aktionen findest du unter Freigaben.'};if(status.podcasts?.enabled&&status.podcasts?.fatal&&!fatal)return {tone:'issue',label:'Podcast-Abgleich benötigt Prüfung',detail:status.podcasts.fatal+' Der Bücher-Abgleich wird davon unabhängig geprüft.'};if(fatal)return {tone:'error',label:status.podcasts?.enabled?'Bücher-Abgleich benötigt Prüfung':'Synchronisation global gesperrt',detail:describe(fatal)+(status.podcasts?.enabled?' Podcasts haben einen eigenen Prüfstatus unter Podcasts.':'')};if(blocked>0)return {tone:'issue',label:'Einzelne Titel benötigen Prüfung',detail:blocked+' Titel warten auf eine Prüfung. Betroffene Titel bleiben für den Abgleich gesperrt; Details stehen unter Freigaben.'};if(status.podcasts?.enabled&&status.podcasts?.pinepods_enabled&&status.podcasts.pine_waiting>0)return {tone:'issue',label:'PinePods teilweise noch nicht zugeordnet',detail:'Audiobookshelf und Jellyfin können diese Folgen weiter abgleichen, wenn ihre übrigen Prüfungen bestanden sind. '+status.podcasts.pine_waiting+' Folgen warten auf eine eindeutige PinePods-Zuordnung; Details stehen unter Podcasts.'};if(status.summary?.healthy===false)return {tone:'issue',label:'Synchronisationsstatus unklar',detail:status.message||'Ein fehlerfreier Lauf ist noch nicht bestätigt. Bitte prüfe die Verbindungen und das Protokoll.'};if(status.worker?.busy)return {tone:'',label:'Ein Auftrag wird ausgeführt',detail:typeof status.worker.busy==='string'?status.worker.busy:'Der Sync-Dienst verarbeitet einen Auftrag.'};return {tone:'',label:'Synchronisation aktiv',detail:'Neue Änderungen werden automatisch geprüft und zwischen den aktiven Diensten übertragen.'};}
function renderOverview(){const status=state.status,summary=status.summary||{};const paused=Boolean(status.paused||status.admin?.paused);const banner=bannerState(status,blockedBooks().length);$('sync-banner').className='sync-banner '+banner.tone;text('sync-label',banner.label);text('sync-detail',banner.detail);$('pause-button').disabled=paused||state.busy;$('resume-button').disabled=!paused||state.busy;$('resume-button').title=paused?'Globale Pause aufheben':'Der automatische Abgleich ist bereits freigegeben';text('metric-mapped',summary.mapped);text('metric-ebooks',summary.ebooks);const count=blockedBooks().length+podcastIssues().length;text('metric-blocked',count);text('review-count',count);$('review-count').hidden=!count;text('metric-podcasts',status.podcasts?.episodes?.length ?? (status.podcasts?.enabled ? '—' : 0));const last=status.last_run||status.last_cycle;const when=typeof last==='string'||typeof last==='number'?last:last?.finished_at||last?.at||last?.ended_at||last?.started_at;text('last-run',when?formatDate(when):'Noch kein abgeschlossener Lauf erfasst.');renderEvents($('recent-events'),state.events.slice(0,4));
  renderIssueList('attention-books', blockedBooks(), 'books');
  renderIssueList('attention-podcasts', podcastIssues(), 'podcasts');
}
function bookPosition(book){if(['finished','completed'].includes(book.progress_status))return 'Abgeschlossen';if(book.type==='ebook'){if(Number.isFinite(book.position_percent))return book.position_percent.toLocaleString('de-DE',{maximumFractionDigits:1})+' %';if(Number.isFinite(book.progress_fraction))return (book.progress_fraction*100).toLocaleString('de-DE',{maximumFractionDigits:1})+' %';return book.position?'Leseposition gespeichert':'Noch kein Fortschritt';}const value=typeof book.position==='number'?book.position:book.position?.seconds;if(!Number.isFinite(value))return 'Noch kein Fortschritt';const seconds=Math.max(0,Math.floor(value));return Math.floor(seconds/3600)+':'+String(Math.floor(seconds%3600/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');}
function renderLibrary(prefix,ebook){clear($(prefix+'-rows'));clear($(prefix+'-empty'));const all=(state.status.books||[]).filter(book=>(book.type==='ebook')===ebook);const query=$(prefix+'-search').value.trim().toLocaleLowerCase('de');const issues=$(prefix+'-filter').value==='issues';const books=all.filter(book=>(!issues||hasIssue(book))&&(book.title||'').toLocaleLowerCase('de').includes(query));text(prefix+'-count',books.length+' von '+all.length+' Titeln · Stand der letzten Rückmeldung');$(prefix+'-empty').hidden=Boolean(books.length);if(!books.length){empty($(prefix+'-empty'),issues?'Keine passenden Fehler oder Sperren':query?'Kein passender Titel':'Noch keine Titel erfasst',issues?'Ändere die Ansicht, um alle Titel zu sehen.':query?'Versuche einen anderen Suchbegriff.':'Die Übersicht wird nach dem nächsten Lauf aktualisiert.');return;}const labels={ok:'Bereit',blocked:'Gesperrt',waiting:'Wartet',paused:'Pausiert',pending_review:'Prüfung nötig'};for(const book of books){const row=element('tr');const title=element('td','book-title');title.append(element('strong','',book.title||'Unbenannter Titel'));if(book.reason)title.append(element('small','muted',describe(book.reason)));const kind=element('td','',book.type==='ebook'?'E-Book':'Hörbuch');const status=element('td');status.append(badge(labels[book.status]||'Nicht bestätigt',book.status==='ok'?'good':book.status==='blocked'?'bad':'warning'));const position=element('td','book-position',bookPosition(book));const action=element('td','book-action');const id=book.abs_id;const reviewable=book.can_review!==false&&Boolean(id)&&(book.has_readalong===true||book.status==='blocked');if(reviewable)action.append(button('Dateiprüfung','secondary',()=>previewReview(id)));row.append(title,kind,status,position,action);$(prefix+'-rows').append(row);}}
function renderBooks(){renderLibrary('book',true);renderLibrary('audio',false);}

function connectionSchema(kind){return state.connections.schemas?.[kind]||{label:names[kind]||kind,fields:[]};}
function renderServices(){
  const target = $('service-list');
  clear(target);
  const profiles = state.connections.profiles || [];
  for (const [kind, name] of Object.entries(names)) {
    const saved = profiles.filter(profile => profile.kind === kind);
    const active = saved.find(profile => profile.active);
    const unhealthy = state.status.services?.[kind]?.healthy === false;
    const card = element('div', 'service-item');
    const copy = element('div');
    const description = saved.length ? saved.map(profile => profile.name || name).join(' · ') : 'Kein Profil eingerichtet';
    copy.append(element('strong', '', name), element('small', '', description));
    const label = active ? (unhealthy ? 'Prüfung nötig' : 'Aktiv') : (saved.length ? 'Inaktiv' : 'Nicht eingerichtet');
    card.append(element('span', 'service-icon', name[0]), copy, badge(label, active ? (unhealthy ? 'warning' : 'good') : ''));
    target.append(card);
  }
}
function renderConnections(){clear($('connection-list'));const profiles=state.connections.profiles||[];if(!profiles.length)empty($('connection-list'),'Noch keine Verbindung vorhanden','Füge deinen ersten Dienst hinzu und prüfe den Zugang.');for(const profile of profiles){const card=element('article','connection-card');const head=element('div','card-heading');const copy=element('div');copy.append(element('h3','',profile.name||names[profile.kind]||'Verbindung'),element('p','muted',names[profile.kind]||profile.kind));head.append(copy,badge(profile.active?'Aktiv':'Inaktiv',profile.active?'good':''));card.append(head);if(profile.settings?.url)card.append(element('p','muted',profile.settings.url));const details=element('div','connection-detail');if(profile.settings?.username)details.append(element('span','','Konto: '+profile.settings.username));const count=Object.values(profile.has_secrets||{}).filter(Boolean).length;details.append(element('span','',count?'Zugangsdaten hinterlegt':'Zugangsdaten fehlen'));card.append(details);if(profile.kind==='pinepods')card.append(element('p','help','GPodder-Zugang für Podcast-Hörfortschritte. In PinePods muss der interne Server aktiviert und die angegebene Geräte-ID bereits vorhanden sein. Der Verbindungstest ändert keine Abos oder Hörstände.'));if(profile.kind==='ntfy')card.append(element('p','help','Beim Prüfen oder Aktivieren wird eine Testmeldung gesendet.'));const actions=element('div','card-actions');actions.append(button('Bearbeiten','secondary',()=>editConnection(profile)),button('Verbindung prüfen','secondary',()=>connectionCommand('test',profile)),button(profile.active?'Deaktivieren':'Aktivieren',profile.active?'secondary':'primary',()=>connectionCommand(profile.active?'deactivate':'activate',profile)),button('Entfernen','quiet danger',()=>connectionCommand('delete',profile)));card.append(actions);$('connection-list').append(card);}if(state.connections.disabled_reason){const note=element('div','notice',describe(state.connections.disabled_reason));$('connection-list').prepend(note);}}
function openItem(page, title){
  $(page === 'podcasts' ? 'podcast-search' : page==='audiobooks'?'audio-search':'book-search').value = title || '';
  $(page === 'podcasts' ? 'podcast-filter' : page==='audiobooks'?'audio-filter':'book-filter').value = 'all';
  if (page === 'podcasts') renderPodcastEpisodes(); else renderBooks();
  navigate(page);
}
function renderIssueList(targetId, rows, page){
  const target = $(targetId);
  const podcast = page === 'podcasts';
  clear(target);
  if (!rows.length) {
    const text = podcast ? 'Keine Podcastfolge benötigt eine Prüfung.' : 'Kein Buch oder Hörbuch benötigt eine Prüfung.';
    target.append(element('p', 'clear-status', text));
    return;
  }
  for (const item of rows) {
    const row = element('article', 'issue-row');
    const copy = element('div', 'issue-copy');
    const type = podcast ? item.show || 'Podcast' : item.type === 'ebook' ? 'E-Book' : 'Hörbuch';
    copy.append(element('small', 'muted', type), element('h3', '', item.title || 'Unbenannter Eintrag'));
    const reason = item.pine_status_reason || item.pine_reason || item.reason;
    if (reason) copy.append(element('p', 'muted', describe(reason)));
    row.append(copy);
    if (podcast && item.id && (item.can_review||item.can_edit_status)) {
      row.append(episodeReviewButton(item));
    } else if (!podcast && item.type !== 'ebook' && item.abs_id && item.can_review !== false) {
      row.append(button('Dateistand prüfen', 'secondary', () => previewReview(item.abs_id)));
    } else {
      if (item.pine_reason_code === 'episode_identity_unresolved') {
        copy.append(element('p', 'help', 'Prüfe die Audio-Adresse der Folge in PinePods. Sobald sie eindeutig zugeordnet ist, prüft der Abgleich sie erneut.'));
      }
      row.append(button(podcast ? 'Folge ansehen' : 'Bücher ansehen', 'quiet', () => openItem(podcast||item.type==='ebook'?page:'audiobooks', item.title)));
    }
    target.append(row);
  }
}
function renderReviews(){
  const books = blockedBooks();
  renderIssueList('review-books', books, 'books');
  renderIssueList('review-podcasts', podcastIssues(), 'podcasts');
  const available = (state.status.books || []).filter(book =>
    book.has_readalong && book.abs_id && book.can_review !== false && !books.includes(book));
  $('additional-book-reviews').hidden = !available.length;
  if (available.length) renderIssueList('review-book-options', available, 'books');
  else clear($('review-book-options'));
}
function eventSeverity(event){const value=[event.level,event.status,event.event,event.kind].filter(Boolean).join(' ').toLowerCase();return /error|failed|fail|fehler/.test(value)?'bad':/warn|block|pending|review/.test(value)?'warning':/success|synced|complete|approved/.test(value)?'good':'';}
function renderEvents(target,items){clear(target);if(!items.length){empty(target,'Noch keine Ereignisse','Neue Aktivitäten erscheinen hier automatisch.');return;}for(const item of items){const row=element('article','event');const title=item.title||item.message||eventNames[item.event]||eventNames[item.kind]||'Synchronisation aktualisiert';const copy=element('div');copy.append(element('strong','',title));const detail=item.reason||item.error||item.description||item.detail;if(detail)copy.append(element('p','',describe(detail)));row.append(element('span','event-dot '+eventSeverity(item)),copy,element('time','',formatDate(item.at||item.created_at||item.timestamp)));target.append(row);}}
function render(){renderOverview();renderBooks();renderServices();renderConnections();renderFolders();renderProvider();renderPodcasts();renderReviews();let events=state.events;if($('event-filter').value==='attention')events=events.filter(item=>['bad','warning'].includes(eventSeverity(item)));renderEvents($('event-list'),events);$('clear-events').disabled=state.busy||!state.events.length;$('add-connection').disabled=state.busy;}
function mergeEvents(response){const commands=Array.isArray(response.commands)?response.commands:[];const ids=new Set(commands.map(item=>item.id));const events=(Array.isArray(response)?response:response.events||[]).filter(item=>!ids.has(item.detail?.command_id));const actions={'connection.create':'Verbindung angelegt','connection.update':'Verbindung bearbeitet','connection.delete':'Verbindung entfernt','connection.test':'Verbindung geprüft','connection.activate':'Verbindung aktiviert','connection.deactivate':'Verbindung deaktiviert','review.preview':'Dateistand geprüft','review.approve':'Dateistand freigegeben','folder.create':'Ordnerprofil angelegt','folder.update':'Ordnerprofil bearbeitet','folder.test':'Ordner geprüft','folder.activate':'Ordnerprofil aktiviert','folder.deactivate':'Ordnerprofil deaktiviert','folder.delete':'Ordnerprofil entfernt','provider.preview':'Zuordnungsmodus geprüft','provider.activate':'Zuordnungsmodus aktiviert','podcast.preview':'Podcast-Abgleich geprüft','podcast.configure':'Podcast-Einstellungen übernommen','episode.status_preview':'Hörstände der Folge geprüft','episode.status_apply':'Hörstatus freigegeben','episode.preview':'Podcastfolge geprüft','episode.approve':'Statusabgleich freigegeben','sync.pause':'Synchronisation pausiert','sync.resume':'Synchronisation fortgesetzt'};const statuses={queued:'wartet',running:'läuft',success:'abgeschlossen',failed:'fehlgeschlagen'};for(const command of commands)events.push({at:command.updated_at||command.created_at,status:command.status,kind:command.kind,title:(actions[command.kind]||'Verwaltungsauftrag')+' · '+(statuses[command.status]||command.status),error:command.error,description:command.result?.message});return events.sort((a,b)=>epoch(b.at||b.created_at||b.timestamp)-epoch(a.at||a.created_at||a.timestamp));}
async function refresh(){if(!state.authenticated||state.refreshing)return;state.refreshing=true;$('refresh-button').disabled=true;try{const [status,connections,events,folders]=await Promise.all([api('/api/status'),api('/api/connections'),api('/api/events'),api('/api/folders')]);state.status=status;state.connections=connections;state.folders=folders;state.events=mergeEvents(events);render();text('last-refreshed','Aktualisiert: '+formatDate(new Date().toISOString()));if(!state.busy)notice('');}catch(error){notice(error.message,true);}finally{state.refreshing=false;$('refresh-button').disabled=false;}}
$('refresh-button').addEventListener('click',refresh);$('event-filter').addEventListener('change',render);$('book-search').addEventListener('input',renderBooks);
function confirmation(title,description,accept='Bestätigen'){return new Promise(resolve=>{text('confirm-title',title);text('confirm-description',description);text('confirm-accept',accept);const dialog=$('confirm-dialog');const done=value=>{dialog.close();$('confirm-accept').onclick=null;$('confirm-cancel').onclick=null;dialog.oncancel=null;resolve(value);};$('confirm-accept').onclick=()=>done(true);$('confirm-cancel').onclick=()=>done(false);dialog.oncancel=event=>{event.preventDefault();done(false);};dialog.showModal();});}
async function runCommand(kind,payload,onStatus){if(state.busy)throw new Error('Bitte warte, bis der laufende Auftrag beendet ist.');state.busy=true;render();notice('Der Auftrag wird ausgeführt. Die übrigen Dienste bleiben verfügbar.');try{const command=await api('/api/commands',{kind,payload});for(let attempt=0;attempt<180;attempt++){await new Promise(resolve=>setTimeout(resolve,attempt<5?350:2000));const result=await api('/api/commands/'+encodeURIComponent(command.id));if(onStatus)onStatus(result.status);if(result.status==='failed')throw new Error(result.error||'Der Auftrag wurde nicht ausgeführt. Details stehen im Protokoll.');if(result.status==='success'){notice('');return result.result||{};}}throw new Error('Der Auftrag läuft noch im Hintergrund. Bitte prüfe den Status später im Protokoll, bevor du ihn erneut startest.');}finally{state.busy=false;await refresh();render();}}
$('clear-events').addEventListener('click', async () => {
  const accepted = await confirmation('Protokoll leeren?',
    'Alle bisherigen Aktivitäten werden aus dem Protokoll gelöscht, auch die durch den Filter ausgeblendeten. Neue Ereignisse erscheinen wieder hier.', 'Protokoll leeren');
  if (!accepted) return;
  try {
    await runCommand('events.clear', {confirm:true});
    toast('Das Protokoll wurde geleert.');
  } catch (error) { toast(error.message, true); }
});
async function globalPause(paused){if(!await confirmation(paused?'Alle Synchronisationen freigeben?':'Alle Synchronisationen pausieren?',paused?'Der automatische Fortschrittsabgleich wird für Bücher, Hörbücher und Podcasts wieder freigegeben. Bestehende Sicherheitsprüfungen bleiben aktiv.':'Der Fortschrittsabgleich wird für Bücher, Hörbücher und Podcasts pausiert. Audiobookshelf, BookOrbit und Jellyfin bleiben weiterhin nutzbar.',paused?'Fortsetzen':'Pausieren'))return;try{await runCommand(paused?'sync.resume':'sync.pause',paused?{}:{reason:'Manuell über die Verwaltung pausiert'});toast(paused?'Synchronisation fortgesetzt.':'Synchronisation pausiert.');}catch(error){toast(error.message,true);}}
$('pause-button').onclick=()=>globalPause(false);$('resume-button').onclick=()=>globalPause(true);
async function connectionCommand(action,profile){const actionLabel={test:'Verbindung prüfen',activate:'Verbindung aktivieren',deactivate:'Verbindung deaktivieren',delete:'Verbindung entfernen'}[action];if(action!=='test'){const description=action==='delete'?'Das gespeicherte Profil „'+profile.name+'“ wird entfernt. Daten in der Anwendung werden dadurch nicht gelöscht.':action==='activate'?'„'+profile.name+'“ wird die aktive Verbindung für '+(names[profile.kind]||profile.kind)+'. Ein anderes aktives Profil dieses Dienstes wird abgelöst. Die Konto- und Bibliotheksprüfung muss erfolgreich sein.':'Der Fortschrittsabgleich mit „'+profile.name+'“ wird deaktiviert.';if(!await confirmation(actionLabel+'?',description+(profile.kind==='ntfy'&&action==='activate'?' Dabei wird eine Testmeldung gesendet.':''),actionLabel))return;}try{const result=await runCommand('connection.'+action,{expected_revision:state.connections.revision,id:profile.id});toast(result.message||actionLabel+': abgeschlossen.');}catch(error){toast(error.message,true);}}
function editConnection(profile=null){state.editing=profile;state.editRevision=state.connections.revision;const kind=$('connection-kind');clear(kind);for(const [key,name] of Object.entries(names)){const option=element('option','',connectionSchema(key).label||name);option.value=key;kind.append(option);}kind.value=profile?.kind||'audiobookshelf';kind.disabled=Boolean(profile);$('connection-name').value=profile?.name||'';text('connection-title',profile?'Verbindung bearbeiten':'Neue Verbindung');text('connection-error','');renderFields();$('connection-dialog').showModal();}
function renderFields(){const profile=state.editing,kind=$('connection-kind').value;clear($('connection-fields'));for(const field of connectionSchema(kind).fields||[]){const label=element('label','',field.label||field.key);const input=element('input');input.dataset.key=field.key;input.dataset.type=field.type||'text';input.dataset.secret=field.secret?'1':'0';input.type=field.secret?'password':field.type==='number'?'number':'text';input.autocomplete=field.secret?'new-password':'off';input.spellcheck=false;input.maxLength=field.secret?8192:4096;const value=profile?.settings?.[field.key];if(!field.secret&&value!==undefined&&value!==null)input.value=typeof value==='object'?JSON.stringify(value):String(value);if(field.secret&&profile?.has_secrets?.[field.key])input.placeholder='Gespeichert · leer lassen zum Beibehalten';else if(field.type==='json')input.placeholder='Zum Beispiel: [1, 2]';label.append(input);const help=[field.help,field.required?'Für eine aktive Verbindung erforderlich.':null].filter(Boolean).join(' ');if(help)label.append(element('span','field-help',help));$('connection-fields').append(label);}if(kind==='bookbridge')$('connection-fields').append(element('p','help','BookBridge 7.11.0 wird unterstützt. Verwende dein persönliches BookBridge-Konto. Diese Verbindung liest dessen Paare; BookBridge selbst übernimmt den E-Book-Abgleich.'));if(kind==='pinepods')$('connection-fields').append(element('p','help','Aktiviere in PinePods den internen GPodder-Server und lege dort eine Geräte-ID für diesen Sync an. Aktuelle Versionen verwenden ein GPodder-App-Passwort. Für PinePods 0.9.0 wird zusätzlich ein persönlicher API-Schlüssel benötigt, damit abgeschlossen und erneut angefangen zuverlässig übernommen werden. Andere Versionen bleiben bis zur Prüfung gesperrt. Die Abschlussgrenze muss PinePods entsprechen (Standard: 60 Sekunden). Der Test liest nur vorhandene Geräte und Aktionen; er legt keine Abos oder Geräte an.'));if(kind==='ntfy')$('connection-fields').append(element('p','help','Beim Prüfen oder Aktivieren wird eine Testmeldung gesendet.'));}
$('add-connection').addEventListener('click',()=>editConnection());$('connection-kind').addEventListener('change',renderFields);
$('connection-form').addEventListener('submit',async event=>{event.preventDefault();text('connection-error','');const settings={},secrets={};try{for(const input of $('connection-fields').querySelectorAll('input[data-key]')){const value=input.value.trim();if(input.dataset.secret==='1'){if(value)secrets[input.dataset.key]=input.value;continue;}if(!state.editing&&!value)continue;if(input.dataset.type==='json'){if(!value){settings[input.dataset.key]=[];continue;}try{settings[input.dataset.key]=JSON.parse(value);}catch{throw new Error('Bitte gib für „'+input.closest('label').firstChild.textContent+'“ eine gültige Liste ein, zum Beispiel [1, 2].');}}else if(input.dataset.type==='number'){settings[input.dataset.key]=value?Number(value):null;if(value&&!Number.isFinite(settings[input.dataset.key]))throw new Error('Bitte prüfe die eingegebenen Zahlen.');}else settings[input.dataset.key]=value;}const payload={expected_revision:state.editRevision,name:$('connection-name').value.trim(),settings,secrets};if(state.editing)payload.id=state.editing.id;else payload.kind=$('connection-kind').value;$('connection-save').disabled=true;await runCommand(state.editing?'connection.update':'connection.create',payload);$('connection-form').reset();$('connection-dialog').close();toast('Verbindung gespeichert.');}catch(error){text('connection-error',error.message);}finally{$('connection-save').disabled=false;}});
async function previewReview(absId){if(state.busy)return;const dialog=$('review-dialog');state.review=null;text('review-title',(state.status.books||[]).find(b=>b.abs_id===absId)?.title||'Dateiprüfung');text('review-reason','Prüfung wird angefordert …');for(const id of ['review-blockers','review-differences','review-digest','review-expiry','review-error','review-files'])clear($(id));$('review-confirm').checked=false;$('review-confirm').disabled=true;$('review-approve').disabled=true;dialog.showModal();try{const result=await runCommand('review.preview',{abs_id:absId},status=>text('review-reason',status==='queued'?'Wartet auf den nächsten freien Prüfschritt …':'Dateistand wird geprüft …'));if(!dialog.open)return;state.review=result;text('review-title',result.title||'Dateistand prüfen');text('review-reason',result.reason||'Bitte prüfe die zugeordneten Dateien.');const blockers=(result.blockers||[]).map(item=>typeof item==='string'?item:item.message||describe(item));text('review-blockers',blockers.join('\n'));$('review-blockers').hidden=!blockers.length;text('review-differences',describe(result.differences)||'Der aktuelle Dateistand ist unten vollständig aufgeführt.');clear($('review-files'));const roles={source:'Original-E-Book',overlay:'Mitlese-Version',audio:'Original-Hörbuch'};for(const file of result.files||[]){const card=element('div','review-file');card.append(element('strong','',(roles[file.role]||file.role||'Datei')+' · '+bytes(file.size)),element('p','',file.path||'Pfad nicht verfügbar'),element('div','file-hash','SHA-256: '+(file.sha256||'Noch nicht verifiziert')));$('review-files').append(card);}text('review-digest','Prüfkennung: '+(result.digest||'nicht verfügbar'));text('review-expiry',result.expires_at?'Gültig bis '+formatDate(result.expires_at)+'. Änderungen an den Dateien machen diese Freigabe ungültig.':'Die Freigabe gilt ausschließlich für den hier angezeigten Dateistand.');$('review-confirm').checked=false;$('review-confirm').disabled=!result.can_approve;$('review-approve').disabled=true;text('review-error',result.can_approve?'':'Dieser Stand kann noch nicht freigegeben werden. Bitte beachte die Hinweise der Prüfung.');}catch(error){text('review-reason','Dateiprüfung nicht abgeschlossen.');text('review-error',error.message);}}
$('review-confirm').addEventListener('change',()=>{$('review-approve').disabled=!$('review-confirm').checked||!state.review?.can_approve||state.busy;});
$('review-approve').addEventListener('click',async()=>{if(!state.review?.can_approve||!$('review-confirm').checked)return;$('review-approve').disabled=true;try{await runCommand('review.approve',{review_id:state.review.review_id,digest:state.review.digest,confirm:true});$('review-dialog').close();state.review=null;toast('Der geprüfte Dateistand wurde freigegeben.');}catch(error){text('review-error',error.message);$('review-confirm').checked=false;}});
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
const folderEndpoints={worker:'Im Sync-Dienst',abs:'In Audiobookshelf',bo:'In BookOrbit',jf:'In Jellyfin',bb:'In BookBridge'};
function renderFolders(){clear($('folder-list'));text('folder-roots','Freigegebene Medien: '+(state.folders.access_roots||[]).map(root=>(root.server||'Serverpfad nicht bestätigt')+' → '+root.worker).join(' · '));$('add-folder').disabled=state.busy||state.folders.unavailable;if(!state.folders.rows?.length)empty($('folder-list'),'Ordnerverwaltung wird vorbereitet','Die freigegebenen Medienordner müssen vom Server bestätigt sein.');for(const row of state.folders.rows||[]){const card=element('article','connection-card');const head=element('div','card-heading');head.append(element('h3','',row.name),badge(row.active?'Aktiv':'Inaktiv',row.active?'good':''));card.append(head,element('p','muted',({audiobooks:'Hörbücher',ebooks:'E-Books',podcasts:'Podcasts'})[row.kind]||row.kind));const list=element('dl','folder-paths');for(const [key,label] of Object.entries({server:'Auf dem Server',...folderEndpoints})){if(row.paths[key])list.append(element('dt','',label),element('dd','',row.paths[key]));}card.append(list);if(!row.check?.ok)card.append(element('p','form-error','Der Ordner ist aktuell nicht als lesbare Medienfreigabe bestätigt.'));const actions=element('div','card-actions');actions.append(button('Bearbeiten','secondary',()=>editFolder(row)),button('Ordner prüfen','secondary',()=>folderCommand('test',row)),button(row.active?'Deaktivieren':'Aktivieren','secondary',()=>folderCommand(row.active?'deactivate':'activate',row)),button('Profil entfernen','quiet danger',()=>folderCommand('delete',row)));card.append(actions);$('folder-list').append(card);}}
function editFolder(row=null){state.editFolder=row;state.folderRevision=state.folders.revision;text('folder-title',row?'Ordnerprofil bearbeiten':'Ordnerprofil hinzufügen');$('folder-kind').value=row?.kind||'audiobooks';$('folder-kind').disabled=Boolean(row);$('folder-name').value=row?.name||'';text('folder-error','');clear($('folder-fields'));for(const [key,label] of Object.entries(folderEndpoints)){const line=element('label','',label);const input=element('input');input.dataset.path=key;input.value=row?.paths[key]||'';input.required=key==='worker';input.placeholder=key==='worker'?'/audiobooks, /ebooks oder /podcasts':'';input.autocomplete='off';input.spellcheck=false;input.maxLength=4096;line.append(input);$('folder-fields').append(line);}$('folder-dialog').showModal();}
$('add-folder').addEventListener('click',()=>editFolder());
$('folder-form').addEventListener('submit',async event=>{event.preventDefault();text('folder-error','');const paths={};for(const input of $('folder-fields').querySelectorAll('[data-path]'))paths[input.dataset.path]=input.value.trim();const payload={expected_revision:state.folderRevision,name:$('folder-name').value.trim(),paths};if(state.editFolder)payload.id=state.editFolder.id;else payload.kind=$('folder-kind').value;try{$('folder-save').disabled=true;await runCommand(state.editFolder?'folder.update':'folder.create',payload);$('folder-dialog').close();toast('Ordnerprofil gespeichert.');}catch(error){text('folder-error',error.message);}finally{$('folder-save').disabled=false;}});
async function folderCommand(action,row){if(action!=='test'&&!await confirmation('Ordnerprofil ändern?',action==='delete'?'Das Profil wird entfernt. Dateien und Docker-Freigaben bleiben bestehen.':'Der neue Zustand wird gegen die vorhandenen Zuordnungen geprüft. Die Anwendungen und Medien werden nicht verändert.','Bestätigen'))return;try{await runCommand('folder.'+action,{expected_revision:state.folders.revision,id:row.id});toast(action==='test'?'Ordner ist vorhanden und lesbar. Die Anwendungspfade werden bei der Aktivierung geprüft.':'Ordnerprofil aktualisiert.');}catch(error){toast(error.message,true);}}
function renderProvider(){const active=state.connections.mapping_provider||'bookorbit';text('provider-current','Aktiver Zuordnungsdienst: '+(names[active]||active));if(!state.providerSelected)$('provider-choice').value=active;const chosen=$('provider-choice').value;text('provider-help',chosen==='bookbridge'?'BookBridge ordnet Hörbücher und E-Books zu und gleicht sie mit seinen unterstützten Readern ab. Dieser Dienst ergänzt Audiobookshelf ↔ Jellyfin für Hörposition und Abschluss. Jellyfins E-Book-Leseposition und Read-Along-Erzeugung werden in diesem Modus nicht unterstützt. In BookBridge muss dasselbe Audiobookshelf-Konto aktiv und BookOrbit deaktiviert sein.':'BookOrbit liefert die Paare und Read-Along-Dateien. Hörposition und kompatibler E-Book-Lesefortschritt werden mit Audiobookshelf und Jellyfin abgeglichen.');$('provider-preview').disabled=state.busy;$('provider-activate').disabled=state.busy||chosen===active;}
$('provider-choice').addEventListener('change',()=>{state.providerSelected=true;$('provider-result').hidden=true;renderProvider();});
async function providerCommand(apply){const provider=$('provider-choice').value;if(apply&&!await confirmation('Zuordnungsdienst wechseln?',provider==='bookbridge'?'BookBridge übernimmt die E-Book-Zuordnung und den Abgleich mit seinen Readern. Dieser Dienst synchronisiert Hörpositionen zwischen Audiobookshelf und Jellyfin. Der direkte BookOrbit-Abgleich und Jellyfins E-Book-Abgleich ruhen in diesem Modus.':'Der bestehende BookOrbit-Abgleich wird nach erneuter Prüfung wieder verwendet. BookBridge darf dieselben Fortschritte nicht gleichzeitig schreiben.','Prüfen und aktivieren'))return;try{const result=await runCommand(apply?'provider.activate':'provider.preview',{expected_revision:state.connections.revision,provider});const preview=result.preview||{};text('provider-result',preview.message||'Prüfung erfolgreich. '+(preview.audiobooks??preview.mapped??preview.pairs?.length??0)+' Hörbuch-Zuordnungen geprüft.');$('provider-result').hidden=false;if(apply){state.providerSelected=false;renderProvider();toast('Zuordnungsdienst aktiviert.');}}catch(error){text('provider-result',error.message);$('provider-result').hidden=false;}}
$('provider-preview').addEventListener('click',()=>providerCommand(false));$('provider-activate').addEventListener('click',()=>providerCommand(true));
function podcastDraft(){return {primary_source:$('podcast-primary').value,enabled:$('podcast-enabled').checked,abs_library_id:$('podcast-abs-library').value.trim(),jellyfin_library_id:$('podcast-jf-library').value.trim(),pinepods_enabled:$('podcast-pinepods').checked,pinepods_optional_enrollment:$('podcast-pinepods-optional').checked,pinepods_variant_status_only:$('podcast-pinepods-status-only').checked,bookorbit_enabled:false};}
function renderPodcasts(){
  const config=state.connections.podcasts||{},value=state.status.podcasts||{};
  if(!state.podcastDirty){$('podcast-primary').value=config.primary_source||'auto';$('podcast-enabled').checked=config.enabled===true;$('podcast-abs-library').value=config.abs_library_id||'';$('podcast-jf-library').value=config.jellyfin_library_id||'';$('podcast-pinepods').checked=config.pinepods_enabled===true;$('podcast-pinepods-optional').checked=config.pinepods_optional_enrollment===true;$('podcast-pinepods-status-only').checked=config.pinepods_variant_status_only===true;}
  const active=config.enabled===true,blocked=(value.blocked||[]).length,pineWaiting=value.pinepods_enabled?Number(value.pine_waiting||0):0,stale=Boolean(value.at&&Date.now()-epoch(value.at)>180000);const status=!active?'Inaktiv':value.paused?'Pausiert':value.fatal?'Gesperrt':blocked?'Prüfung nötig':stale?'Status veraltet':pineWaiting?'Aktiv · PinePods teilweise offen':value.at?'Aktiv':'Warte auf Prüfung';
  text('podcast-state',status);$('podcast-state').className='badge '+(!active?'':value.fatal?'bad':blocked||value.paused||stale||pineWaiting?'warning':'good');
  text('podcast-status',value.fatal||(blocked?'Podcastfolgen sind gesperrt: '+describe(value.blocked[0].reason)+'.':null)||(!active?'Die Podcast-Einstellungen sind vorbereitet.':value.at?'Letzter Podcast-Abgleich: '+formatDate(value.at):'Ein erfolgreicher Podcast-Lauf ist noch nicht bestätigt.'));
  const pine=(state.connections.profiles||[]).some(row=>row.kind==='pinepods'&&row.active);
  text('podcast-pinepods-help',pine?(config.pinepods_enabled?'PinePods ist eingeschaltet: '+Number(value.pine_enrolled||0)+' Folgen aufgenommen, '+Number(value.pine_joining||0)+' in Aufnahmeprüfung, '+pineWaiting+' warten auf Zuordnung, '+Number(value.pine_blocked||0)+' benötigen eine Prüfung. '+(config.pinepods_optional_enrollment?'Für noch nicht aufgenommene Folgen läuft Audiobookshelf ↔ Jellyfin weiter.':'Auch neue Folgen benötigen zunächst eine passende PinePods-Zuordnung.')+' Eindeutige Laufzeitabweichungen lassen sich über den Statusabgleich behandeln.':'PinePods-Verbindung ist aktiv. Die zusätzliche Teilnahme wird erst mit den Podcast-Einstellungen eingeschaltet.')+' Andere GPodder-Apps müssen mit demselben PinePods-Konto verbunden sein.':'PinePods ist noch nicht aktiv verbunden. Füge unter „Verbindungen“ ein persönliches Profil mit bestehendem GPodder-Gerät hinzu.');
  text('podcast-primary-current','Aktuell: '+({auto:'Keine Bevorzugung',pinepods:'PinePods',abs:'Audiobookshelf',jf:'Jellyfin'}[config.primary_source||'auto']||'Keine Bevorzugung'));
  renderPodcastPrimary();
  $('podcast-preview').disabled=state.busy;$('podcast-save').disabled=state.busy;
  renderPodcastEpisodes();
}
function renderPodcastPrimary(){const pine=(state.connections.profiles||[]).some(row=>row.kind==='pinepods'&&row.active);$('podcast-primary').querySelector('[value="pinepods"]').disabled=!pine||!$('podcast-pinepods').checked;}
function podcastPeerLabel(peers){
  const labels={abs:'Audiobookshelf',jf:'Jellyfin',bo:'BookOrbit'};
  const names=peers.filter(peer=>labels[peer]).map(peer=>labels[peer]);
  if(peers.includes('pinepods')&&peers.includes('gpodder'))names.push('PinePods · GPodder');
  else if(peers.includes('pinepods'))names.push('PinePods');
  else if(peers.includes('gpodder'))names.push('GPodder (PinePods)');
  return names.join(' · ')||'Noch keine Dienste bestätigt';
}
function podcastPinePresentation(episode){
  const labels={waiting:['PinePods wartet','warning'],joining:['PinePods wird aufgenommen','warning'],enrolled:['PinePods aufgenommen','good'],blocked:['PinePods gesperrt','bad']};
  const value=labels[episode.pine_status];
  if(!value)return null;
  const statusOnly=episode.pine_mode==='status_only';
  return {label:statusOnly?value[0]+' · nur Status':episode.pine_status==='enrolled'?'Position + Status':value[0],tone:value[1],detail:[statusOnly?'Verschiedene Audiofassungen: nur angefangen/abgeschlossen, keine gemeinsame Hörposition.':'',episode.pine_status_reason||episode.pine_reason||''].filter(Boolean).join(' ')};
}
function renderPodcastEpisodes(){
  clear($('podcast-rows'));clear($('podcast-empty'));
  const all=state.status.podcasts?.episodes||[],query=$('podcast-search').value.trim().toLocaleLowerCase('de');
  const filter=$('podcast-filter').value;
  const rows=all.filter(row=>(filter==='all'||(filter==='issues'?hasIssue(row):['position','status_only'].includes(filter)?row.pine_mode===filter&&row.pine_status==='enrolled':
    !hasIssue(row)&&(row.status==='waiting'||['waiting','joining'].includes(row.pine_status))))&&[row.title,row.show].filter(Boolean).join(' ').toLocaleLowerCase('de').includes(query));
  text('podcast-count',rows.length+' von '+all.length+' Folgen · neue eindeutige Folgen werden automatisch zugeordnet');
  if(!rows.length){empty($('podcast-empty'),filter==='issues'?'Keine passenden Fehler oder Sperren':filter==='waiting'?'Keine passenden wartenden Folgen':query?'Keine passende Folge':'Noch keine Podcastfolgen zugeordnet',filter!=='all'?'Mit „Alle anzeigen“ siehst du die gesamte Liste.':query?'Versuche einen anderen Suchbegriff.':'Die Dateien müssen in beiden Bibliotheken verfügbar sein.');return;}
  const labels={ok:'Bereit',synced:'Synchronisiert',blocked:'Gesperrt',waiting:'Wartet',paused:'Pausiert',unmatched:'Noch nicht zugeordnet'};
  for(const episode of rows){
    const row=element('tr'),title=element('td','book-title');
    title.append(element('strong','',episode.title||'Unbenannte Folge'));
    if(episode.show)title.append(element('small','muted',episode.show));
    if(episode.reason&&!episode.pine_reason&&!episode.pine_status_reason)title.append(element('small','muted',describe(episode.reason)));
    const peers=Array.isArray(episode.participants)?episode.participants:Object.keys(episode.participants||{});
    const services=element('td','podcast-participants');
    services.append(element('span','',podcastPeerLabel(peers)));
    const pine=podcastPinePresentation(episode);
    if(pine){services.append(badge(pine.label,pine.tone));if(pine.detail)services.append(element('small','muted',pine.detail));}
    const status=element('td');status.append(badge(labels[episode.status]||'Nicht bestätigt',['ok','synced'].includes(episode.status)?'good':episode.status==='blocked'?'bad':'warning'));
    if(episode.status==='waiting'&&!episode.pine_status_reason&&episode.pine_status==='enrolled')status.append(element('small','muted','Der Abgleich wartet auf einen stabilen Hörstand oder das Ende einer Wiedergabe.'));const position=element('td','book-position',bookPosition(episode));
    if(episode.pine_mode==='status_only')position.append(element('small','muted','Hörposition in Audiobookshelf / Jellyfin'));
    const action=element('td','book-action');
    if(episode.can_review||episode.can_edit_status)action.append(episodeReviewButton(episode));
    row.append(title,services,status,position,action);$('podcast-rows').append(row);
  }
}

const episodePeerNames={abs:'Audiobookshelf',jf:'Jellyfin',pinepods:'PinePods',gpodder:'GPodder'};
const episodeStatusNames={not_started:'Ungehört',in_progress:'Angefangen',finished:'Abgeschlossen'};
function durationLabel(value){const seconds=Math.round(value);return Math.floor(seconds/60)+' min '+String(seconds%60).padStart(2,'0')+' s';}
function episodeReviewButton(row){
  const btn=button('Folge prüfen','secondary',()=>previewEpisode(row.id,row.can_edit_status));
  btn.disabled=state.busy;
  return btn;
}
async function previewEpisode(id,editable){
  try{
    const result=await runCommand(editable?'episode.status_preview':'episode.preview',{episode_id:id});
    state.episodeReview=result;$('episode-detail-help').hidden=Boolean(result.editable);text('episode-approve',result.editable?'Auswahl übernehmen':'Statusabgleich freigeben');
    text('episode-title',[result.show,result.title].filter(Boolean).join(' · '));
    text('episode-durations','Audiobookshelf / Jellyfin: '+durationLabel(result.duration)+(result.pine_duration?' · PinePods: '+durationLabel(result.pine_duration):' · PinePods nicht zugeordnet'));
    text('episode-mode',result.explanation|| (result.mode==='reported'?'Mit der Freigabe bestätigst du die von PinePods gemeldete Laufzeit für diese Fassung.':'Die abweichende Fassung wurde anhand der Audiodatei geprüft.'));
    const select=$('episode-source'),different=new Set(Object.values(result.states)).size>1;
    clear(select);clear($('episode-states'));text('episode-error','');
    const placeholder=element('option','','Hörstatus auswählen …');placeholder.value='';select.append(placeholder);
    for(const [peer,status] of Object.entries(result.states)){
      const name=episodePeerNames[peer],label=episodeStatusNames[status];
      $('episode-states').append(element('dt','',name),element('dd','',result.missing?.includes(peer)?'Kein gespeicherter Hörstand':label));
      if(result.sources.includes(peer)){
        const option=element('option','',name+' · '+label);option.value=peer;select.append(option);
      }
    }
    if(result.editable){for(const [v,l] of [['finished','Als gehört markieren'],['not_started','Auf 0 setzen · ungehört']]){const o=element('option','',l);o.value=v;select.append(o);}}else if(!different)select.value=result.sources[0];
    $('episode-source-label').hidden=!different&&!result.editable;
    $('episode-approve').disabled=!select.value;
    $('episode-dialog').showModal();
  }catch(error){toast(error.message,true);}
}
$('episode-source').addEventListener('change',()=>{$('episode-approve').disabled=!$('episode-source').value;});
$('episode-approve').addEventListener('click',async()=>{
  const result=state.episodeReview,source=$('episode-source').value;
  if(!result||!source||state.busy)return;
  $('episode-approve').disabled=true;
  try{
    await runCommand(result.editable?'episode.status_apply':'episode.approve',{review_id:result.review_id,digest:result.digest,source,confirm:true});
    $('episode-dialog').close();toast('Freigegeben. Der nächste Abgleich übernimmt den Hörstatus.');
  }catch(error){text('episode-error',error.message);}
});
$('audio-search').addEventListener('input',renderBooks);$('audio-filter').addEventListener('change',renderBooks);
$('book-filter').addEventListener('change',renderBooks);
$('podcast-filter').addEventListener('change',renderPodcastEpisodes);

function podcastScopeLabel(scope){if(!scope)return '';return scope.position+' Folgen mit Position + Status, '+scope.status_only+' nur Status, '+scope.waiting+' warten, '+scope.blocked+' gesperrt. '+scope.proposed+' Änderungen vorgesehen.';}
async function podcastCommand(apply){
  const settings=podcastDraft();
  let checked;
  if(apply){try{checked=await runCommand('podcast.preview',{expected_revision:state.connections.revision,settings});text('podcast-result',podcastScopeLabel(checked.preview?.scope));$('podcast-result').hidden=false;}catch(error){text('podcast-result',error.message);$('podcast-result').hidden=false;return;}}
  if(apply&&!await confirmation('Podcast-Einstellungen übernehmen?',settings.enabled?podcastScopeLabel(checked?.preview?.scope)+' Primäre Podcastquelle: '+({auto:'Keine Bevorzugung',pinepods:'PinePods',abs:'Audiobookshelf',jf:'Jellyfin'}[settings.primary_source]||settings.primary_source)+'. Bei widersprüchlichen geänderten Hörständen kann diese Auswahl vorhandene Fortschritte überschreiben. Hörposition und Abschlussstatus werden nach erneuter Prüfung zwischen den gewählten Podcast-Diensten abgeglichen. '+(settings.pinepods_optional_enrollment?'Für noch nie aufgenommene PinePods-Folgen darf Audiobookshelf ↔ Jellyfin zunächst unabhängig weiterlaufen. Bereits aufgenommene Folgen sind davon ausgenommen. ':'')+(settings.pinepods_variant_status_only?'Bei nachweislich unterschiedlichen Audiofassungen wird mit PinePods nur angefangen/abgeschlossen abgeglichen: 1 Sekunde bei zuvor ungehörten Folgen, Abschluss am jeweiligen Ende. ':'')+'Abos und Dateien bleiben unverändert. BookOrbit-Podcasts bleiben vorbereitet und inaktiv.':'Der Podcast-Abgleich wird ausgeschaltet. Gespeicherte Fortschritte bleiben erhalten. Bücher und Hörbücher laufen unabhängig weiter.','Prüfen und übernehmen'))return;
  try{const result=await runCommand(apply?'podcast.configure':'podcast.preview',{expected_revision:state.connections.revision,settings});text('podcast-result',(podcastScopeLabel(result.preview?.scope)+' ')+(result.preview?.message||'Prüfung erfolgreich: '+(result.preview?.mapped||0)+' Podcastfolgen zugeordnet.'+(result.preview?.pine_waiting?' '+result.preview.pine_waiting+' davon warten noch auf PinePods; Audiobookshelf ↔ Jellyfin kann weiterlaufen.':'')));$('podcast-result').hidden=false;if(apply){state.podcastDirty=false;renderPodcasts();toast('Podcast-Einstellungen übernommen.');}}
  catch(error){text('podcast-result',error.message);$('podcast-result').hidden=false;}
}
$('podcast-form').addEventListener('input',()=>{state.podcastDirty=true;renderPodcastPrimary();});
$('podcast-form').addEventListener('submit',event=>{event.preventDefault();podcastCommand(true);});
$('podcast-preview').addEventListener('click',()=>podcastCommand(false));
$('podcast-search').addEventListener('input',renderPodcastEpisodes);

document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden&&state.authenticated&&!state.busy)refresh();},20000);
(async()=>{navigate(location.hash.slice(1));try{if(await session())await refresh();}catch(error){showAuth();text('auth-error',error.message);}})();
