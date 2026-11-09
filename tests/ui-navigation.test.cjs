const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const http=require('node:http'), fs=require('node:fs'), path=require('node:path'), os=require('node:os');
// Run with an installed Playwright package and optional CHROME_BINARY. Never uses a saved browser profile.
const output=process.env.UI_SCREENSHOT_DIR || fs.mkdtempSync(path.join(os.tmpdir(),'sdd-navigation-'));
fs.mkdirSync(output,{recursive:true});
(async()=>{
const staticRoot=path.resolve(__dirname,'../spec_enricher/static');
const server=http.createServer((req,res)=>{const name=req.url==='/'?'index.html':req.url.replace(/^\/assets\//,'');if(!/^[a-zA-Z0-9_.-]+$/.test(name)){res.writeHead(404).end();return;}const file=path.join(staticRoot,name);if(!fs.existsSync(file)){res.writeHead(404).end();return;}res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');fs.createReadStream(file).pipe(res);});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_BINARY || undefined,args:['--disable-background-networking','--disable-component-update','--no-first-run']});
const context=await browser.newContext({viewport:{width:1280,height:900}}); const page=await context.newPage();
let delayedConnection = null;
const calls=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.route('**/*',async route=>{const u=new URL(route.request().url());if(u.origin!==origin)return route.abort();if(!u.pathname.startsWith('/api/'))return route.continue();calls.push(u.pathname);assert.equal(route.request().method(),'GET');const path=u.pathname;let data=[];
if(delayedConnection && path.endsWith('/connections')) { const pending=delayedConnection; delayedConnection=null; pending.started(); await pending.wait; }
if(path.endsWith('/session'))data={csrf_token:'fixture'};
else if(path==='/api/v1/projects')data=[{id:'fixture',name:'Navigation test',description:'Synthetic only'}];
else if(path.endsWith('/operator/network-policy'))data={allow_external_egress:false};
else if(path.endsWith('/pipeline-defaults')||path.endsWith('/source-defaults'))data={};
else if(path.endsWith('/builtin/visibility'))data={visible:true};
await route.fulfill({json:data});});
await page.goto(origin);await page.locator('#project').selectOption('fixture');
assert.deepEqual(await page.locator('nav > button').allTextContents(),['Documentation','Requirements','Analysis','Settings']);
await page.locator('[data-primary="preferences"]').click();assert.equal(await page.locator('.settings-group').count(),4);assert.equal(await page.locator('.settings-group[open]').count(),0);assert.equal(calls.filter(x=>x.endsWith('/connections')).length,0);
const model=page.locator('.settings-group').filter({has:page.locator('summary',{hasText:'Model connections'})});
await model.locator('summary').first().click();await page.getByRole('heading',{name:'LLM, LM Studio, and read-only MCP',exact:true}).waitFor();assert.equal(calls.filter(x=>x.endsWith('/connections')).length,1);
await model.locator('summary').first().click();await model.locator('summary').first().click();assert.equal(calls.filter(x=>x.endsWith('/connections')).length,1);
const sources=page.locator('.settings-group').filter({has:page.locator('summary',{hasText:'External sources: import'})});await sources.locator('summary').first().click();await page.getByRole('heading',{name:'Documentation and catalog sources',exact:true}).waitFor();assert.equal(await model.locator('h2').count(),1);
await page.screenshot({path:path.join(output,'navigation-settings-desktop.png'),fullPage:true});
// Reload preserves last selected group, aliases target the corresponding group.
await page.reload();await sources.locator('h2').waitFor();for(const alias of ['connections','sources','configuration','settings']){await page.evaluate(v=>sessionStorage.setItem('sdd-view',v),alias);await page.reload();await page.locator('.settings-group[open]').waitFor();const expected={connections:'Model connections',sources:'External sources: import',configuration:'Skills and advanced',settings:'System settings'}[alias];assert.ok((await page.locator('.settings-group[open]>summary').textContent()).includes(expected));}
// A late first-group read must not append stale groups into a different page.
let releaseRead, signalStarted;
const readStarted=new Promise(resolve=>signalStarted=resolve);
delayedConnection={started:signalStarted,wait:new Promise(resolve=>releaseRead=resolve)};
await page.evaluate(()=>sessionStorage.setItem('sdd-view','connections'));
await page.reload({waitUntil:'domcontentloaded'});await readStarted;
await page.locator('[data-primary="documents"]').click();
await page.getByRole('heading',{name:'Specification files',exact:true}).waitFor();
const readFinished=page.waitForResponse(response=>new URL(response.url()).pathname.endsWith('/connections'));
releaseRead();
await readFinished;
// Two animation frames let the awaiting old render finish, without a timed sleep.
await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
assert.equal(await page.locator('.settings-group').count(),0);
await page.getByRole('heading',{name:'Specification files',exact:true}).waitFor();
await page.locator('[data-primary="analysis"]').click();await page.getByRole('button',{name:'History and results',exact:true}).click();await page.getByRole('heading',{name:'Run history',exact:true}).waitFor();await page.screenshot({path:path.join(output,'navigation-analysis-desktop.png'),fullPage:true});await page.locator('[data-primary="documents"]').click();await page.locator('[data-primary="analysis"]').click();await page.getByRole('heading',{name:'Run history',exact:true}).waitFor();
await page.evaluate(()=>{localStorage.setItem('sdd-ui-language','ru');sessionStorage.setItem('sdd-view','preferences');sessionStorage.setItem('sdd-settings-group','connections')});await page.setViewportSize({width:390,height:844});await page.reload();await page.getByRole('heading',{name:'Модели, LM Studio и MCP только для чтения',exact:true}).waitFor();await page.screenshot({path:path.join(output,'navigation-settings-mobile-ru.png'),fullPage:true});
assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
assert.ok(await page.locator('[data-primary=preferences]').evaluate(node=>node.getBoundingClientRect().right<=innerWidth));
// System group remains available with no product; model group only gives prompt.
await page.evaluate(()=>{sessionStorage.removeItem('sdd-project');sessionStorage.setItem('sdd-view','settings')});await page.reload();await page.getByRole('heading',{name:'Перенос настроек и данные',exact:true}).waitFor();
await page.evaluate(()=>sessionStorage.setItem('sdd-view','connections'));await page.reload();await page.getByText('Выберите или создайте продукт вверху страницы для настройки этого раздела.',{exact:true}).waitFor();
// Synthetic full-workflow UI checks. All writes below are fulfilled in memory, never sent to a backend.
let uploadedDocs=[],enabledCatalogs=[],uploadRelease,uploadStarted;
const firstUpload=new Promise(resolve=>uploadStarted=resolve), uploadGate=new Promise(resolve=>uploadRelease=resolve);
let uploadCount=0, preflightRequest;
const fixtureCatalog={id:'cat',catalog_id:'synthetic',version:'1',records:1,enabled:true};
const fixtureRun={id:'run',status:'partial',created_at:'2026-11-09',stages:[],error_code:null};
const fixtureReport={matrix:Array.from({length:64},(_,i)=>({requirement_id:`REQ-${i}`,assessment:{assessment:'unknown',applicability:'unknown',reason:'Synthetic evidence pending'}})),findings:[],unknown_requirements:64,incomplete:true,review_revision:0,questions:[],scope_confirmations:Array.from({length:12},(_,i)=>({requirement_id:`REQ-${i}`,reason:'review_scope_exclusion_requires_confirmation',question:`Scope ${i}`,facts:[{predicate_id:'p',predicate_text:'Synthetic condition?',value:false}]}))};
await page.route('**/api/v1/**',async route=>{const req=route.request(),u=new URL(req.url()),p=u.pathname;let data=[];
if(p.endsWith('/session'))data={csrf_token:'fixture'};
else if(p==='/api/v1/projects')data=[{id:'fixture',name:'Synthetic workflow'}];
else if(p.endsWith('/documents') && req.method()==='POST') { uploadCount++;if(uploadCount===1){uploadStarted();await uploadGate;}assert.equal(u.searchParams.get('role'),'master_spec');data={id:`doc${uploadCount}`,name:u.searchParams.get('filename'),role:'master_spec',sha256:'a'.repeat(64),size:req.postDataBuffer().length,active:true};uploadedDocs.push(data); }
else if(p.endsWith('/documents'))data=uploadedDocs;
else if(p.endsWith('/catalogs'))data=enabledCatalogs;
else if(p==='/api/v1/builtin-catalog')data={catalog_id:'synthetic',catalog_version:'1',records:1,active_records:1,sha256:'hash'};
else if(p.endsWith('/builtin-catalog') && req.method()==='POST'){enabledCatalogs=[fixtureCatalog];data=fixtureCatalog;}
else if(p==='/api/v1/builtin-catalog/coverage')data={domains:[]};
else if(p.endsWith('/builtin/visibility'))data={hidden:false};
else if(p.endsWith('/connections'))data=[{id:'model',revision:1,content:{kind:'llm',enabled:true,name:'Synthetic disconnected model'}}];
else if(p.endsWith('/runs/preflight')){preflightRequest=req.postDataJSON();data={active_requirements:1,plan_sha256:'plan',destinations:[],future_payloads:'Synthetic only',first_payload_preview:{}};}
else if(p.endsWith('/runs'))data=[fixtureRun,{...fixtureRun,id:'oldrun'}];
else if(p.endsWith('/runs/run'))data=fixtureRun;
else if(p.endsWith('/manifest'))data={request:{mode:'review_documents',review_document_ids:['doc1','doc2'],catalog_ids:['cat'],connection:{id:'model',revision:1}}};
else if(p.endsWith('/report'))data=fixtureReport;
else assert.equal(req.method(),'GET',`Unexpected write ${p}`);
await route.fulfill({json:data});});
await page.evaluate(()=>{localStorage.setItem('sdd-ui-language','en');sessionStorage.setItem('sdd-project','fixture');sessionStorage.setItem('sdd-view','documents');});await page.setViewportSize({width:1280,height:900});await page.reload();
await page.locator('#document-files').setInputFiles([{name:'first.md',mimeType:'text/markdown',buffer:Buffer.from('# First')},{name:'second.md',mimeType:'text/markdown',buffer:Buffer.from('# Second')}]);
await page.getByRole('button',{name:'Upload documents',exact:true}).click();await firstUpload;assert.equal(await page.locator('#upload-role').isDisabled(),true);assert.equal(await page.locator('#document-files').isDisabled(),true);uploadRelease();await page.getByRole('heading',{name:'second.md',exact:true}).waitFor();assert.equal(uploadedDocs.length,2);
await page.getByRole('button',{name:'Continue: add requirements',exact:true}).click();await page.getByRole('button',{name:'Add all built-in requirements',exact:true}).waitFor();assert.equal(await page.evaluate(()=>document.activeElement.id),'main');
await page.getByRole('button',{name:'Add all built-in requirements',exact:true}).click();await page.getByRole('button',{name:'Built-in requirements added',exact:true}).waitFor();
await page.locator('[data-primary="analysis"]').click();await page.getByRole('button',{name:'New review',exact:true}).click();await page.getByRole('button',{name:'Check readiness and review data access',exact:true}).click();await page.getByRole('button',{name:'Start document review',exact:true}).waitFor();assert.deepEqual(preflightRequest.review_document_ids,['doc1','doc2']);assert.deepEqual(preflightRequest.catalog_ids,['cat']);
await page.locator('input[type=checkbox]').first().uncheck();assert.equal(await page.getByRole('button',{name:'Start document review',exact:true,includeHidden:true}).isDisabled(),true);
await page.getByRole('button',{name:'History and results',exact:true}).click();await page.getByText('Optional: compare previous runs',{exact:true}).waitFor();assert.equal(await page.locator('details').first().getAttribute('open'),null);await page.getByRole('button',{name:'Open results',exact:true}).first().click();await page.getByRole('heading',{name:'Document review report',exact:true}).waitFor();
assert.ok(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight*4));assert.equal(await page.locator('#back-to-top').isVisible(),false);
await page.getByRole('button',{name:'Next',exact:true}).click();assert.equal(await page.getByRole('button',{name:'Next',exact:true}).isDisabled(),true);await page.getByRole('button',{name:'Previous',exact:true}).click();assert.equal(await page.getByRole('button',{name:'Previous',exact:true}).isDisabled(),true);
const summary=page.locator('summary').filter({hasText:'REQ-0 · Unknown'}).first();await summary.focus();await page.keyboard.press('Enter');assert.equal(await summary.locator('..').getAttribute('open'),'');await page.keyboard.press('Space');assert.equal(await summary.locator('..').getAttribute('open'),null);
await page.setViewportSize({width:320,height:800});await page.emulateMedia({reducedMotion:'reduce'});await page.evaluate(()=>scrollTo(0,document.documentElement.scrollHeight));await page.locator('#back-to-top').waitFor({state:'visible'});await page.locator('#back-to-top').focus();assert.ok(await page.locator('#back-to-top').evaluate(e=>e.getBoundingClientRect().bottom<=innerHeight+1));assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(output,'workflow-report-bottom-320.png'),fullPage:false});
await page.locator('#back-to-top').click();assert.equal(await page.evaluate(()=>document.activeElement.id),'main');await page.locator('#back-to-top').waitFor({state:'hidden'});
await page.evaluate(()=>scrollTo(0,document.documentElement.scrollHeight));await page.getByRole('button',{name:'Versions and manifest',exact:true}).click();await page.locator('#details[open]').waitFor();assert.equal(await page.locator('#back-to-top').isVisible(),false);await page.keyboard.press('Escape');
await page.locator('[data-primary="documents"]').click();await page.getByRole('heading',{name:'Specification files',exact:true}).waitFor();assert.equal(await page.locator('#back-to-top').isVisible(),false);
await page.evaluate(()=>{localStorage.setItem('sdd-ui-language','ru');sessionStorage.setItem('sdd-view','history')});await page.setViewportSize({width:390,height:844});await page.reload();await page.getByRole('button',{name:'Открыть результаты',exact:true}).first().click();await page.getByRole('heading',{name:'Отчёт о проверке документов',exact:true}).waitFor();await page.evaluate(()=>scrollTo(0,document.documentElement.scrollHeight));await page.getByRole('link',{name:'Наверх к содержимому',exact:true}).waitFor({state:'visible'});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(output,'workflow-report-bottom-390-ru.png'),fullPage:false});
console.log('PASS synthetic workflow: two-file upload lock/snapshot, requirements activation, preflight selections/stale invalidation (no start), history/report, keyboard disclosures, pager disabled state, long-report320/reduced-motion/footer-focus, dialog hide, view focus.');

assert.deepEqual(errors,[]);console.log('PASS isolated headless: four primary views, lazy read-only groups, one-load/reopen, sibling preservation, delayed-read navigation isolation, aliases, reload state, history return, RU390 no overflow, system without product.');await browser.close();server.close();
})().catch(e=>{console.error(e);process.exit(1)});
