/* Cart stores only catalog IDs and quantities, never buyer details. */
(() => {
  'use strict';
  const BOOKS = [
  {
    "id": "guidance",
    "title": "หนังสือจิตวิทยาและการแนะแนวสำหรับครู",
    "category": "จิตวิทยา • การแนะแนว",
    "tagline": "เข้าใจผู้เรียน ส่งเสริมการเติบโต",
    "description": "นำเสนอแนวคิดทางจิตวิทยาและหลักการแนะแนวสำหรับครู เพื่อเป็นพื้นฐานในการทำความเข้าใจผู้เรียนและส่งเสริมพัฒนาการอย่างเหมาะสม",
    "theme": "sage",
    "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
    "price": 300
  },
  {
    "id": "health",
    "title": "หนังสือจิตวิทยาสุขภาพ",
    "category": "จิตวิทยา • สุขภาวะ",
    "tagline": "เรียนรู้ความสัมพันธ์ของกายและใจ",
    "description": "ศึกษาความสัมพันธ์ระหว่างปัจจัยทางจิตใจ พฤติกรรม และสุขภาพ เหมาะสำหรับผู้สนใจแนวคิดการดูแลตนเองและการส่งเสริมสุขภาวะ",
    "theme": "rose",
    "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
    "price": 300
  },
  {
    "id": "counseling",
    "title": "หนังสือทฤษฎีการให้คำปรึกษา",
    "category": "จิตวิทยา • การให้คำปรึกษา",
    "tagline": "รากฐานของการรับฟังและความเข้าใจ",
    "description": "รวบรวมแนวคิดพื้นฐานของทฤษฎีการให้คำปรึกษา เพื่อสนับสนุนการเรียนรู้และการพิจารณาแนวทางช่วยเหลือที่สอดคล้องกับบริบทของผู้รับบริการ",
    "theme": "lavender",
    "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
    "price": 300
  },
  {
    "id": "learning",
    "title": "หนังสือทฤษฎีทางจิตวิทยาที่ใช้จัดการเรียนการสอน",
    "category": "จิตวิทยา • การเรียนรู้",
    "tagline": "เชื่อมทฤษฎีสู่การเรียนรู้ในห้องเรียน",
    "description": "อธิบายแนวคิดทางจิตวิทยาที่เกี่ยวข้องกับการเรียนรู้ และการประยุกต์ใช้ในการจัดการเรียนการสอน เหมาะสำหรับครูและผู้สนใจการออกแบบการเรียนรู้",
    "theme": "sand",
    "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
    "price": 300
  },
  {
    "id": "skills",
    "title": "หนังสือกระบวนการขั้นตอนเทคนิคและทักษะการให้คำปรึกษา",
    "category": "จิตวิทยา • ทักษะการปรึกษา",
    "tagline": "เรียนรู้อย่างเป็นขั้นตอน ฝึกทักษะอย่างเข้าใจ",
    "description": "นำเสนอกระบวนการ ขั้นตอน เทคนิค และทักษะพื้นฐานในการให้คำปรึกษา เพื่อใช้ประกอบการศึกษาและพัฒนาความเข้าใจในการปฏิบัติงานอย่างเป็นระบบ",
    "theme": "sky",
    "author": "รศ.ดร. จันทร์เพ็ญ ภูโสภา",
    "price": 300
  }
];
  const CART_KEY = 'speak-grow-books-v1';
  const MAX_QUANTITY = 99;
  const byId = new Map(BOOKS.map(book => [book.id, book]));
  const el = id => document.getElementById(id);
  const money = value => `${value.toLocaleString('th-TH')} บาท`;
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    if(className)result.className=className;
    if(text!==undefined)result.textContent=text;
    return result;
  };
  let cart = {};
  let busy = false;
  let activeOrder = null;
  let orderToken = null;
  let recovering = false;
  let polling = false;
  let qrOrderId = null;
  const ORDER_KEY = 'speak-grow-order-session-v1';
  const terminal = status => ['paid','cancelled','expired'].includes(status);
  const statusText = {pending_payment:'รอการชำระเงิน',awaiting_verification:'รอตรวจสอบการชำระเงิน',paid:'ชำระเงินแล้ว',cancelled:'ยกเลิก',expired:'หมดเวลาการชำระเงิน'};
  try{
    const saved = sessionStorage.getItem(ORDER_KEY);
    if(saved && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(saved))orderToken=saved;
  }catch(_){/* Cart still works without session storage. Checkout explains the requirement. */}
  try{
    const saved = JSON.parse(localStorage.getItem(CART_KEY) || '{}');
    if(saved && typeof saved==='object' && !Array.isArray(saved)){
      for(const book of BOOKS){
        if(Number.isInteger(saved[book.id]) && saved[book.id]>0 && saved[book.id]<=MAX_QUANTITY)cart[book.id]=saved[book.id];
      }
    }
  }catch(_){/* Storage unavailable or malformed: keep a working in-memory cart. */}
  const lines = () => BOOKS.filter(book => cart[book.id]).map(book => ({...book,quantity:cart[book.id]}));
  const count = () => lines().reduce((sum,book) => sum+book.quantity,0);
  const total = () => lines().reduce((sum,book) => sum+book.quantity*book.price,0);
  function saveCart(){
    try{localStorage.setItem(CART_KEY,JSON.stringify(cart));}
    catch(_){el('booksNotice').textContent='ตะกร้าใช้งานได้ในหน้านี้ แต่เบราว์เซอร์ไม่อนุญาตให้บันทึกไว้หลังรีเฟรช';}
  }
  // Decorative SVG motifs share the same geometry and line weight across the series.
  const motifs = [
    '<path d="M35 62V20q25-9 45 6v42q-20-15-45-6Zm90 0V20q-25-9-45 6v42q20-15 45-6Z"/><path d="M80 26v42M80 18C55 8 63 0 80 8c17-8 25 0 0 10Z"/>',
    '<path d="M80 66 44 35C15 8 62-8 80 17c18-25 65-9 36 18Z"/><path d="M46 40h20l8-17 12 29 9-12h19"/>',
    '<path d="M26 10h72v36H62L43 60V46H26ZM104 27h30v36h-13v12l-18-12H72V52"/><path d="M41 23h43M41 34h31"/>',
    '<path d="m25 34 55-25 55 25-55 25ZM45 45v18q35 20 70 0V45M135 34v33"/><path d="M33 76h94"/>',
    '<path d="M24 64h28V44h28V25h28V8h28M33 19l13 12 23-23"/><circle cx="120" cy="55" r="14"/><path d="m113 55 5 5 10-11"/>'
  ];
  function renderCatalog(){
    el('bookCatalog').replaceChildren(...BOOKS.map((book,index) => {
      const article=node('article','book-card');
      const wrap=node('div','book-cover-wrap');
      const cover=node('div',`book-cover ${book.theme}`);
      cover.setAttribute('aria-hidden','true'); // Full title and author repeated below.
      const top=node('div','book-cover-top');
      top.append(node('span','',book.category),node('span','book-cover-number',String(index+1).padStart(2,'0')));
      const art=node('div','book-cover-art');
      art.innerHTML=`<svg viewBox="0 0 160 84" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true">${motifs[index]}</svg>`;
      cover.append(top,node('h3','',book.title),node('p','book-cover-tagline',book.tagline),art,node('p','book-cover-author',book.author),node('span','book-cover-price','300 บาท'));
      wrap.append(cover);
      const details=node('div','book-details');
      const buy=node('div','book-buy-row');
      const button=node('button','btn primary','เพิ่มลงตะกร้า');
      button.type='button';button.dataset.bookAdd=book.id;
      button.setAttribute('aria-label',`เพิ่มลงตะกร้า ${book.title}`);
      button.addEventListener('click',() => changeQuantity(book.id,(cart[book.id]||0)+1));
      buy.append(node('strong','','300 บาท'),button);
      details.append(node('h3','',book.title),node('p','book-author',book.author),node('p','book-description',book.description),buy);
      article.append(wrap,details);
      return article;
    }));
  }
  function renderSummary(items=lines(),amount=total()){
    const summary=el('bookCheckoutSummary');
    summary.replaceChildren(...items.map(book => {
      const row=node('div','books-summary-line');
      row.append(node('span','',`${book.title} × ${book.quantity}`),node('span','',money(book.line_total ?? book.price*book.quantity)));
      return row;
    }));
    const grand=node('div','books-total');
    grand.append(node('span','','ยอดชำระทั้งหมด'),node('strong','',money(amount)));
    summary.append(grand);
  }
  function renderCart(){
    el('bookCartItems').replaceChildren(...lines().map(book => {
      const row=node('div','books-cart-row');
      const controls=node('div','books-cart-controls');
      const minus=node('button','','−'),plus=node('button','','+'),remove=node('button','book-remove','ลบ');
      for(const button of [minus,plus,remove])button.type='button';
      minus.setAttribute('aria-label',`ลดจำนวน ${book.title}`);
      plus.setAttribute('aria-label',`เพิ่มจำนวน ${book.title}`);
      remove.setAttribute('aria-label',`ลบ ${book.title}`);
      plus.disabled=book.quantity>=MAX_QUANTITY;
      minus.addEventListener('click',() => changeQuantity(book.id,book.quantity-1));
      plus.addEventListener('click',() => changeQuantity(book.id,book.quantity+1));
      remove.addEventListener('click',() => changeQuantity(book.id,0));
      controls.append(minus,node('span','',`จำนวน ${book.quantity}`),plus,remove,node('span','books-line-total',money(book.price*book.quantity)));
      row.append(node('h4','',book.title),controls);return row;
    }));
    if(!count())el('bookCartItems').append(node('p','small','ตะกร้ายังว่าง เลือกหนังสือที่สนใจเพื่อเริ่มต้น'));
    el('bookCartCount').textContent=`${count()} เล่ม`;
    el('booksCartBadge').textContent=String(count());
    el('bookCartTotal').textContent=money(total());
    el('bookCheckoutButton').disabled=(!count() && !activeOrder) || busy || recovering;
    el('bookBuyerFields').disabled=busy || recovering || Boolean(activeOrder);
    for(const button of el('bookCatalog').querySelectorAll('button'))button.disabled=busy || (cart[button.dataset.bookAdd]||0)>=MAX_QUANTITY;
    if(busy)for(const button of el('bookCartItems').querySelectorAll('button'))button.disabled=true;
  }
  function changeQuantity(id,quantity){
    if(busy || !byId.has(id) || !Number.isInteger(quantity) || quantity<0 || quantity>MAX_QUANTITY)return;
    if(quantity)cart[id]=quantity;else delete cart[id];
    el('booksNotice').textContent=`อัปเดตตะกร้าแล้ว จำนวน ${count()} เล่ม`;
    saveCart();
    renderCart();
    if(activeOrder){renderOrder(activeOrder);return;}
    renderSummary();
    el('bookPaymentPanel').hidden=true;
    el('bookOrderStatus').textContent='รายการเปลี่ยนแปลง กรุณาตรวจสอบสรุปและส่งข้อมูลอีกครั้ง';
    if(!count())el('bookCheckout').hidden=true;
  }
  function ensureOrderToken(){
    if(!orderToken){
      if(!globalThis.crypto?.randomUUID)throw new Error('กรุณาเปิดเว็บไซต์ผ่าน HTTPS เพื่อสร้างคำสั่งซื้ออย่างปลอดภัย');
      orderToken=crypto.randomUUID();
    }
    try{sessionStorage.setItem(ORDER_KEY,orderToken);}
    catch(_){throw new Error('กรุณาอนุญาตพื้นที่จัดเก็บของเบราว์เซอร์ก่อนสร้างคำสั่งซื้อ เพื่อให้กลับมาตรวจสอบรายการเดิมได้');}
    return orderToken;
  }
  function validateOrder(order){
    if(!order || !/^BSJ-\d{8}-[A-F0-9]{24}$/.test(order.order_id) || !Object.hasOwn(statusText,order.payment_status)
       || !Array.isArray(order.items) || !order.items.length || !Number.isInteger(order.total_amount)
       || order.total_amount<=0 || !Number.isFinite(Date.parse(order.expires_at)))throw new Error('ข้อมูลคำสั่งซื้อไม่ถูกต้อง กรุณาลองตรวจสอบสถานะอีกครั้ง');
    return order;
  }
  async function orderRequest(path,method='GET',body){
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),15000);
    try{
      const base=window.APP_CONFIG?.backendUrl?.replace(/\/$/,'') || 'http://localhost:8000';
      const headers={'Content-Type':'application/json'};
      if(orderToken)headers.Authorization=`Bearer ${orderToken}`;
      const response=await fetch(`${base}${path}`,{method,headers,body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal,cache:'no-store'});
      const result=await response.json();
      if(!response.ok){
        const error=new Error(typeof result.detail==='string'?result.detail:'กรุณาตรวจสอบข้อมูลและลองใหม่');
        error.status=response.status;throw error;
      }
      return result;
    }finally{clearTimeout(timeout);}
  }
  function showOrderError(error){
    el('bookOrderStatus').textContent=(error.status || error.message?.startsWith('กรุณา')) ? error.message : 'ยังไม่ทราบผลคำขอ กรุณาลองใหม่ ระบบจะใช้คำขอเดิมเพื่อไม่สร้างคำสั่งซื้อซ้ำ';
  }
  function expiredLocally(){return activeOrder && Date.now()>=Date.parse(activeOrder.expires_at);}
  function updateDeadline(){
    if(!activeOrder)return;
    const expired=expiredLocally() && !terminal(activeOrder.payment_status);
    if(expired || terminal(activeOrder.payment_status)){
      el('bookQrArea').hidden=true;
      el('bookQrImage').removeAttribute('src');
      qrOrderId=null;
    }
    el('bookPromptPayButton').disabled=busy || polling || recovering || expired || terminal(activeOrder.payment_status);
    if(expired)el('bookPaymentState').textContent='คำสั่งซื้อนี้หมดเวลาการชำระเงินแล้ว';
  }
  function renderOrder(order){
    activeOrder=validateOrder(order);
    renderSummary(order.items,order.total_amount);
    el('bookCheckout').hidden=false;
    el('bookBuyerForm').hidden=true;
    el('bookPaymentPanel').hidden=false;
    el('bookOrderReference').textContent=`หมายเลขคำสั่งซื้อ: ${order.order_id}`;
    el('bookPaymentState').textContent=statusText[order.payment_status];
    el('bookOrderExpiry').textContent=`ชำระภายใน ${new Date(order.expires_at).toLocaleString('th-TH',{timeZone:'Asia/Bangkok'})} (Asia/Bangkok)`;
    el('bookOrderStatus').textContent=order.payment_status==='expired'?'คำสั่งซื้อนี้หมดเวลาการชำระเงินแล้ว':`สถานะคำสั่งซื้อ: ${statusText[order.payment_status]}`;
    el('bookNewOrder').hidden=!terminal(order.payment_status);
    if(qrOrderId!==order.order_id)el('bookQrArea').hidden=true;
    updateDeadline();renderCart();
  }
  async function refreshOrder(){
    if(!orderToken || busy || polling || recovering)return;
    polling=true;updateDeadline();
    try{
      const path=activeOrder?`/api/book-orders/${encodeURIComponent(activeOrder.order_id)}`:'/api/book-orders/session/current';
      renderOrder(await orderRequest(path));
    }catch(error){
      if(error.status!==404 || activeOrder)showOrderError(error);
    }finally{polling=false;updateDeadline();}
  }
  el('bookCheckoutButton').addEventListener('click',() => {
    if((!count() && !activeOrder) || busy || recovering)return;
    if(activeOrder)renderOrder(activeOrder);else renderSummary();
    el('bookCheckout').hidden=false;
    el('bookCheckoutHeading').focus();
    el('bookCheckout').scrollIntoView({behavior:'smooth',block:'start'});
  });
  el('bookBuyerForm').addEventListener('submit',async event => {
    event.preventDefault();
    if(busy || recovering || activeOrder || !count() || !el('bookBuyerForm').reportValidity())return;
    const buyerName=el('bookBuyerName').value.trim();
    if(!buyerName){el('bookOrderStatus').textContent='กรุณาระบุชื่อผู้สั่งซื้อ';el('bookBuyerName').focus();return;}
    busy=true;renderCart();
    try{
      const token=ensureOrderToken(); // Save before POST so a lost response can be recovered.
      const payload={buyer_name:buyerName,buyer_email:el('bookBuyerEmail').value.trim(),buyer_phone:el('bookBuyerPhone').value.trim()||null,items:lines().map(book=>({book_id:book.id,quantity:book.quantity})),payment_method:'promptpay',idempotency_key:token};
      el('bookOrderStatus').textContent='กำลังสร้างคำสั่งซื้อ ยังไม่มีการชำระเงิน…';
      renderOrder(await orderRequest('/api/book-orders','POST',payload));
      el('bookPaymentHeading').focus();
    }catch(error){
      if(error.status===409){
        // A previous POST may have committed despite a lost response. Recover its
        // immutable order instead of issuing a different key or replacing items.
        try{renderOrder(await orderRequest('/api/book-orders/session/current'));}
        catch(recoveryError){showOrderError(recoveryError);}
      }else showOrderError(error);
    }finally{busy=false;renderCart();updateDeadline();}
  });
  el('bookPromptPayButton').addEventListener('click',async()=>{
    if(!activeOrder || busy || polling || terminal(activeOrder.payment_status) || expiredLocally())return;
    busy=true;updateDeadline();renderCart();
    try{
      const result=await orderRequest(`/api/book-orders/${encodeURIComponent(activeOrder.order_id)}/payment`,'POST');
      if(!/^data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+$/.test(result.qr_image) || result.order?.payment_status!=='awaiting_verification')throw new Error('Invalid payment instruction');
      renderOrder(result.order);
      if(!expiredLocally()){
        qrOrderId=activeOrder.order_id;
        el('bookQrImage').src=result.qr_image;
        el('bookMerchantName').textContent=`ผู้รับเงิน: ${result.merchant_name}`;
        el('bookQrArea').hidden=false;
      }
    }catch(error){el('bookQrArea').hidden=true;showOrderError(error);}
    finally{busy=false;renderCart();updateDeadline();}
  });
  el('bookRefreshStatus').addEventListener('click',refreshOrder);
  el('bookNewOrder').addEventListener('click',()=>{
    if(!activeOrder || !terminal(activeOrder.payment_status) || busy || polling)return;
    try{sessionStorage.removeItem(ORDER_KEY);}catch(_){return;}
    activeOrder=null;orderToken=null;qrOrderId=null;
    el('bookPaymentPanel').hidden=true;el('bookQrArea').hidden=true;
    el('bookQrImage').removeAttribute('src');el('bookBuyerForm').hidden=false;
    el('bookBuyerName').value='';el('bookBuyerEmail').value='';el('bookBuyerPhone').value='';
    el('bookOrderStatus').textContent='กรุณาตรวจสอบรายการก่อนสร้างคำสั่งซื้อใหม่';
    renderCart();renderSummary();
  });
  setInterval(()=>{
    updateDeadline();
    if(!document.hidden && el('books').classList.contains('active') && activeOrder && !terminal(activeOrder.payment_status))refreshOrder();
  },15000);
  // Hide stale QR locally even when a slow/offline status request cannot complete.
  setInterval(updateDeadline,1000);
  document.addEventListener('visibilitychange',()=>{
    if(!document.hidden && el('books').classList.contains('active'))refreshOrder();
  });
  renderCatalog();renderCart();
  if(orderToken){
    recovering=true;renderCart();
    orderRequest('/api/book-orders/session/current').then(renderOrder).catch(error=>{
      if(error.status!==404){el('bookCheckout').hidden=false;showOrderError(error);}
    }).finally(()=>{recovering=false;renderCart();updateDeadline();});
  }
})();
