(function () {
  'use strict';
  var config = window.KAIFORWARD_CONFIG;
  var background = document.body.getAttribute('data-background') === 'true';
  var database, active = false, again = false, wakeLock;
  var stats = { uploaded: 0, pending: 0, lastError: null, lastSync: null, scanned: 0 };
  // Exposes only counts and errors to device diagnostics, never message contents.
  window.KaiForward = { stats: stats, sync: synchronize };

  function status(text) {
    var element = document.getElementById('status');
    if (element) { element.textContent = text; }
  }
  function requestResult(request) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () { reject(new Error('Device API did not respond. Reopen KaiForward to retry.')); }, 30000);
      request.onsuccess = function () { clearTimeout(timer); resolve(request.result); };
      request.onerror = function () { clearTimeout(timer); reject(new Error(request.error ? request.error.name : 'Device API failed')); };
    });
  }
  function transact(stores, action) {
    return new Promise(function (resolve, reject) {
      var transaction = database.transaction(stores, 'readwrite');
      var result;
      transaction.oncomplete = function () { resolve(result); };
      transaction.onabort = transaction.onerror = function () { reject(new Error('Local storage failed')); };
      action(transaction, function (value) { result = value; });
    });
  }
  var ready = new Promise(function (resolve, reject) {
    var request = indexedDB.open('kaiforward', 1);
    request.onupgradeneeded = function () {
      request.result.createObjectStore('pending', { keyPath: 'key' });
      request.result.createObjectStore('sent', { keyPath: 'key' });
    };
    request.onsuccess = function () { database = request.result; resolve(); };
    request.onerror = function () { reject(new Error('Cannot open local storage')); };
  });

  function enqueue(message) {
    if (!message || message.type !== 'sms' || (message.delivery !== 'received' && message.delivery !== 'sent')) { return Promise.resolve(); }
    var timestamp = Number(message.timestamp);
    var record = { key: message.id + ':' + timestamp, id: message.id, timestamp: timestamp,
                   sender: message.sender || '', recipient: message.delivery === 'sent' ? message.receiver || '' : '',
                   direction: message.delivery, body: message.body || '' };
    return ready.then(function () {
      return transact(['pending', 'sent'], function (transaction) {
        var sent = transaction.objectStore('sent').get(record.key);
        sent.onsuccess = function () {
          if (!sent.result) { transaction.objectStore('pending').put(record); }
        };
      });
    });
  }
  function scanMessages() {
    return new Promise(function (resolve, reject) {
      if (!navigator.mozMobileMessage) { reject(new Error('SMS permission unavailable')); return; }
      var cursor = navigator.mozMobileMessage.getMessages({}, false);
      var settled = false;
      var timer;
      function watchProgress() {
        clearTimeout(timer);
        timer = setTimeout(function () {
          settled = true;
          reject(new Error('SMS database did not respond. Check phone service and try again.'));
        }, 45000);
      }
      watchProgress();
      function end(error) {
        if (settled) { return; }
        settled = true;
        clearTimeout(timer);
        if (error) { reject(error); } else { resolve(); }
      }
      cursor.onerror = function () { end(new Error('SMS scan failed')); };
      cursor.onsuccess = function () {
        if (settled) { return; }
        if (!cursor.result) { end(); return; }
        watchProgress();
        enqueue(cursor.result).then(function () {
          if (settled) { return; }
          stats.scanned += 1;
          status('Checking SMS: ' + stats.scanned + ' messages');
          cursor.continue();
        }, end);
      };
    });
  }
  function batch() {
    return transact(['pending'], function (transaction, result) {
      var records = [];
      var request = transaction.objectStore('pending').openCursor();
      request.onsuccess = function () {
        var cursor = request.result;
        if (cursor && records.length < 50) { records.push(cursor.value); cursor.continue(); }
        else { result(records); }
      };
    });
  }
  function upload(records) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest({ mozSystem: true });
      xhr.open('POST', config.endpoint + '/v1/messages', true);
      xhr.timeout = 20000;
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.setRequestHeader('Authorization', 'Bearer ' + config.token);
      xhr.onload = function () {
        if (xhr.status !== 200) { reject(new Error('Server returned ' + xhr.status)); return; }
        try {
          var response = JSON.parse(xhr.responseText);
          if (!Array.isArray(response.accepted) || response.accepted.length !== records.length ||
              records.some(function (record) { return response.accepted.indexOf(record.key) < 0; })) {
            throw new Error('Invalid server acknowledgement');
          }
          resolve();
        } catch (error) { reject(error); }
      };
      xhr.onerror = function () { reject(new Error('Connection failed; messages kept for retry')); };
      xhr.ontimeout = function () { reject(new Error('Connection timed out; messages kept for retry')); };
      xhr.send(JSON.stringify({ device_id: config.device_id, messages: records }));
    });
  }
  function drain(deadline) {
    return batch().then(function (records) {
      if (!records.length || Date.now() > deadline) { return; }
      status('Uploading ' + records.length + ' SMS…');
      return upload(records).then(function () {
        return transact(['pending', 'sent'], function (transaction) {
          records.forEach(function (record) {
            transaction.objectStore('sent').put({ key: record.key });
            transaction.objectStore('pending').delete(record.key);
          });
        });
      }).then(function () { return drain(deadline); });
    });
  }
  function updateCounts() {
    return transact(['pending', 'sent'], function (transaction) {
      transaction.objectStore('pending').count().onsuccess = function (event) { stats.pending = event.target.result; };
      transaction.objectStore('sent').count().onsuccess = function (event) { stats.uploaded = event.target.result; };
    }).then(function () {
      var element = document.getElementById('counts');
      if (element) { element.textContent = stats.uploaded + ' backed up · ' + stats.pending + ' pending'; }
    });
  }
  function scheduleRetry() {
    if (!navigator.mozAlarms) { return Promise.reject(new Error('Alarm API unavailable')); }
    return requestResult(navigator.mozAlarms.getAll()).then(function (alarms) {
      alarms.forEach(function (alarm) {
        if (alarm.data && alarm.data.app === 'kaiforward') { navigator.mozAlarms.remove(alarm.id); }
      });
      return requestResult(navigator.mozAlarms.add(new Date(Date.now() + 5 * 60 * 1000), 'ignoreTimezone', { app: 'kaiforward' }));
    });
  }
  function finish() {
    return updateCounts().then(function () {
      if (wakeLock) { wakeLock.unlock(); wakeLock = null; }
      active = false;
      setBusy(false);
      if (again) { again = false; return synchronize(); }
      if (background) { window.close(); }
    });
  }
  function synchronize() {
    if (active) { again = true; return; }
    active = true;
    setBusy(true);
    stats.lastError = null;
    stats.scanned = 0;
    try { if (navigator.requestWakeLock) { wakeLock = navigator.requestWakeLock('cpu'); } } catch (error) {}
    status('Checking SMS…');
    return ready.then(scheduleRetry).then(scanMessages).then(function () { return drain(Date.now() + 60000); })
      .then(function () { stats.lastSync = Date.now(); status('Sync complete'); })
      .catch(function (error) { stats.lastError = error.message; status(error.message); })
      .then(finish).catch(function (error) {
        stats.lastError = error.message;
        active = false;
        setBusy(false);
        if (wakeLock) { wakeLock.unlock(); wakeLock = null; }
        status(error.message);
      });
  }

  function setBusy(value) {
    var button = document.getElementById('sync');
    if (button) { button.disabled = value; button.textContent = value ? 'Syncing…' : 'Sync now'; }
  }
  function manualSync() {
    if (active) { status('Sync is running. Checked ' + stats.scanned + ' messages.'); return; }
    synchronize();
  }

  if (!config || !/^https:\/\//.test(config.endpoint) && config.endpoint !== 'http://127.0.0.1:8765') {
    status('Receiver configuration missing or invalid'); return;
  }
  if (navigator.mozSetMessageHandler) {
    function onMessage(message) {
      enqueue(message).then(synchronize).catch(function (error) { stats.lastError = error.message; status(error.message); });
    }
    navigator.mozSetMessageHandler('sms-received', onMessage);
    navigator.mozSetMessageHandler('sms-sent', onMessage);
    navigator.mozSetMessageHandler('alarm', function (alarm) {
      if (alarm.data && alarm.data.app === 'kaiforward') { synchronize(); }
    });
  }
  window.addEventListener('online', synchronize);
  var button = document.getElementById('sync');
  if (button) { button.onclick = manualSync; }
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' || event.key === 'SoftLeft') { event.preventDefault(); manualSync(); }
  });
  synchronize();
}());
