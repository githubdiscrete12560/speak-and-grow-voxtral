const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const home = html.match(/<section id="home"[^>]*>([\s\S]*?)<\/section>/)[1];

assert.match(home, /class="hero-art encouragement-panel"/);
assert.match(home, /คุณไม่จำเป็นต้องผ่านทุกอย่างไปเพียงลำพัง/);
assert.match(home, /ในวันที่ชีวิตรู้สึกหนัก ขอให้ค่อย ๆ ก้าวไปทีละนิด พักเมื่อจำเป็น และเปิดใจรับความช่วยเหลือเมื่อพร้อม/);
assert.match(home, /ทุกก้าวเล็ก ๆ ก็มีความหมาย/);
assert.doesNotMatch(home, /class="(?:hero-art mascot|face|eye left|mouth speaking)"/);
console.log('Homepage encouragement panel checks passed.');
