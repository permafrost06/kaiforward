const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const { IDBFactory } = require('fake-indexeddb');

const source = fs.readFileSync('phone/app.js', 'utf8');
const sms = (id = 1, body = 'বাংলা SMS ✓') => ({type: 'sms', delivery: 'received', id, sender: '+880123', body, timestamp: new Date(1720000000000 + id)});
const sentSms = (id = 2) => ({...sms(id), delivery:'sent', sender:null, receiver:'+880456'});

function boot({database = new IDBFactory(), inbox = [], failure = false, malformed = false, stallInbox = false, fastTimeouts = false} = {}) {
  const uploads = [], handlers = {}, alarms = [];
  let mode = {failure, malformed};
  const request = result => {
    const object = {result};
    setTimeout(() => object.onsuccess && object.onsuccess(), 0);
    return object;
  };
  const window = {KAIFORWARD_CONFIG: {endpoint: 'http://127.0.0.1:8765', token: 'test', device_id: 'phone'}, addEventListener() {}, close() {}};
  const elements = {};
  const document = {body: {getAttribute: () => null}, getElementById: id => elements[id] || (elements[id] = {}), addEventListener() {}};
  const navigator = {
    requestWakeLock: () => ({unlock() {}}),
    mozSetMessageHandler: (type, handler) => { handlers[type] = handler; },
    mozAlarms: {
      getAll: () => request(alarms.slice()),
      remove: id => { const index = alarms.findIndex(alarm => alarm.id === id); if (index >= 0) alarms.splice(index, 1); },
      add: (date, timezone, data) => { alarms.push({id: 1, date, data}); return request(1); }
    },
    mozMobileMessage: {getMessages(filter) {
      assert.equal(Object.keys(filter).length, 0);
      let index = 0;
      const cursor = {continue: () => setTimeout(next, 0)};
      function next() { cursor.result = inbox[index++]; cursor.onsuccess(); }
      if (!stallInbox) setTimeout(next, 0);
      return cursor;
    }}
  };
  class XHR {
    open(method, url) { assert.equal(method, 'POST'); assert.equal(url, 'http://127.0.0.1:8765/v1/messages'); }
    setRequestHeader() {}
    send(body) {
      uploads.push(JSON.parse(body));
      setTimeout(() => {
        if (mode.failure) { this.onerror(); return; }
        this.status = 200;
        this.responseText = JSON.stringify({accepted: mode.malformed ? ['wrong-key'] : uploads[uploads.length - 1].messages.map(item => item.key)});
        this.onload();
      }, 0);
    }
  }
  const appTimer = (callback, ms) => setTimeout(callback, fastTimeouts && ms >= 30000 ? 20 : ms);
  vm.runInNewContext(source, {window, document, navigator, indexedDB: database, XMLHttpRequest: XHR, Promise, setTimeout: appTimer, clearTimeout});
  return {window, database, uploads, handlers, alarms, elements, setMode(value) { mode = value; }};
}

