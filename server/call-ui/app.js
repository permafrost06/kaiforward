'use strict';
const el = id => document.getElementById(id);
let socket, context, stream, node, source;
let state = {phone_connected:false, slots:[]};
let busy = false, seenActive = false, reconnect;
function send(message) {
  if (socket?.readyState !== WebSocket.OPEN) throw new Error('Browser connection is offline');
  socket.send(JSON.stringify(message));
}
function render() {
  const slot = Number(el('sim').value);
  const call = state.slots.find(s => s.slot === slot) || {};
  const labels = {'-2':'Checking call status','-1':'No call',0:'Call connected',1:'Call on hold',2:'Calling…',3:'Ringing…',4:'Incoming call',5:'Call waiting'};
  el('connection').textContent = state.phone_connected ? 'Phone online' : 'Phone offline';
  el('connection').classList.toggle('online', state.phone_connected);
  el('call-state').textContent = state.phone_connected ? (labels[call.state] || 'Checking SIM') : 'Waiting for phone';
  el('call-state').classList.toggle('incoming', call.state === 4);
  for (const option of el('sim').options) {
    const sim = state.slots.find(s => s.slot === Number(option.value));
    option.textContent = `SIM ${option.value}${sim ? (sim.registered ? ' · registered' : ' · unavailable') : ''}`;
  }
  el('answer').disabled = busy || !state.phone_connected || call.calls !== 1 || call.state !== 4;
  el('hangup').disabled = busy || !state.phone_connected || call.calls !== 1;
  el('dial').disabled = busy || !state.phone_connected || !call.registered || call.calls !== 0 || call.state !== -1;
  el('sim').disabled = Boolean(stream) || busy;
  el('audio').textContent = stream ? 'Disable audio' : 'Enable audio';
  el('audio-state').textContent = stream ? (state.media === 'running' ? 'Microphone and speaker connected' : 'Microphone ready · waiting for call audio') : 'Microphone off';
  if (state.audio_error) el('notice').textContent = state.audio_error;
  if (call.state === 0) seenActive = true;
  if (stream && (!state.phone_connected || (seenActive && call.calls === 0))) stopAudio();
}
async function startAudio() {
  if (stream) return;
  if (!state.phone_connected) throw new Error('Phone is offline');
  const audioContext = new AudioContext({latencyHint:'interactive'});
  try {
    await audioContext.resume();
    const media = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    try {
      await audioContext.audioWorklet.addModule('/audio-worklet.js');
      context = audioContext; stream = media; seenActive = false;
      source = context.createMediaStreamSource(stream);
      node = new AudioWorkletNode(context, 'call-audio', {numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
      node.port.onmessage = ({data}) => {
        if (data instanceof ArrayBuffer) {
          if (socket?.readyState === WebSocket.OPEN && socket.bufferedAmount < 4096 && state.media === 'running') socket.send(data);
        } else el('audio-level').firstElementChild.style.width = `${Math.min(100,data.peak * 150)}%`;
      };
      source.connect(node); node.connect(context.destination);
      send({type:'audio',enabled:true,slot:Number(el('sim').value)});
    } catch (error) { media.getTracks().forEach(t => t.stop()); throw error; }
  } catch (error) { await audioContext.close(); context = stream = node = source = null; throw error; }
  render();
}
function stopAudio() {
  source?.disconnect(); node?.disconnect(); stream?.getTracks().forEach(t => t.stop());
  context?.close(); context = stream = node = source = null; seenActive = false;
  if (socket?.readyState === WebSocket.OPEN) send({type:'audio',enabled:false,slot:Number(el('sim').value)});
  el('audio-level').firstElementChild.style.width = '0%';
  render();
}
async function command(action) {
  try {
    busy = true; render();
    if (action !== 'hangup') await startAudio();
    send({type:'command',action,slot:Number(el('sim').value),number:el('number').value.trim()});
    el('notice').textContent = action === 'hangup' ? 'Ending call…' : 'Waiting for the phone…';
  } catch (error) { busy = false; el('notice').textContent = error.message; stopAudio(); render(); }
}
function connect() {
  clearTimeout(reconnect);
  socket = new WebSocket(`ws://${location.host}/call`); socket.binaryType = 'arraybuffer';
  socket.onmessage = ({data}) => {
    if (data instanceof ArrayBuffer) { node?.port.postMessage(data, [data]); return; }
    const message = JSON.parse(data);
    if (message.type === 'state') { state = message; render(); }
    if (message.type === 'result') {
      busy = false;
      el('notice').textContent = message.ok ? 'Call operation accepted.' : (message.error || (message.uncertain ? 'Result unknown. Check call status before retrying.' : `Phone refused the operation (${message.result}).`));
      if (!message.ok && !message.uncertain) stopAudio();
      render();
    }
  };
  socket.onclose = () => { state = {phone_connected:false,slots:[]}; busy = false; stopAudio(); render(); reconnect = setTimeout(connect,2000); };
}
el('answer').onclick = () => command('answer');
el('hangup').onclick = () => command('hangup');
el('dial').onclick = () => command('dial');
el('audio').onclick = async () => { try { stream ? stopAudio() : await startAudio(); } catch (error) { el('notice').textContent = error.message; } };
el('sim').onchange = render;
window.addEventListener('pagehide', () => { stopAudio(); socket?.close(); });
connect();
