const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const html = fs.readFileSync('frontend/index.html','utf8');
const source = fs.readFileSync('frontend/books.js','utf8');
new vm.Script(source);
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
assert.equal(new Set(ids).size,ids.length,'Duplicate IDs');
assert.ok(html.includes('<script src="./books.js"></script>'));
assert.ok(html.includes('<link rel="stylesheet" href="./books.css">'));
class Element {
  constructor(tag='div') {this.tagName=tag;this.children=[];this.dataset={};this.attributes={};this.listeners={};this.value='';this.disabled=false;this.hidden=false;this.textContent='';this.className='';}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];delete this[name];}
  addEventListener(event,fn){this.listeners[event]=fn;}
  querySelectorAll(tag){return this.children.flatMap(child=>[...(child.tagName===tag?[child]:[]),...child.querySelectorAll(tag)]);}
  focus(){this.focused=true;}
  scrollIntoView(){}
  reportValidity(){return true;}
}
function setup(saved='{}',storageThrows=false,session=null,sharedServer=null){
  const elements=Object.fromEntries(ids.map(id=>[id,new Element()]));
  for(const match of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*\bhidden[^>]*>/g))elements[match[1]].hidden=true;
  elements.books.classList={contains(){return true;}};
  let stored=saved, sessionValue=session, now=Date.now();
  const requests=[], intervals=[];
  const server=sharedServer || {orders:new Map(),count:0};
  let mode='ok';
  const document={hidden:false,addEventListener(){},getElementById(id){assert.ok(elements[id],id);return elements[id];},createElement:tag=>new Element(tag)};
  const context=vm.createContext({document, crypto,
    Date:class extends Date {static now(){return now;}},
    window:{APP_CONFIG:{backendUrl:'https://backend.example/'}},
    localStorage:{getItem(){if(storageThrows)throw Error('blocked');return stored;},setItem(key,value){if(storageThrows)throw Error('blocked');stored=value;}},
    sessionStorage:{getItem(){if(storageThrows)throw Error('blocked');return sessionValue;},setItem(key,value){if(storageThrows)throw Error('blocked');sessionValue=value;},removeItem(){sessionValue=null;}},
    AbortController,setTimeout,clearTimeout,setInterval(fn,ms){intervals.push({fn,ms});return intervals.length;},
    async fetch(url,options){
      const body=options.body?JSON.parse(options.body):undefined;
      requests.push({url,body,headers:options.headers,method:options.method});
      if(mode==='offline')throw Error('offline');
      if(mode==='conflict' && options.method==='POST')return {ok:false,status:409,json:async()=>({detail:'Different retry payload'})};
      if(mode==='invalid')return {ok:false,status:422,json:async()=>({detail:'invalid'})};
      if(mode==='unexpected')return {ok:true,json:async()=>({payment_status:'paid'})};
      const token=body?.idempotency_key || options.headers.Authorization?.slice(7);
      let order=server.orders.get(token);
      if(url.endsWith('/api/book-orders') && options.method==='POST'){
        if(!order){
          server.count++;
          order={order_id:'BSJ-20261006-'+crypto.randomBytes(12).toString('hex').toUpperCase(),
            payment_status:'pending_payment',payment_method:'promptpay',currency:'THB',created_at:new Date(now).toISOString(),expires_at:new Date(now+1800000).toISOString(),paid_at:null,payment_reference:null,
            total_amount:body.items.reduce((sum,item)=>sum+item.quantity*300,0),
            items:body.items.map(item=>({book_id:item.book_id,title:item.book_id,quantity:item.quantity,unit_price:300,line_total:item.quantity*300}))};
          server.orders.set(token,order);
        }
        if(mode==='lostResponse')throw Error('connection lost after commit');
      }
      if(!order)return {ok:false,status:404,json:async()=>({detail:'Not found'})};
      if(['pending_payment','awaiting_verification'].includes(order.payment_status) && now>=Date.parse(order.expires_at))order.payment_status='expired';
      let result=order;
      if(url.endsWith('/payment')){
        if(['paid','cancelled','expired'].includes(order.payment_status))return {ok:false,status:409,json:async()=>({detail:'expired'})};
        order.payment_status='awaiting_verification';
        result={order,qr_image:'data:image/svg+xml;base64,PHN2Zy8+',merchant_name:'TEST ONLY'};
      }
      return {ok:true,json:async()=>JSON.parse(JSON.stringify(result))};
    }});
  vm.runInContext(source,context);
  return {elements,requests,server,document,intervals,stored:()=>stored,session:()=>sessionValue,setMode:value=>{mode=value;},advance:ms=>{now+=ms;}};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function texts(element){return [element.textContent,...element.children.map(texts)].join(' ');}
