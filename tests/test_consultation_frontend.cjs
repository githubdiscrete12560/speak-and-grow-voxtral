const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(ids.length, new Set(ids).size, 'Duplicate HTML IDs');
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(match => match[1]);
for (const script of scripts) new vm.Script(script);
const script = scripts.find(text => text.includes('const consultationForm'));
const elements = Object.fromEntries(ids.map(id => [id, {
  value:'', checked:false, disabled:false, hidden:false, required:false,
  listeners:{}, options:[], textContent:'',
  addEventListener(name, fn){this.listeners[name]=fn;},
  replaceChildren(...options){this.options=options;this.value=options[0]?.value||'';},
  add(option){this.options.push(option);},
  reportValidity(){return true;}
}]));
elements.childAgeRange.value='20+ years';
let payload;
const context = vm.createContext({
  document:{getElementById(id){assert.ok(elements[id], `Missing ID ${id}`);return elements[id];}},
  window:{addEventListener(){}}, Date, Intl, Object, Math, Number,
  Option:function(text,value){this.text=text;this.value=value;},
  setInterval(){return 1;}, clearInterval(){}, BACKEND_URL:'http://test',
  async fetch(url, options){payload=JSON.parse(options.body);return {ok:false,status:400,json:async()=>({detail:'test'})};}
});
vm.runInContext(script.slice(script.indexOf('  const consultationForm'), script.indexOf('  function speak(')), context);
function evaluate(code){return vm.runInContext(code,context);}
function json(code){return JSON.parse(JSON.stringify(evaluate(code)));}
assert.deepEqual(json("availableSlots('2026-10-12', new Date('2026-10-06T00:00:00Z'))"), [['19:10','20:00']]);
assert.deepEqual(json("availableSlots('2026-10-10', new Date('2026-10-06T00:00:00Z'))"), [['10:00','10:50'],['19:30','20:20']]);
assert.deepEqual(json("availableSlots('2026-10-11', new Date('2026-10-06T00:00:00Z'))"), [['10:00','10:50'],['19:30','20:20']]);
assert.deepEqual(json("availableSlots('2026-10-10', new Date('2026-10-10T03:00:00Z'))"), [['19:30','20:20']]);
for(const date of ['2026-10-05','2027-12-01','2026-02-30','invalid']){
  assert.deepEqual(json(`availableSlots('${date}', new Date('2026-10-06T00:00:00Z'))`), []);
}
assert.equal(elements.consultationTimezone.value,'Asia/Bangkok');
assert.equal(elements.consultationConsent.required,true);
assert.equal(elements.guardianConsentSection.disabled,true);
assert.equal(elements.guardianConsentSection.hidden,true);
assert.equal(elements.appointmentTime.disabled,true);
async function submit(){await elements.consultationForm.listeners.submit({preventDefault(){}});}
(async()=>{
  const day = new Date(Date.now()+7*86400000);
  elements.appointmentDate.value=evaluate(`bangkokDate(new Date('${day.toISOString()}'))`);
  elements.appointmentDate.listeners.change();
  assert.ok(elements.appointmentTime.options.length);
  assert.ok(elements.appointmentTime.options[0].text.includes('–'));
  elements.guardianName.value='Client Example';
  elements.guardianEmail.value='client@example.com';
  elements.consultationReason.value='General consultation request';
  elements.consultationConsent.checked=true;
  await submit();
  assert.equal(payload.duration_minutes,50);
  assert.equal(payload.timezone,'Asia/Bangkok');
  assert.equal(payload.consent_confirmed,true);
  assert.equal(payload.under_20,false);
  assert.equal(payload.legal_guardian_email,null);
  assert.ok(!('child_display_name' in payload));
  assert.equal(new Date(payload.appointment_start).getTime(),new Date(`${elements.appointmentDate.value}T${elements.appointmentTime.value}:00+07:00`).getTime());
  elements.childAgeRange.value='under 20 years';
  elements.childAgeRange.listeners.change();
  assert.equal(elements.consultationConsent.disabled,true);
  assert.equal(elements.consultationConsent.required,false);
  assert.equal(elements.adultConsentSection.hidden,true);
  assert.equal(elements.guardianConsentSection.hidden,false);
  assert.equal(elements.guardianConsentSection.disabled,false);
  elements.legalGuardianName.value='Guardian Example';
  elements.legalGuardianEmail.value='guardian@example.com';
  elements.guardianRelationship.value='Mother';
  elements.guardianConsent.checked=true;
  await submit();
  assert.equal(payload.under_20,true);
  assert.equal(payload.consent_confirmed,false);
  assert.equal(payload.guardian_consent_confirmed,true);
  assert.equal(payload.legal_guardian_email,'guardian@example.com');
  elements.childAgeRange.value='20+ years';
  elements.childAgeRange.listeners.change();
  assert.equal(elements.consultationConsent.required,true);
  assert.equal(elements.guardianConsentSection.disabled,true);
  assert.equal(elements.guardianConsent.checked,false);
  payload=null;
  elements.appointmentTime.value='12:00';
  await submit();
  assert.equal(payload,null,'Invalid slot must not be submitted');
  console.log('Frontend checks passed: script initialization, IDs, Bangkok slots, date limits, consent switching, payloads.');
})().catch(error=>{console.error(error);process.exitCode=1;});