async function waitFor(check) {
  const deadline = Date.now() + 4000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error('Phone test timed out');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

test('received SMS uploads and inbox reconciliation does not upload it twice', async () => {
  const app = boot({inbox: [sms()]});
  await waitFor(() => app.window.KaiForward.stats.uploaded === 1);
  assert.equal(app.uploads[0].messages[0].body, 'বাংলা SMS ✓');
  const first = app.window.KaiForward.stats.lastSync;
  app.window.KaiForward.sync();
  await waitFor(() => app.window.KaiForward.stats.lastSync > first);
  assert.equal(app.uploads.length, 1);
  assert.equal(app.alarms.length, 1);
});

test('network failure retains queue across app restart and retries successfully', async () => {
  const first = boot({inbox: [sms()], failure: true});
  await waitFor(() => first.window.KaiForward.stats.pending === 1);
  assert.equal(first.window.KaiForward.stats.uploaded, 0);
  const second = boot({database: first.database});
  await waitFor(() => second.window.KaiForward.stats.uploaded === 1);
  assert.equal(second.window.KaiForward.stats.pending, 0);
  assert.equal(second.uploads[0].messages.length, 1);
});

test('invalid acknowledgement never discards a queued SMS', async () => {
  const app = boot({inbox: [sms()], malformed: true});
  await waitFor(() => app.window.KaiForward.stats.pending === 1);
  assert.equal(app.window.KaiForward.stats.uploaded, 0);
  assert.match(app.window.KaiForward.stats.lastError, /acknowledgement/);
});

test('incoming event is durably queued even when absent from inbox', async () => {
  const app = boot();
  await waitFor(() => app.window.KaiForward.stats.lastSync);
  app.handlers['sms-received'](sms(2));
  await waitFor(() => app.window.KaiForward.stats.uploaded === 1);
  assert.equal(app.uploads[0].messages[0].id, 2);
});

test('sent SMS includes its recipient and direction alongside received SMS', async () => {
  const app = boot({inbox: [sms(), sentSms()]});
  await waitFor(() => app.window.KaiForward.stats.uploaded === 2);
  const messages = app.uploads.flatMap(upload => upload.messages);
  assert.equal(messages[0].direction, 'received');
  assert.equal(messages[0].recipient, '');
  assert.equal(messages[1].direction, 'sent');
  assert.equal(messages[1].recipient, '+880456');
  assert.equal(messages[1].sender, '');
  const first = app.window.KaiForward.stats.lastSync;
  app.window.KaiForward.sync();
  await waitFor(() => app.window.KaiForward.stats.lastSync > first);
  assert.equal(app.uploads.length, 1);
});

test('sent event persists offline and uploads after restart even if absent from the phone database', async () => {
  const first = boot({failure:true});
  await waitFor(() => first.window.KaiForward.stats.lastSync);
  first.handlers['sms-sent'](sentSms());
  await waitFor(() => first.window.KaiForward.stats.pending === 1);
  const second = boot({database:first.database});
  await waitFor(() => second.window.KaiForward.stats.uploaded === 1);
  assert.equal(second.uploads[0].messages[0].direction, 'sent');
  assert.equal(second.uploads[0].messages[0].recipient, '+880456');
});

test('MMS, sending and failed messages are excluded', async () => {
  const app = boot({inbox: [{...sentSms(), delivery: 'sending'}, {...sentSms(3), delivery:'error'}, {...sms(4), type: 'mms'}]});
  await waitFor(() => app.window.KaiForward.stats.lastSync);
  assert.equal(app.uploads.length, 0);
  assert.equal(app.window.KaiForward.stats.uploaded, 0);
});

test('more than one batch uploads without dropping messages', async () => {
  const app = boot({inbox: Array.from({length: 65}, (_, index) => sms(index + 1))});
  await waitFor(() => app.window.KaiForward.stats.uploaded === 65);
  assert.deepEqual(app.uploads.map(upload => upload.messages.length), [50, 15]);
  assert.equal(app.window.KaiForward.stats.pending, 0);
});

test('stalled SMS database reports a timeout and releases the sync button', async () => {
  const app = boot({stallInbox: true, fastTimeouts: true});
  assert.equal(app.elements.sync.disabled, true);
  assert.equal(app.elements.sync.textContent, 'Syncing…');
  await waitFor(() => app.window.KaiForward.stats.lastError && !app.elements.sync.disabled);
  assert.match(app.elements.status.textContent, /SMS database did not respond/);
  assert.equal(app.elements.sync.textContent, 'Sync now');
  assert.equal(app.uploads.length, 0);
});

test('pressing sync during an active scan gives immediate feedback', async () => {
  const app = boot({stallInbox: true, fastTimeouts: true});
  app.elements.sync.onclick();
  assert.match(app.elements.status.textContent, /Sync is running/);
  await waitFor(() => app.window.KaiForward.stats.lastError);
});
