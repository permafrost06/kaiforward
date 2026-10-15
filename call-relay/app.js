'use strict';
const el = id => document.getElementById(id);
const ussdLabels = {waiting:'waiting for the network', reply:'reply needed', done:'done', failed:'failed', cancelled:'cancelled'};
const labels = {'-2':'checking', '-1':'no call', 0:'connected', 1:'on hold', 2:'calling', 3:'ringing', 4:'incoming call', 5:'call waiting'};
let socket, context, stream, node;
let state = {phone_connected:false, slots:[]};
let busy = false, ussdPending = false, seenActive = false, audioSlot = 0, micError = '';

function send(message) {
  if (socket?.readyState !== WebSocket.OPEN) throw new Error('Server connection is offline');
  socket.send(JSON.stringify(message));
}

const sim = slot => state.slots.find(s => s.slot === slot) || {};
const ussd = slot => state.ussd?.[slot - 1] || {};
const ussdOpen = slot => ['waiting', 'reply'].includes(ussd(slot).state);

function ussdText(slot) {
  const u = ussd(slot);
  if (!u.state || u.state === 'idle') return '';
  const reason = u.state !== 'failed' ? ''
    : u.type === 4 ? ': not supported by the network'
    : u.type === 5 ? ': the network timed out'
    : u.error === -2 ? ': no answer from the network'
    : u.error === -1 ? ': the phone lost its modem connection'
    : u.error > 0 ? `: modem error ${u.error}` : '';
  return `SIM ${slot} USSD ${ussdLabels[u.state] ?? u.state}${reason}` + (u.message ? `\n${u.message}` : '');
}
// Only one SIM can have a call at a time.
const callSlot = () => [1, 2].find(slot => sim(slot).calls === 1) || 0;

function render() {
  const online = state.phone_connected;
  const slot = callSlot(), call = sim(slot);
  el('status').textContent = !online ? 'Phone offline'
    : slot ? `SIM ${slot}: ${labels[call.state] ?? 'checking'}` + (context ? `, audio ${state.media === 'running' ? 'connected' : 'waiting'}${stream ? '' : ` (listen only, microphone: ${micError})`}` : '')
    : [1, 2].map(s => `SIM ${s} ${sim(s).registered ? 'ready' : 'unavailable'}`).join(', ');
  el('answer').disabled = busy || !online || call.state !== 4;
  el('hangup').disabled = busy || !online || !slot;
  for (const s of [1, 2]) el('dial' + s).disabled = busy || !online || Boolean(slot) || !sim(s).registered || sim(s).state !== -1;
  for (const s of [1, 2]) el('ussd' + s).disabled = busy || !online || !sim(s).registered || ussd(s).state === 'waiting';
  el('ussdcancel').disabled = busy || !online || ![1, 2].some(ussdOpen);
  el('ussd').textContent = [1, 2].map(ussdText).filter(Boolean).join('\n\n');
  el('audio').textContent = context ? 'Audio off' : 'Audio on';
  el('audio').disabled = !context && (!online || !slot);
  if (state.audio_error) el('notice').textContent = state.audio_error;
  const audioCall = sim(audioSlot);
  if (audioCall.state === 0) seenActive = true;
  if (context && (!online || (seenActive && audioCall.calls === 0))) stopAudio();
}

async function startAudio(slot) {
  if (context) return;
  if (!state.phone_connected) throw new Error('Phone is offline');
  const audioContext = new AudioContext({latencyHint:'interactive'});
  let media = null;
  try {
    await audioContext.resume();
    try {
      media = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true}});
    } catch (error) {
      // Without a microphone, still play the call and send silence.
      micError = error.message;
    }
    await audioContext.audioWorklet.addModule('/audio-worklet.js');
    node = new AudioWorkletNode(audioContext, 'call-audio', {numberOfInputs:1, numberOfOutputs:1, outputChannelCount:[1]});
    node.port.onmessage = ({data}) => {
      if (socket?.readyState === WebSocket.OPEN && socket.bufferedAmount < 4096 && state.media === 'running') socket.send(data);
    };
    if (media) audioContext.createMediaStreamSource(media).connect(node);
    node.connect(audioContext.destination);
    context = audioContext; stream = media; audioSlot = slot; seenActive = false;
    send({type:'audio', enabled:true, slot});
  } catch (error) {
    media?.getTracks().forEach(t => t.stop());
    node = null; await audioContext.close(); throw error;
  }
  render();
}

function stopAudio() {
  stream?.getTracks().forEach(t => t.stop());
  context?.close();
  const wasOn = Boolean(context);
  context = stream = node = null; seenActive = false;
  if (wasOn && socket?.readyState === WebSocket.OPEN) send({type:'audio', enabled:false, slot:audioSlot});
  render();
}

async function command(action, slot) {
  try {
    busy = true; render();
    if (action !== 'hangup') await startAudio(slot);
    send({type:'command', action, slot, number:el('number').value.trim()});
    el('notice').textContent = 'Waiting for the phone';
  } catch (error) { busy = false; el('notice').textContent = error.message; stopAudio(); }
}

function ussdCommand(action, slot) {
  try {
    busy = true; render();
    send({type:'command', action, slot, code:el('code').value.trim()});
    ussdPending = true;
    el('notice').textContent = 'Waiting for the phone';
  } catch (error) { busy = false; el('notice').textContent = error.message; render(); }
}

function connect() {
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/call`);
  socket.binaryType = 'arraybuffer';
  socket.onmessage = ({data}) => {
    if (data instanceof ArrayBuffer) { node?.port.postMessage(data, [data]); return; }
    const message = JSON.parse(data);
    if (message.type === 'state') state = message;
    if (message.type === 'result') {
      const wasUssd = ussdPending;
      busy = ussdPending = false;
      el('notice').textContent = message.ok ? (wasUssd ? '' : 'Done') : (message.error || (message.uncertain ? 'Result unknown; check the call state before retrying' : `Phone refused (${message.result})`));
      if (!message.ok && !message.uncertain && !wasUssd) stopAudio();
    }
    render();
  };
  socket.onclose = () => { state = {phone_connected:false, slots:[]}; busy = ussdPending = false; stopAudio(); setTimeout(connect, 2000); };
}

el('answer').onclick = () => command('answer', callSlot());
el('hangup').onclick = () => command('hangup', callSlot());
el('dial1').onclick = () => command('dial', 1);
el('dial2').onclick = () => command('dial', 2);
el('ussd1').onclick = () => ussdCommand('ussd', 1);
el('ussd2').onclick = () => ussdCommand('ussd', 2);
el('ussdcancel').onclick = () => ussdCommand('ussd-cancel', [1, 2].find(ussdOpen));
el('audio').onclick = async () => {
  try { context ? stopAudio() : await startAudio(callSlot()); } catch (error) { el('notice').textContent = error.message; }
};
window.addEventListener('pagehide', () => { stopAudio(); socket?.close(); });
connect();