function buttonByLabel(root,prefix){return root.querySelectorAll('button').find(button=>button.attributes['aria-label']?.startsWith(prefix));}
function click(button){assert.ok(button);assert.equal(button.disabled,false);return button.listeners.click();}
async function submit(state){await state.elements.bookBuyerForm.listeners.submit({preventDefault(){}});}
// Execute the actual navigation logic with all existing and new tab/panel IDs.
const inline=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match=>match[1]).find(text=>text.includes('function showTab'));
function navElement(id,tab){return {id,dataset:{tab},active:false,listeners:{},classList:{toggle(name,active){this.owner.active=active;}},addEventListener(name,fn){this.listeners[name]=fn;}};}
const tabs=[...html.matchAll(/class="tab(?: active)?" data-tab="([^"]+)"/g)].map(match=>navElement(null,match[1]));
const panels=[...html.matchAll(/<section id="([^"]+)" class="panel/g)].map(match=>navElement(match[1]));
for(const element of [...tabs,...panels])element.classList.owner=element;
const navContext=vm.createContext({document:{querySelectorAll:selector=>selector==='.tab'?tabs:selector==='.panel'?panels:[]},window:{scrollTo(){}}});
vm.runInContext(inline.slice(inline.indexOf('  const tabs'),inline.indexOf('  const consultationForm')),navContext);
assert.deepEqual(tabs.map(tab=>tab.dataset.tab),['home','psychologist','books','donation','consultation']);
for(const tab of tabs){tab.listeners.click();assert.equal(panels.filter(panel=>panel.active).length,1);assert.equal(panels.find(panel=>panel.active).id,tab.dataset.tab);}
(async()=>{
  const state=setup();const e=state.elements;
  assert.equal(e.bookCatalog.children.length,5);
  assert.equal(e.bookCheckoutButton.disabled,true);
  for(const card of e.bookCatalog.children){
    assert.ok(texts(card).includes('300 บาท'));
    assert.ok(texts(card).includes('รศ.ดร. จันทร์เพ็ญ ภูโสภา'));
    assert.ok(card.children[0].children[0].children.some(child=>child.innerHTML?.includes('<svg')));
  }
  const addButtons=e.bookCatalog.querySelectorAll('button');
  click(addButtons[0]);click(addButtons[0]);click(addButtons[1]);
  assert.equal(e.bookCartTotal.textContent,'900 บาท');
  assert.equal(e.bookCartCount.textContent,'3 เล่ม');
  assert.deepEqual(JSON.parse(state.stored()),{guidance:2,health:1});
  assert.equal(setup(state.stored()).elements.bookCartTotal.textContent,'900 บาท');
  click(buttonByLabel(e.bookCartItems,'ลดจำนวน'));assert.equal(e.bookCartTotal.textContent,'600 บาท');
  click(buttonByLabel(e.bookCartItems,'เพิ่มจำนวน'));assert.equal(e.bookCartTotal.textContent,'900 บาท');
  click(buttonByLabel(e.bookCartItems,'ลบ'));assert.equal(e.bookCartTotal.textContent,'300 บาท');
  click(e.bookCheckoutButton);assert.equal(e.bookCheckout.hidden,false);
  e.bookBuyerName.value='Reader';e.bookBuyerEmail.value='reader@example.com';
  // Network uncertainty must reuse the same key; double clicks cannot duplicate orders.
  state.setMode('lostResponse');await submit(state);
  assert.equal(state.server.count,1);assert.equal(e.bookBuyerForm.hidden,false);
  const savedToken=state.session();assert.match(savedToken,/^[0-9a-f-]{36}$/);
  state.setMode('ok');await Promise.all([submit(state),submit(state)]);
  assert.equal(state.server.count,1);
  assert.equal(state.requests[0].body.idempotency_key,state.requests[1].body.idempotency_key);
  assert.ok(!('total' in state.requests[0].body));
  assert.equal(e.bookPaymentState.textContent,'รอการชำระเงิน');
  assert.equal(e.bookBuyerForm.hidden,true);
  assert.equal(e.bookQrArea.hidden,true);
  assert.ok(!state.stored().includes('Reader') && !state.session().includes('Reader'));
  const frozenSummary=texts(e.bookCheckoutSummary);
  click(addButtons[2]);assert.equal(e.bookCartTotal.textContent,'600 บาท');
  assert.equal(texts(e.bookCheckoutSummary),frozenSummary,'Created order items must stay frozen');
  const restored=setup(state.stored(),false,state.session(),state.server);await flush();
  assert.equal(restored.elements.bookPaymentState.textContent,'รอการชำระเงิน');
  assert.equal(restored.server.count,1);
  await click(e.bookPromptPayButton);
  assert.equal(e.bookPaymentState.textContent,'รอตรวจสอบการชำระเงิน');
  assert.equal(e.bookQrArea.hidden,false);
  assert.ok(e.bookQrImage.src.startsWith('data:image/svg+xml;base64,'));
  assert.equal(state.requests.at(-1).headers.Authorization,`Bearer ${savedToken}`);
  // No status polling while hidden; no fake payment on a timer or page return.
  const poll=state.intervals.find(timer=>timer.ms===15000).fn;
  state.document.hidden=true;const before=state.requests.length;poll();assert.equal(state.requests.length,before);
  state.document.hidden=false;await poll();await flush();
  assert.equal(e.bookPaymentState.textContent,'รอตรวจสอบการชำระเงิน');
  // Local timeout hides QR immediately, even before a server status round-trip.
  state.advance(1800001);state.intervals.find(timer=>timer.ms===1000).fn();
  assert.equal(e.bookQrArea.hidden,true);assert.equal(e.bookPromptPayButton.disabled,true);
  assert.ok(e.bookPaymentState.textContent.includes('หมดเวลา'));
  await click(e.bookRefreshStatus);assert.equal(e.bookPaymentState.textContent,'หมดเวลาการชำระเงิน');
  assert.equal(e.bookNewOrder.hidden,false);
  click(e.bookNewOrder);assert.equal(state.session(),null);assert.equal(e.bookBuyerForm.hidden,false);
  e.bookBuyerName.value='Reader';e.bookBuyerEmail.value='reader@example.com';await submit(state);
  assert.equal(state.server.count,2);assert.notEqual(state.session(),savedToken);
  // Only a server status response may show paid; this is a trusted test fixture.
  state.server.orders.get(state.session()).payment_status='paid';await click(e.bookRefreshStatus);
  assert.equal(e.bookPaymentState.textContent,'ชำระเงินแล้ว');assert.equal(e.bookQrArea.hidden,true);
  const conflict=setup('{"health":1}');
  conflict.elements.bookBuyerName.value='Reader';conflict.elements.bookBuyerEmail.value='reader@example.com';
  conflict.setMode('lostResponse');await submit(conflict);
  conflict.setMode('conflict');conflict.elements.bookBuyerName.value='Changed';await submit(conflict);
  assert.equal(conflict.server.count,1);assert.equal(conflict.elements.bookBuyerForm.hidden,true);
  assert.equal(conflict.elements.bookPaymentState.textContent,'รอการชำระเงิน');
  const failing=setup('{"health":1}');failing.elements.bookBuyerName.value='Reader';failing.elements.bookBuyerEmail.value='reader@example.com';
  for(const mode of ['offline','invalid','unexpected']){
    failing.setMode(mode);await submit(failing);
    assert.equal(failing.elements.bookPaymentPanel.hidden,true);
    assert.equal(failing.elements.bookCartTotal.textContent,'300 บาท');
    assert.equal(failing.elements.bookBuyerFields.disabled,false);
  }
  while(failing.elements.bookCartItems.querySelectorAll('button').length)click(buttonByLabel(failing.elements.bookCartItems,'ลบ'));
  assert.equal(failing.elements.bookCheckoutButton.disabled,true);
  for(const saved of ['broken','null','[]','{"unknown":2,"guidance":-1,"health":"2"}'])assert.equal(setup(saved).elements.bookCartTotal.textContent,'0 บาท');
  const blocked=setup('{}',true);click(blocked.elements.bookCatalog.querySelectorAll('button')[0]);assert.equal(blocked.elements.bookCartTotal.textContent,'300 บาท');
  blocked.elements.bookBuyerName.value='Reader';blocked.elements.bookBuyerEmail.value='reader@example.com';await submit(blocked);assert.equal(blocked.requests.length,0);
  const max=setup('{"health":99}');assert.equal(max.elements.bookCatalog.querySelectorAll('button')[1].disabled,true);
  assert.equal(buttonByLabel(max.elements.bookCartItems,'เพิ่มจำนวน').disabled,true);
  console.log('Books frontend checks passed: catalog/nav/cart, frozen orders, retries/recovery, QR unpaid state, authenticated useful polling, expiry, error recovery.');
})().catch(error=>{console.error(error);process.exitCode=1;});
