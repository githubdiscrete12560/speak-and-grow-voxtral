const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const home = html.match(/<section id="home"[^>]*>([\s\S]*?)<\/section>/)[1];

assert.match(html, /<title>Safe Zone by พี่น้อย \| /);
assert.match(html, /<div class="brand"><span class="logo" aria-hidden="true">[^<]+<\/span><span>Safe Zone by <span class="brand-name">พี่น้อย<\/span><\/span><\/div>/);
assert.match(html, /<button class="tab active" data-tab="home" aria-current="true">เกี่ยวกับ Safe Zone by พี่น้อย<\/button>/);
assert.match(html, /<\/nav>\s*<button class="tab" data-tab="consultation"[^>]*>นัดหมายปรึกษา<\/button>/);
assert.match(home, /class="actions hero-actions"[\s\S]*?<button class="btn primary jump" data-target="consultation"[^>]*>[\s\S]*?<button class="btn secondary jump" data-target="psychologist"[^>]*>รู้จักผู้ให้คำปรึกษา<\/button>/);
assert.match(home, /<h1>เกี่ยวกับ Safe Zone by พี่น้อย<\/h1>/);
assert.equal((home.match(/Safe Zone by พี่น้อย/g) || []).length, 3);
assert.ok(html.includes('ชำระเงินแล้ว แต่ไม่สามารถยืนยันนัดหมายได้ กรุณาติดต่อ Safe Zone by พี่น้อย'));
assert.doesNotMatch(html, /\u0E1A้านสุขใจ|Safe Space by/);

assert.match(home, /class="hero-art encouragement-panel [^"]+"/);
assert.match(home, /คุณไม่จำเป็นต้องผ่านทุกอย่างไปเพียงลำพัง/);
assert.match(home, /ในวันที่ชีวิตรู้สึกหนัก ขอให้ค่อย ๆ ก้าวไปทีละนิด พักเมื่อจำเป็น และเปิดใจรับความช่วยเหลือเมื่อพร้อม/);
assert.match(home, /ทุกก้าวเล็ก ๆ ก็มีความหมาย/);
assert.doesNotMatch(home, /class="(?:hero-art mascot|face|eye left|mouth speaking)"/);
console.log('Homepage encouragement panel checks passed.');
